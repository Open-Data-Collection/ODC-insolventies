"""Scheduler for insolventies.

Discovery runs here (not against an upstream CH table). Each run queues two
disjoint sets of kenmerks onto `insolventies:tasks`:

1. **Discovery** — searches every court for recent publications, then anti-joins
   the discovered kenmerks against `insolventies.raw_cases` so only cases that
   haven't been scraped successfully yet are queued (with a cooldown +
   max-attempts cap for previously-failed kenmerks). Each publication event has
   its own kenmerk, so new events surface here as new discoveries.

2. **Refresh** (full-lifecycle) — re-queues already-succeeded company /
   eenmanszaak cases that are NOT yet in a terminal state (no opheffing/einde
   event) and whose last successful scrape is older than REFRESH_DAYS. Verslagen
   (financial reports) accrue over a case's multi-year life and are added long
   after the case first appears, so a case must be revisited to catch them.
   Bounded oldest-first by REFRESH_BATCH per run so cost stays predictable.

Deployed as a Nomad batch-periodic job on odc-services (next to Redis).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from odc.scheduler import Scheduler
from odc.logging import info

from src.api import ApiClient
from src.discovery import discover_all

MAX_ATTEMPTS = 3
COOLDOWN_MINS = 60

# Full-lifecycle refresh knobs (env-overridable).
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS", "14"))
REFRESH_BATCH = int(os.environ.get("REFRESH_BATCH", "2000"))
# Publication event_types that mark a case as closed (no more verslagen expected).
TERMINAL_EVENTS = ("opheffing", "einde")


class InsolventiesScheduler(Scheduler):
    name = "insolventies-scheduler"
    target_queue = "insolventies:tasks"
    backpressure_threshold = 100_000

    def fetch_rows(self):
        out = []
        queued = set()

        # --- 1. Discovery: never-succeeded kenmerks from the last month ---
        api = ApiClient()
        cases = discover_all(api, output=_NullWriter())
        candidates = {c.kenmerk: c for c in cases}
        info("discovery done", discovered=len(candidates), **self._log_kwargs())

        if candidates:
            rows = self.ch.query(
                """
                SELECT
                    kenmerk,
                    max(scraped_at)                         AS last_attempt,
                    count()                                 AS attempts,
                    countIf(status = 'ok')                  AS successes
                FROM insolventies.raw_cases
                WHERE kenmerk IN %(ks)s
                GROUP BY kenmerk
                """,
                parameters={"ks": list(candidates.keys())},
            ).named_results()
            seen = {r["kenmerk"]: r for r in rows}
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINS)

            for kenmerk, case in candidates.items():
                prior = seen.get(kenmerk)
                if prior is not None:
                    if prior["successes"] > 0:
                        continue  # already scraped successfully (refresh handles these)
                    if prior["attempts"] >= MAX_ATTEMPTS:
                        continue  # exhausted retries
                    last = prior["last_attempt"]
                    if last is not None and last > cutoff:
                        continue  # cooldown: last failure too recent
                out.append({"kenmerk": kenmerk, "court": case.court})
                queued.add(kenmerk)

            info("discovery candidates after anti-join", queueable=len(out),
                 already_done=len(candidates) - len(out), **self._log_kwargs())

        # --- 2. Refresh: stale, still-open company/eenmanszaak cases ---
        refresh = self.ch.query(
            """
            SELECT r.kenmerk AS kenmerk
            FROM (
                SELECT
                    kenmerk,
                    max(scraped_at)                       AS last_ok,
                    argMax(entity_type, scraped_at)       AS entity_type
                FROM insolventies.raw_cases
                WHERE status = 'ok'
                GROUP BY kenmerk
            ) AS r
            LEFT JOIN (
                SELECT kenmerk, countIf(event_type IN %(term)s) AS terminal_events
                FROM insolventies.processed_publications
                GROUP BY kenmerk
            ) AS p ON r.kenmerk = p.kenmerk
            WHERE r.entity_type IN ('company', 'eenmanszaak')
              AND coalesce(p.terminal_events, 0) = 0
              AND r.last_ok < now64(3, 'UTC') - INTERVAL %(days)s DAY
            ORDER BY r.last_ok ASC
            LIMIT %(n)s
            """,
            parameters={"term": list(TERMINAL_EVENTS), "days": REFRESH_DAYS, "n": REFRESH_BATCH},
        ).named_results()

        refreshed = 0
        for r in refresh:
            k = r["kenmerk"]
            if k in queued:
                continue  # already queued by discovery
            out.append({"kenmerk": k, "court": ""})
            queued.add(k)
            refreshed += 1

        info("refresh candidates", refreshed=refreshed,
             refresh_days=REFRESH_DAYS, refresh_cap=REFRESH_BATCH, **self._log_kwargs())
        return out

    def row_to_task(self, row):
        return {"kenmerk": row["kenmerk"], "court": row.get("court", "")}


class _NullWriter:
    """discover_all writes JSONL to a file object; the scheduler doesn't need it."""

    def write(self, _s):
        return None


if __name__ == "__main__":
    raise SystemExit(InsolventiesScheduler().run())
