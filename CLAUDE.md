# ODC Insolventies

Scraper for Dutch insolvency records from `insolventies.rechtspraak.nl`.

## Architecture

- **Phase 1 (Discovery)**: Searches all courts for new insolvency publications via REST API, pushes kenmerks to Redis task queue
- **Phase 2 (Scraping)**: Fetches case details and PDFs for discovered kenmerks, uploads to MinIO and ClickHouse

## Key Design Decisions

- API client handles CSRF tokens automatically (fetched from homepage HTML)
- Three entity types: `company` (B.V./N.V. with KvK on persoon), `eenmanszaak` (natural person with KvK on trade names), `person` (no KvK)
- Natural person and eenmanszaak records are anonymized (SHA-256 hashed names, stripped personal addresses) before storage
- Eenmanszaak keeps trade names, vestigingsadressen, and KvK (business data is public)
- Publication descriptions are parsed into structured event_type/event_subtype fields
- MinIO for raw JSON + PDFs, ClickHouse for queryable analytics tables

## Task Queue

This project uses odc-lib for infrastructure access. See ODC-scraping-infra/CLAUDE.md for the full pattern.

```
pip install odc-lib @ git+https://github.com/open-data-collection/ODC-scraping-infra.git#subdirectory=lib/odc-lib
```

- Discovery phase pushes kenmerks to `insolventies:tasks` Redis queue via `OdcClient.push_tasks()`
- Scrape phase reads kenmerks from queue (or from --input file / --kenmerk flag)
- Raw records go to Storage MinIO via `OdcClient.dump_raw_data()`
- Flattened records go to ClickHouse via `OdcClient.file_to_clickhouse()`
- PDFs uploaded directly to `raw-data/insolventies/pdfs/` via S3

## Running

```bash
python -m src.main --phase discover              # Find new cases, push to queue
python -m src.main --phase scrape --input file    # Scrape from discovery file
python -m src.main --phase scrape --kenmerk X     # Scrape single case
python -m src.main --phase all                    # Full pipeline
python -m src.main --phase discover --no-upload   # Discovery without queue/storage
```

## Environment Variables

- `REDIS_URL` — Redis connection (for task queue)
- `TASKS_MINIO_ENDPOINT`, `TASKS_MINIO_ACCESS_KEY`, `TASKS_MINIO_SECRET_KEY` — Tasks MinIO
- `STORAGE_MINIO_ENDPOINT`, `STORAGE_MINIO_ACCESS_KEY`, `STORAGE_MINIO_SECRET_KEY` — Storage MinIO
- `ANONYMIZATION_SALT` — Salt for hashing personal names
- `REQUEST_DELAY` — Delay between API requests in seconds (default: 1.0)
