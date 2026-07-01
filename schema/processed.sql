-- Processed (queryable) layer for the insolventies pipeline.
--
-- The processor (src/processor.py) reads status='ok' rows from raw_cases,
-- parses the embedded record JSON, and fans it out into these three analytics
-- tables. These replace the legacy default.{insolventies,insolventie_publicaties,
-- insolventie_documenten} tables, now scoped to the per-project `insolventies` DB.
--
-- All ReplacingMergeTree keyed so re-processing a case overwrites cleanly.

CREATE DATABASE IF NOT EXISTS insolventies;

-- One row per case.
CREATE TABLE IF NOT EXISTS insolventies.processed_cases (
    kenmerk            String,
    insolventienummer  String,
    toezichtzaaknummer String,
    type               LowCardinality(String),           -- company | eenmanszaak | person
    court              LowCardinality(String),
    judge              String,
    is_anonymized      Bool,
    debtor_name        String,
    kvk_nummer         Nullable(String),
    city               Nullable(String),
    curator_names      Array(String),
    publication_count  UInt16,
    document_count     UInt16,
    scraped_at         DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(scraped_at)
PARTITION BY toYYYYMM(scraped_at)
ORDER BY (court, type, kenmerk)
SETTINGS date_time_input_format = 'best_effort';

-- One row per publication event per case.
CREATE TABLE IF NOT EXISTS insolventies.processed_publications (
    kenmerk            String,
    publicatie_kenmerk String,
    publicatie_datum   Nullable(Date),
    description        String,
    event_type         LowCardinality(String),
    event_subtype      LowCardinality(Nullable(String)),
    event_date         Nullable(Date),
    insolvency_type    LowCardinality(String),
    scraped_at         DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(scraped_at)
PARTITION BY toYYYYMM(scraped_at)
ORDER BY (kenmerk, publicatie_kenmerk)
SETTINGS date_time_input_format = 'best_effort';

-- One row per document / verslag.
CREATE TABLE IF NOT EXISTS insolventies.processed_documents (
    kenmerk         String,
    document_kenmerk String,
    document_date   Nullable(Date),
    document_type   LowCardinality(String),
    pdf_path        Nullable(String),
    scraped_at      DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(scraped_at)
PARTITION BY toYYYYMM(scraped_at)
ORDER BY (kenmerk, document_kenmerk)
SETTINGS date_time_input_format = 'best_effort';
