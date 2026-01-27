from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.api import ApiClient
from src.models import (
    Address, Curator, Debtor, Document, InsolvencyRecord, Publication,
)
from src.privacy import anonymize_record
from src.storage import upload_record, upload_pdf

logger = logging.getLogger(__name__)

# .NET date format: /Date(1769382000000+0100)/
_DOTNET_DATE_RE = re.compile(r"/Date\((\d+)([+-]\d{4})?\)/")


def _parse_dotnet_date(value: Optional[str]) -> Optional[str]:
    """Parse .NET /Date(...)/ format to ISO date string."""
    if not value:
        return None
    m = _DOTNET_DATE_RE.search(value)
    if not m:
        return None
    ts_ms = int(m.group(1))
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _extract_addresses(data: dict, addr_type: str, key: str) -> list[Address]:
    addresses = []
    for addr in data.get(key, []) or []:
        number = addr.get("huisnummer", "")
        suffix1 = addr.get("huisnummerToevoeging1") or ""
        suffix2 = addr.get("huisnummerToevoeging2") or ""
        full_number = " ".join(filter(None, [number, suffix1, suffix2])).strip() or None
        addresses.append(Address(
            type=addr_type,
            street=addr.get("straat"),
            number=full_number,
            postcode=addr.get("postcode"),
            city=addr.get("plaats"),
        ))
    return addresses


def _format_curator_address(adres: Optional[dict]) -> Optional[str]:
    if not adres:
        return None
    parts = []
    street = adres.get("straat", "")
    number = adres.get("huisnummer", "")
    if street:
        parts.append(f"{street} {number}".strip())
    postcode = adres.get("postcode", "")
    city = adres.get("plaats", "")
    if postcode or city:
        parts.append(f"{postcode} {city}".strip())
    return ", ".join(parts) if parts else None


def _parse_case(raw: dict) -> InsolvencyRecord:
    """Parse raw API response into an InsolvencyRecord."""
    # Response is wrapped in {model: {...}, status: N}
    model = raw.get("model", raw) if isinstance(raw, dict) else raw

    persoon = model.get("persoon", {}) or {}
    persoon_kvk = persoon.get("KvKNummer") or persoon.get("kvkNummer")
    has_personal_info = bool(persoon.get("voornaam") or persoon.get("geboortedatum"))

    # Classify entity type:
    # - "company": rechtspersoon (B.V., N.V., etc.) — KvK on persoon, no personal info
    # - "eenmanszaak": natural person with trade name — personal info present, KvK on handelsnamen
    # - "person": pure natural person — no KvK anywhere
    kvk = persoon_kvk

    # Build name — always use full personal name if available
    if persoon_kvk and not has_personal_info:
        # True company: achternaam is the company name
        name = persoon.get("achternaam", "")
    else:
        # Natural person (eenmanszaak or pure person)
        parts = [persoon.get("voornaam", ""), persoon.get("voorvoegsel", ""), persoon.get("achternaam", "")]
        name = " ".join(p for p in parts if p).strip() or persoon.get("achternaam", "")

    # Addresses from model level
    addresses = []
    addresses.extend(_extract_addresses(model, "vestiging", "vestigingsadressen"))
    addresses.extend(_extract_addresses(model, "woon", "woonadressen"))
    addresses.extend(_extract_addresses(model, "correspondentie", "correspondentieadressen"))

    # Trade names (with their own addresses and KvK numbers)
    trade_names = []
    for hn in model.get("handelendOnderDeNamen", []) or []:
        if isinstance(hn, dict):
            trade_names.append(hn.get("handelsnaam", ""))
            # Also capture addresses from trade names
            addresses.extend(_extract_addresses(hn, "vestiging", "vestigingsadressen"))
            addresses.extend(_extract_addresses(hn, "correspondentie", "correspondentieadressen"))
            # Use KvK from trade name if not on persoon
            if not kvk:
                kvk = hn.get("KvKNummer")
        elif isinstance(hn, str):
            trade_names.append(hn)

    debtor = Debtor(
        name=name,
        kvk_nummer=kvk,
        trade_names=trade_names,
        addresses=addresses,
    )

    # Curators (active)
    curators = []
    for cur in model.get("curators", []) or []:
        title = cur.get("titulatuur", "")
        initials = cur.get("voorletters", "")
        prefix = cur.get("voorvoegsel", "") or ""
        surname = cur.get("achternaam", "")
        full_name = " ".join(filter(None, [title, initials, prefix, surname])).strip()
        curators.append(Curator(
            name=full_name,
            address=_format_curator_address(cur.get("adres")),
            phone=(cur.get("adres") or {}).get("telefoonnummer"),
        ))

    # Bewindvoerders (for schuldsanering) — treat same as curators
    for bw in model.get("bewindvoerders", []) or []:
        title = bw.get("titulatuur", "")
        initials = bw.get("voorletters", "")
        prefix = bw.get("voorvoegsel", "") or ""
        surname = bw.get("achternaam", "")
        full_name = " ".join(filter(None, [title, initials, prefix, surname])).strip()
        curators.append(Curator(
            name=full_name,
            address=_format_curator_address(bw.get("adres")),
            phone=(bw.get("adres") or {}).get("telefoonnummer"),
        ))

    # Publications
    publications = []
    for pub in model.get("publicatiegeschiedenis", []) or []:
        pub_date = _parse_dotnet_date(pub.get("publicatieDatum"))
        pub_kenmerk = pub.get("publicatieKenmerk", "")
        description = pub.get("publicatieOmschrijving", "")
        publications.append(Publication.from_raw(pub_date, pub_kenmerk, description))

    # Determine type
    if persoon_kvk and not has_personal_info:
        entity_type = "company"
    elif has_personal_info:
        # Natural person — could be eenmanszaak if KvK exists on trade names
        entity_type = "eenmanszaak" if kvk else "person"
    elif kvk:
        # KvK found on trade names but no personal info — treat as company
        entity_type = "company"
    else:
        entity_type = "person"

    # Get the kenmerk from the most recent publication or model
    kenmerk = ""
    if publications:
        kenmerk = publications[0].kenmerk
    # The API doesn't have a top-level kenmerk field — it comes from the query

    return InsolvencyRecord(
        kenmerk=kenmerk,
        insolventienummer=model.get("landelijkUniekZaaknummer", ""),
        toezichtzaaknummer=model.get("toezichtZaaknummer", ""),
        type=entity_type,
        court=model.get("behandelendeInstantieNaam", ""),
        judge=model.get("RC", "") or "",
        is_anonymized=False,
        debtor=debtor,
        curators=curators,
        publications=publications,
    )


def scrape_case(
    client: ApiClient,
    kenmerk: str,
    upload: bool = True,
) -> Optional[InsolvencyRecord]:
    """Fetch and process a single case by kenmerk."""
    try:
        raw = client.get_case(kenmerk)
    except Exception:
        logger.exception("Failed to fetch case %s", kenmerk)
        return None

    record = _parse_case(raw)
    # Set the query kenmerk (may differ from the latest publication kenmerk)
    if not record.kenmerk:
        record.kenmerk = kenmerk

    # Fetch reports/documents for company and eenmanszaak cases
    if record.type in ("company", "eenmanszaak"):
        try:
            reports = client.get_reports(kenmerk)
            if isinstance(reports, dict):
                reports = reports.get("model", reports)
                if isinstance(reports, dict):
                    reports = reports.get("items", [])
            if isinstance(reports, list):
                for report in reports:
                    doc_kenmerk = report.get("kenmerk", "").replace(".", "_")
                    doc_date = report.get("datum") or _parse_dotnet_date(report.get("datumGoedkeuring"))
                    doc_type = report.get("type", "Verslag")
                    if doc_kenmerk:
                        record.documents.append(Document(kenmerk=doc_kenmerk, date=doc_date, type=doc_type))
        except Exception:
            logger.warning("Failed to fetch reports for %s", kenmerk, exc_info=True)

    # Anonymize natural persons and eenmanszaken
    if record.type in ("person", "eenmanszaak"):
        anonymize_record(record)

    if upload:
        try:
            upload_record(record.to_dict(), kenmerk)
        except Exception:
            logger.exception("Failed to upload record for %s", kenmerk)

        # Download and upload PDFs for company and eenmanszaak cases
        if record.type in ("company", "eenmanszaak"):
            for doc in record.documents:
                try:
                    pdf_data = client.download_pdf(doc.kenmerk)
                    upload_pdf(pdf_data, doc.kenmerk)
                except Exception:
                    logger.warning("Failed to download/upload PDF %s", doc.kenmerk, exc_info=True)

    return record


def scrape_from_file(
    client: ApiClient,
    input_path: str,
    upload: bool = True,
) -> list[InsolvencyRecord]:
    """Scrape all kenmerks from a discovery JSONL file."""
    import json

    records = []
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            kenmerk = entry["kenmerk"]
            record = scrape_case(client, kenmerk, upload=upload)
            if record:
                records.append(record)

    logger.info("Scraped %d cases from %s", len(records), input_path)
    return records
