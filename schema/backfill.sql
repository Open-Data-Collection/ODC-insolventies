-- Historical backfill via faillissementsverslagen.com (see scripts/backfill_fv.py).
--
-- fv_listing: one row per verslag listing row scraped from the fv.com index
-- (705k+ rows back to 2010). The listing alone carries the verslag kenmerk
-- (which encodes the registry insolventienummer), the KvK number, company
-- name, city, and publication date. Re-scrapes of a page collapse via
-- ReplacingMergeTree on verslag_kenmerk.

CREATE TABLE IF NOT EXISTS insolventies.fv_listing (
    verslag_kenmerk    String,               -- as listed, e.g. 10_rot_26_194_F_V_02_B or alk.09.164.F.V.01
    case_key           String,               -- verslag_kenmerk minus the verslag suffix
    insolventienummer  String,               -- derived registry number, e.g. F.10/26/194 ('' if underivable)
    kvk_nummer         String,
    verslag_datum      Nullable(Date),       -- 00-00-0000 rows -> NULL
    company_name       String,
    city               String,
    page               UInt32,               -- listing page (oldest-first, 100/page) it was seen on
    fetched_at         DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY verslag_kenmerk;

-- backfill_probe: one row per insolventienummer probed against the official
-- register (zoekOpKenmerk with the insolventienummer field, which bypasses
-- the ~1-month startDate window). status:
--   found      -> case still in the register; publicatiekenmerk captured and
--                 (unless --dry-run) pushed to insolventies:tasks for the
--                 normal worker to scrape
--   not_found  -> purged (ended >~6 months ago) or number derivation wrong
--   error      -> request failed after retries; re-probe by deleting the row
CREATE TABLE IF NOT EXISTS insolventies.backfill_probe (
    insolventienummer String,
    status            LowCardinality(String),
    aantal            UInt32,                -- aantalResultaten from the search
    publicatiekenmerk String,                -- first publication's kenmerk when found
    queued            UInt8,                 -- 1 = task pushed to insolventies:tasks
    error             String,
    probed_at         DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(probed_at)
ORDER BY insolventienummer;
