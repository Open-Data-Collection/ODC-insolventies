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
    -- v2 (2026-07): firmographics + doorstart + governance. PRIVACY RULE:
    -- no cleartext natural-person names in ANY column; persons appear only as
    -- salted hashes (bestuurders_gehasht, ANONYMIZATION_SALT — same mechanism
    -- as src/privacy.py, stable across cases for linkage). Company names are
    -- required data and stored verbatim.
    koopsom                    Nullable(Float64),        -- agreed doorstart purchase price
    koopsom_toelichting        Nullable(String),
    personeel_ttv              Nullable(UInt32),         -- headcount at bankruptcy
    personeel_jaar_voor        Nullable(UInt32),         -- headcount year before
    bestuurders_gehasht        Array(String),            -- salted hashes of natural-person directors
    onroerend_goed             Nullable(Bool),           -- owned real estate present
    huurpand                   Nullable(Bool),           -- rented premises
    boekhoudplicht_voldaan     Nullable(Bool),           -- 7.1
    depot_jaarrekeningen_ok    Nullable(Bool),           -- 7.2 filings on time
    onbehoorlijk_bestuur       Nullable(Bool),           -- 7.5 (curator-asserted)
    paulianeus_handelen        Nullable(Bool),           -- 7.6
    afwikkeling                LowCardinality(String) DEFAULT '',  -- expected settlement mode
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

-- Debt load (crediteuren, sections 8.x + financieel verslag G): one row per company.
CREATE TABLE IF NOT EXISTS insolventies.processed_debt (
    kenmerk           String,
    boedelvorderingen Nullable(Float64),
    pref_fiscus       Nullable(Float64),
    pref_uwv          Nullable(Float64),
    pref_overig       Nullable(Float64),
    pref_totaal       Nullable(Float64),
    concurrent_bedrag Nullable(Float64),
    concurrent_aantal Nullable(UInt32),
    extracted_at      DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(extracted_at)
ORDER BY kenmerk
SETTINGS date_time_input_format = 'best_effort';

-- Company-to-company graph around an insolvency: one row per related LEGAL
-- ENTITY. Natural persons are NEVER rows here (they hash into
-- processed_profile.bestuurders_gehasht instead). Company names must be
-- grounded: the extractor verifies the name literally appears in the source
-- section before writing.
CREATE TABLE IF NOT EXISTS insolventies.processed_relations (
    kenmerk      String,
    relatie      LowCardinality(String),   -- bestuurder | aandeelhouder | moeder | dochter | gelieerd | overnemer | bank
    company_name String,
    detail       String DEFAULT '',        -- e.g. bank claim amount, share pct
    extracted_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(extracted_at)
ORDER BY (kenmerk, relatie, company_name)
SETTINGS date_time_input_format = 'best_effort';
