#!/usr/bin/env python3
"""One-off backfill of legacy default.insolventies* cases into the new pipeline.

The legacy tables (default.insolventies / default.insolventie_publicaties,
scraped Jan-Apr 2026 by the pre-migration scraper) hold ~11.5k cases the new
pipeline never saw. The live API still serves full case detail for old
kenmerks, so the primary import path is a re-scrape through the normal
worker — richer than a row-copy (raw record JSON, verslagen PDFs for
companies, current state, refresh lifecycle).

Two phases, both idempotent / safe to re-run:

  push           Anti-join legacy kenmerks against raw_cases successes and
                 the live queue, then rpush {"kenmerk": ...} tasks onto
                 insolventies:tasks for the deployed workers to drain.
  import-failed  AFTER the queue drains: for legacy cases that still have no
                 status='ok' row (purged from the register — records are
                 removed ~6 months after a case ends), copy the legacy
                 processed rows into insolventies.processed_cases /
                 processed_publications so the data isn't lost. Server-side
                 INSERT..SELECT into ReplacingMergeTree → re-runs collapse.

Creds come from pass (same entries the odc-monitor tooling uses); override
with CLICKHOUSE_URL / REDIS_HOST / REDIS_PASSWORD env vars if needed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import redis
import requests

CH_HOST = "100.81.80.15"
REDIS_HOST = os.environ.get("REDIS_HOST", "100.83.247.12")
QUEUE = "insolventies:tasks"
BATCH = 1000


def _pass(entry: str) -> str:
    return subprocess.run(
        ["pass", entry], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]


def ch_url() -> str:
    if url := os.environ.get("CLICKHOUSE_URL"):
        return url
    pw = _pass("projects/ODC-infra/clickhouse")
    return f"http://{CH_HOST}:8123/?user=odc&password={pw}"


def ch(query: str) -> str:
    resp = requests.post(ch_url(), data=query.encode(), timeout=120)
    resp.raise_for_status()
    return resp.text


def redis_client() -> redis.Redis:
    pw = os.environ.get("REDIS_PASSWORD") or _pass("projects/ODC-infra/redis-services")
    return redis.Redis(host=REDIS_HOST, password=pw)


# Legacy kenmerks with no successful scrape in the new pipeline.
PENDING_SQL = """
SELECT DISTINCT kenmerk FROM default.insolventies
WHERE kenmerk NOT IN (
    SELECT kenmerk FROM insolventies.raw_cases WHERE status = 'ok'
)
FORMAT TSV
"""


def cmd_push() -> int:
    pending = [k for k in ch(PENDING_SQL).splitlines() if k]
    print(f"legacy kenmerks without a successful scrape: {len(pending)}")

    r = redis_client()
    queued = set()
    for body in r.lrange(QUEUE, 0, -1):
        try:
            queued.add(json.loads(body).get("kenmerk"))
        except (json.JSONDecodeError, TypeError):
            pass
    todo = [k for k in pending if k not in queued]
    print(f"already in queue: {len(pending) - len(todo)}, to push: {len(todo)}")

    pushed = 0
    for i in range(0, len(todo), BATCH):
        pipe = r.pipeline()
        for k in todo[i : i + BATCH]:
            pipe.rpush(QUEUE, json.dumps({"kenmerk": k, "court": ""}))
        pipe.execute()
        pushed += len(todo[i : i + BATCH])
        print(f"  pushed {pushed}/{len(todo)}", end="\r")
    print(f"\ndone. queue depth now: {r.llen(QUEUE)}")
    return 0


# Copy legacy rows for cases the re-scrape could not recover. Explicit column
# lists (schemas are identical, but don't depend on ordering); the DateTime →
# DateTime64 scraped_at cast is implicit.
IMPORT_CASES_SQL = """
INSERT INTO insolventies.processed_cases
    (kenmerk, insolventienummer, toezichtzaaknummer, type, court, judge,
     is_anonymized, debtor_name, kvk_nummer, city, curator_names,
     publication_count, document_count, scraped_at)
SELECT
     kenmerk, insolventienummer, toezichtzaaknummer, type, court, judge,
     is_anonymized, debtor_name, kvk_nummer, city, curator_names,
     publication_count, document_count, scraped_at
FROM default.insolventies FINAL
WHERE kenmerk NOT IN (
    SELECT kenmerk FROM insolventies.raw_cases WHERE status = 'ok'
)
"""

IMPORT_PUBS_SQL = """
INSERT INTO insolventies.processed_publications
    (kenmerk, publicatie_kenmerk, publicatie_datum, description, event_type,
     event_subtype, event_date, insolvency_type, scraped_at)
SELECT
     kenmerk, publicatie_kenmerk, publicatie_datum, description, event_type,
     event_subtype, event_date, insolvency_type, scraped_at
FROM default.insolventie_publicaties FINAL
WHERE kenmerk NOT IN (
    SELECT kenmerk FROM insolventies.raw_cases WHERE status = 'ok'
)
"""


def cmd_import_failed() -> int:
    depth = redis_client().llen(QUEUE)
    if depth > 0:
        print(f"queue still has {depth} tasks — the re-scrape hasn't finished.")
        print("run this again once the queue is drained (or pass --force).")
        if "--force" not in sys.argv:
            return 1

    n = ch(PENDING_SQL).splitlines()
    print(f"legacy cases still without a successful scrape: {len([x for x in n if x])}")
    print("importing their legacy rows into processed_cases / processed_publications ...")
    ch(IMPORT_CASES_SQL)
    ch(IMPORT_PUBS_SQL)
    for table in ("processed_cases", "processed_publications"):
        cnt = ch(f"SELECT uniqExact(kenmerk) FROM insolventies.{table}").strip()
        print(f"  insolventies.{table}: {cnt} unique kenmerks")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "push":
        return cmd_push()
    if cmd == "import-failed":
        return cmd_import_failed()
    print(__doc__)
    print("usage: backfill_legacy.py {push|import-failed [--force]}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
