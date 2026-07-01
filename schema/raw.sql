-- Raw layer for the insolventies pipeline.
--
-- The worker writes one row per scrape attempt of a kenmerk (success or
-- failure). This is the worker→processor handoff and the source of truth for
-- the scheduler's retry/dedup logic (see src/scheduler.py). The full parsed
-- record is carried as a JSON string in `record` so the processor can fan it
-- out into the processed_* tables without re-fetching anything.
--
-- Plain MergeTree (one row per attempt, like the fleet's raw_responses) — the
-- processor checkpoints on scraped_at and only reads status='ok' rows.

CREATE DATABASE IF NOT EXISTS insolventies;

CREATE TABLE IF NOT EXISTS insolventies.raw_cases (
    kenmerk       String,
    scraped_at    DateTime64(3, 'UTC') DEFAULT now64(3),
    status        LowCardinality(String),          -- 'ok' | 'fetch_failed' | 'parse_failed'
    entity_type   LowCardinality(String),          -- company | eenmanszaak | person | ''
    error         String DEFAULT '',
    record        String DEFAULT '',               -- full record.to_dict() as JSON ('' on failure)
    git_sha       LowCardinality(String) DEFAULT ''
) ENGINE = MergeTree
PARTITION BY toYYYYMM(scraped_at)
ORDER BY (scraped_at, kenmerk)
SETTINGS date_time_input_format = 'best_effort';
