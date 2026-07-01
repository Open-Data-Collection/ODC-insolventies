"""Worker for insolventies.

Drains the `insolventies:tasks` Redis queue. For each kenmerk: scrape the case
via the rechtspraak API, download + upload any report PDFs to storage MinIO,
and write ONE row to `insolventies.raw_cases` per attempt (success or failure).

The worker is intentionally dumb about retries — it records every attempt with
a status, and the scheduler (src/scheduler.py) decides what to re-queue based on
those rows.
"""
from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone

from odc import OdcClient
from odc.logging import info, warn, error, heartbeat

from src.api import ApiClient
from src.scrape import build_record

PROJECT       = os.environ.get("PROJECT_NAME", os.environ.get("PROJECT", "insolventies"))
QUEUE_KEY     = os.environ.get("QUEUE_KEY", "insolventies:tasks")
RAW_TABLE     = "insolventies.raw_cases"
POP_TIMEOUT_S = int(os.environ.get("POP_TIMEOUT_S", "5"))
HEARTBEAT_S   = int(os.environ.get("HEARTBEAT_S", "60"))
FLUSH_ROWS    = int(os.environ.get("FLUSH_ROWS", "20"))
FLUSH_SECS    = int(os.environ.get("FLUSH_SECS", "10"))
GIT_SHA       = os.environ.get("GIT_SHA", "")

_running = True
_LOG = {"service": "worker", "project": PROJECT}


def _heartbeat_loop():
    while _running:
        heartbeat(project=PROJECT, service="worker")
        time.sleep(HEARTBEAT_S)


def _shutdown(signum, _frame):
    global _running
    info("shutdown requested", signal=signum, **_LOG)
    _running = False


def _upload_pdfs(client: OdcClient, api: ApiClient, record) -> None:
    """Download each report PDF and upload to storage MinIO; set doc.pdf_path."""
    for doc in record.documents:
        try:
            pdf_bytes = api.download_pdf(doc.kenmerk)
        except Exception as e:
            warn("pdf download failed", kenmerk=record.kenmerk, doc=doc.kenmerk,
                 err=f"{type(e).__name__}: {e}", **_LOG)
            continue
        key = f"{PROJECT}/pdfs/{doc.kenmerk}.pdf"
        try:
            client.put_object("storage", key, pdf_bytes, content_type="application/pdf")
            doc.pdf_path = key
        except Exception as e:
            warn("pdf upload failed", kenmerk=record.kenmerk, doc=doc.kenmerk,
                 err=f"{type(e).__name__}: {e}", **_LOG)


def scrape_task(client: OdcClient, api: ApiClient, kenmerk: str) -> dict:
    """Scrape one kenmerk, returning a raw_cases row (never raises)."""
    now = datetime.now(timezone.utc)
    base = {
        "kenmerk":     kenmerk,
        "scraped_at":  now,
        "status":      "ok",
        "entity_type": "",
        "error":       "",
        "record":      "",
        "git_sha":     GIT_SHA,
    }
    try:
        record = build_record(api, kenmerk)
    except Exception as e:
        error("scrape failed", kenmerk=kenmerk, err=f"{type(e).__name__}: {e}", **_LOG)
        base["status"] = "fetch_failed"
        base["error"] = f"{type(e).__name__}: {e}"[:1000]
        return base

    # Download PDFs for cases that have documents (company / eenmanszaak).
    if record.documents:
        _upload_pdfs(client, api, record)

    try:
        base["entity_type"] = record.type
        base["record"] = json.dumps(record.to_dict(), ensure_ascii=False)
    except Exception as e:
        base["status"] = "parse_failed"
        base["error"] = f"serialize: {type(e).__name__}: {e}"[:1000]
    return base


def _flush(ch, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    data = [[r[c] for c in cols] for r in rows]
    try:
        ch.insert(RAW_TABLE, data, column_names=cols)
        info("flushed", rows=len(rows), **_LOG)
    except Exception as e:
        error("flush failed", err=str(e), rows=len(rows), **_LOG)


def main() -> int:
    import threading

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    client = OdcClient(PROJECT)
    ch = client.clickhouse
    redis = client.redis
    api = ApiClient()

    info("worker start", queue=QUEUE_KEY, **_LOG)

    pending: list[dict] = []
    last_flush = time.time()

    while _running:
        popped = redis.blpop(QUEUE_KEY, timeout=POP_TIMEOUT_S)
        if popped is None:
            if pending and (time.time() - last_flush) > FLUSH_SECS:
                _flush(ch, pending)
                pending = []
                last_flush = time.time()
            continue

        _, body = popped
        try:
            task = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            warn("malformed task", body=str(body)[:200], **_LOG)
            continue

        kenmerk = task.get("kenmerk")
        if not kenmerk:
            warn("task missing kenmerk", task=task, **_LOG)
            continue

        row = scrape_task(client, api, kenmerk)
        pending.append(row)

        if len(pending) >= FLUSH_ROWS or (time.time() - last_flush) > FLUSH_SECS:
            _flush(ch, pending)
            pending = []
            last_flush = time.time()

    if pending:
        _flush(ch, pending)
    info("worker stop", **_LOG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
