-- Main case table (one row per case)
CREATE TABLE IF NOT EXISTS insolventies (
    kenmerk String,
    insolventienummer String,
    toezichtzaaknummer String,
    type LowCardinality(String),
    court LowCardinality(String),
    judge String,
    is_anonymized Bool,
    debtor_name String,
    kvk_nummer Nullable(String),
    city Nullable(String),
    curator_names Array(String),
    publication_count UInt16,
    document_count UInt16,
    scraped_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(scraped_at)
PARTITION BY toYYYYMM(scraped_at)
ORDER BY (court, type, kenmerk);

-- Publication events table (one row per publication per case)
CREATE TABLE IF NOT EXISTS insolventie_publicaties (
    kenmerk String,
    publicatie_kenmerk String,
    publicatie_datum Date,
    description String,
    event_type LowCardinality(String),
    event_subtype LowCardinality(Nullable(String)),
    event_date Nullable(Date),
    insolvency_type LowCardinality(String),
    scraped_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(scraped_at)
PARTITION BY toYYYYMM(publicatie_datum)
ORDER BY (publicatie_kenmerk);

-- Documents table (one row per document/verslag)
CREATE TABLE IF NOT EXISTS insolventie_documenten (
    kenmerk String,
    document_kenmerk String,
    document_date Date,
    document_type LowCardinality(String),
    pdf_path Nullable(String),
    scraped_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(scraped_at)
PARTITION BY toYYYYMM(document_date)
ORDER BY (document_kenmerk);
