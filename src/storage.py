from __future__ import annotations

import json
import logging
import os
from datetime import date
from io import BytesIO
from typing import Optional

from odc import OdcClient

logger = logging.getLogger(__name__)

PROJECT = "insolventies"

_odc_client: Optional[OdcClient] = None


def get_odc_client() -> OdcClient:
    global _odc_client
    if _odc_client is None:
        _odc_client = OdcClient(PROJECT)
    return _odc_client


def upload_record(record: dict, kenmerk: str):
    """Upload a case record as raw data to MinIO via odc-lib."""
    client = get_odc_client()
    obj = client.dump_raw_data([record], subfolder="records", compress=False)
    logger.info("Uploaded record for %s to %s", kenmerk, obj)
    return obj


def upload_records_to_clickhouse(records: list[dict]):
    """Upload flattened records for ClickHouse ingestion via S3Queue."""
    if not records:
        return
    client = get_odc_client()
    obj = client.file_to_clickhouse(records)
    logger.info("Uploaded %d records to ClickHouse via %s", len(records), obj)
    return obj


def upload_pdf(pdf_bytes: bytes, kenmerk: str):
    """Upload a PDF to MinIO. Uses raw S3 client since odc-lib doesn't handle binary."""
    client = get_odc_client()
    s3 = client._storage_s3  # access underlying S3 client
    bucket = client._config.raw_data_bucket
    kenmerk_underscored = kenmerk.replace(".", "_")
    key = f"{PROJECT}/pdfs/{kenmerk_underscored}.pdf"
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=BytesIO(pdf_bytes), ContentLength=len(pdf_bytes),
        ContentType="application/pdf",
    )
    logger.info("Uploaded PDF to %s", key)
    return key


def push_scrape_tasks(kenmerks: list[str]):
    """Push discovered kenmerks as tasks to the Redis queue."""
    if not kenmerks:
        return
    client = get_odc_client()
    tasks = [{"kenmerk": k} for k in kenmerks]
    count = client.push_tasks(tasks)
    logger.info("Pushed %d scrape tasks to queue", count)
    return count


def push_scrape_tasks_file(kenmerks: list[str]):
    """Push discovered kenmerks as a task file to MinIO inbox (for large batches)."""
    if not kenmerks:
        return
    client = get_odc_client()
    obj = client.push_task_file(kenmerks)
    logger.info("Pushed task file with %d kenmerks to %s", len(kenmerks), obj)
    return obj
