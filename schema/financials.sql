-- Extracted financial + firmographic layer (verslag extraction, src/verslag/).
-- Populated by the extraction runner from the latest/first verslagen per company.

CREATE DATABASE IF NOT EXISTS insolventies;

-- One row per company (case kenmerk): firmographics + qualitative + boedel summary.
CREATE TABLE IF NOT EXISTS insolventies.processed_profile (
    kenmerk                    String,
    insolventienummer          String,
    kvk_nummer                 Nullable(String),
    sector                     LowCardinality(String),
    activiteiten               String,
    oorzaak                    String,
    oorzaak_categorie          LowCardinality(String),
    doorstart                  Nullable(Bool),
    overnemer                  Nullable(String),        -- acquirer company (doorstart) -> a lead
    domeinnamen                Array(String),           -- feeds the domain-value cross-ref
    ie_rechten                 Nullable(String),
    saldo_boedelrekening       Nullable(Float64),
    definitief_saldo           Nullable(Float64),
    beschikbaar_voor_uitdeling Nullable(Float64),
    pref_recovery_pct          Nullable(Float64),
    conc_recovery_pct          Nullable(Float64),
    model                      LowCardinality(String) DEFAULT '',
    extracted_at               DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(extracted_at)
ORDER BY kenmerk
SETTINGS date_time_input_format = 'best_effort';

-- Pre-bankruptcy revenue/P&L/balance history: one row per company per year.
CREATE TABLE IF NOT EXISTS insolventies.processed_revenue (
    kenmerk       String,
    jaar          UInt16,
    omzet         Nullable(Float64),
    winst_verlies Nullable(Float64),
    balanstotaal  Nullable(Float64),
    extracted_at  DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(extracted_at)
ORDER BY (kenmerk, jaar)
SETTINGS date_time_input_format = 'best_effort';

-- What each asset category was realized (sold) for: one row per company per category.
CREATE TABLE IF NOT EXISTS insolventies.processed_asset_sales (
    kenmerk          String,
    categorie        LowCardinality(String),
    verkoopopbrengst Float64,
    extracted_at     DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(extracted_at)
ORDER BY (kenmerk, categorie)
SETTINGS date_time_input_format = 'best_effort';
