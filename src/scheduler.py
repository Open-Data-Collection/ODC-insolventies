"""Scheduler for insolventies.

Discovery runs here (not against an upstream CH table): each run searches every
court on insolventies.rechtspraak.nl for recent publications, then anti-joins
the discovered kenmerks against `insolventies.raw_cases` so we only queue cases
that haven't been scraped successfully yet (with a cooldown + max-attempts cap
for failures). Queued kenmerks land on `insolventies:tasks` for the workers.

Deployed as a Nomad batch-periodic job on odc-services (next to Redis).

Note (v1): a kenmerk that has been scraped successfully once is never
re-queued. Each publication event has its own kenmerk, so new events surface as
new discoveries — but later-added verslagen on an already-scraped case are not
re-fetched. A periodic full-refresh cadence is a possible follow-up.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from odc.scheduler import Scheduler
from odc.logging import info

from src.api import ApiClient
from src.discovery import discover_all

MAX_ATTEMPTS = 3
COOLDOWN_MINS = 60


class InsolventiesScheduler(Scheduler):
    name = "insolventies-scheduler"
    target_queue = "insolventies:tasks"
    backpressure_threshold = 100_000

    def fetch_rows(self):
        # 1. Discover candidate kenmerks across all courts (API, rate-limited).
        api = ApiClient()
        cases = discover_all(api, output=_NullWriter())
        candidates = {c.kenmerk: c for c in cases}
        info("discovery done", discovered=len(candidates), **self._log_kwargs())
        if not candidates:
            return []

        # 2. Anti-join against raw_cases: skip already-succeeded, honour
        #    cooldown + max-attempts for previously-failed kenmerks.
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

        out = []
        for kenmerk, case in candidates.items():
            prior = seen.get(kenmerk)
            if prior is not None:
                if prior["successes"] > 0:
                    continue  # already scraped successfully
                if prior["attempts"] >= MAX_ATTEMPTS:
                    continue  # exhausted retries
                # cooldown: skip if the last failure is too recent
                last = prior["last_attempt"]
                if last is not None and last > cutoff:
                    continue
            out.append({"kenmerk": kenmerk, "court": case.court})

        info("candidates after anti-join", queueable=len(out),
             already_done=len(candidates) - len(out), **self._log_kwargs())
        return out

    def row_to_task(self, row):
        return {"kenmerk": row["kenmerk"], "court": row.get("court", "")}


class _NullWriter:
    """discover_all writes JSONL to a file object; the scheduler doesn't need it."""

    def write(self, _s):
        return None


if __name__ == "__main__":
    raise SystemExit(InsolventiesScheduler().run())
