# ODC Insolventies

Scraper for Dutch insolvency records from `insolventies.rechtspraak.nl`.

Built on the ODC three-component pipeline pattern (see ODC-scraping-infra
`docs/SPEC-pipeline-components.md`). One Docker image, three modes.

## Architecture

- **Scheduler** (`src/scheduler.py`, Nomad batch-periodic on `services`, daily
  `0 6 * * *`): discovery runs here — searches every court for recent
  publications, anti-joins the discovered kenmerks against
  `insolventies.raw_cases` (skip already-scraped, honour cooldown/max-attempts
  for failures), and pushes `{kenmerk}` tasks to the `insolventies:tasks` Redis
  queue.
- **Worker** (`src/worker.py`, Nomad service on `processing`): drains
  `insolventies:tasks`, scrapes each case (`src/scrape.py:build_record`),
  downloads + uploads report PDFs to storage MinIO, and writes ONE row per
  attempt to `insolventies.raw_cases` (status ok/fetch_failed/parse_failed,
  full record JSON on success).
- **Processor** (`src/processor.py`, Nomad batch-periodic on `storage`, every
  30 min): reads `status='ok'` rows from `raw_cases`, parses the record JSON,
  and fans out into `processed_cases` / `processed_publications` /
  `processed_documents`. Checkpoints on `scraped_at`.

## Key Design Decisions

- API client handles CSRF tokens automatically (fetched from homepage HTML).
  `search()` self-corrects the `startDate` from the API's validation message —
  the site enforces a ~one-calendar-month lower bound and rejects earlier dates.
- Three entity types: `company` (B.V./N.V. with KvK on persoon), `eenmanszaak`
  (natural person with KvK on trade names), `person` (no KvK).
- Natural person and eenmanszaak records are anonymized (SHA-256 hashed names,
  stripped personal addresses) before storage. `ANONYMIZATION_SALT` must stay
  constant so hashes remain stable across runs.
- Eenmanszaak keeps trade names, vestigingsadressen, and KvK (business data is public).
- Publication descriptions are parsed into structured event_type/event_subtype fields.
- Retry policy lives entirely in the scheduler's anti-join SQL — the worker is
  dumb about retries (writes one raw_cases row per attempt).
- All CH tables live in the per-project `insolventies` database (the legacy
  `default.insolventies*` tables are superseded).

## ClickHouse tables (`insolventies` database)

| Table | Written by | Shape |
|-------|-----------|-------|
| `raw_cases` | worker | one row per scrape attempt; full record as JSON string |
| `processed_cases` | processor | one row per case |
| `processed_publications` | processor | one row per publication event |
| `processed_documents` | processor | one row per verslag (with `pdf_path`) |

Apply schemas: `schema/raw.sql`, `schema/processed.sql` (comment-aware split via
`OdcClient.ensure_ch_schema()` or `clickhouse-client < schema/*.sql`).

## Running locally

```bash
# odc dev stack (from ODC-scraping-infra): odc dev up -d
odc dev run -- python -m src.scheduler          # discovery → insolventies:tasks
odc dev run -- python -m src.worker             # drain queue → raw_cases
odc dev run -- python -m src.processor          # raw_cases → processed_*
python -m src.scheduler --dry-run               # discover + count, no push
python -m src.processor --dry-run               # fetch + transform, no write
```

## Deploy (Nomad)

```bash
odc deploy nomad/scheduler.nomad.hcl
odc deploy nomad/worker.nomad.hcl
odc deploy nomad/processor.nomad.hcl
# each job id needs its own Variable ACL policy the first time:
scripts/grant-job-secrets.py insolventies-scheduler   # (and -worker, -processor)
```

## Environment Variables

- `REDIS_URL` — Redis (task queue), with auth
- `CLICKHOUSE_HOST` / `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` — ClickHouse
- `STORAGE_MINIO_ENDPOINT` / `STORAGE_MINIO_ACCESS_KEY` / `STORAGE_MINIO_SECRET_KEY` — Storage MinIO (PDFs)
- `ANONYMIZATION_SALT` — salt for hashing personal names (keep constant)
- `REQUEST_DELAY` — delay between API requests in seconds (default: 1.0)
