-- KvK registry snapshot per debtor (see src/processor.py enrichment step).
--
-- A bankrupt rechtspersoon stays in the Handelsregister until its liquidation
-- winds up, then gets deregistered — so registry data (SBI, activity text,
-- trade names) is only joinable while the case is fresh (2026 cohort: ~83%,
-- two-year-old cases: <10%). This table SNAPSHOTS that data at processing
-- time so the classification survives deregistration permanently.
--
-- Sources: kvk.businesses + kvk.sbi (Handelsregister postcode sweep; kvk.sbi
-- is the canonical home of legacy default.kvk_sbi). One row per kvk_nummer;
-- re-snapshots collapse via ReplacingMergeTree, and the enrichment INSERT
-- anti-joins so existing snapshots are never overwritten with emptier data.

CREATE TABLE IF NOT EXISTS insolventies.kvk_snapshot (
    kvk_nummer              String,
    sbi_hoofd               String,           -- reserved: source scrape lacks the
                                              -- main-activity flag (all-false), so
                                              -- currently always ''; use sbi_codes
    sbi_codes               Array(String),
    sbi_descriptions        Array(String),
    activiteit_omschrijving String,
    handelsnamen            Array(String),
    rechtsvorm              String,
    bron                    LowCardinality(String) DEFAULT 'kvk_scrape_2026',
    snapshot_at             DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(snapshot_at)
ORDER BY kvk_nummer;
