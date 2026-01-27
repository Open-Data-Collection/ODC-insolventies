from __future__ import annotations

import hashlib
import os

from src.models import InsolvencyRecord


def _hash_value(value: str, salt: str) -> str:
    return hashlib.sha256((salt + value).encode()).hexdigest()[:12]


def anonymize_record(record: InsolvencyRecord) -> InsolvencyRecord:
    """Anonymize personal data in-place and return the record.

    For 'person' type: anonymize everything (name, all addresses).
    For 'eenmanszaak' type: anonymize personal name and woon/correspondentie
    addresses, but keep trade names, vestigingsadressen, and KvK.
    """
    salt = os.environ.get("ANONYMIZATION_SALT", "")
    is_eenmanszaak = record.type == "eenmanszaak"

    record.is_anonymized = True

    # Hash the debtor name (always a personal name for person/eenmanszaak)
    if record.debtor.name:
        record.debtor.name = _hash_value(record.debtor.name, salt)

    if is_eenmanszaak:
        # Eenmanszaak: keep trade names as-is (they are business names)
        # Strip personal addresses (woon, correspondentie) but keep vestiging
        for addr in record.debtor.addresses:
            if addr.type in ("woon", "correspondentie"):
                addr.street = None
                addr.number = None
                addr.postcode = None
    else:
        # Pure person: hash non-company trade names, strip all addresses
        company_suffixes = ("b.v.", "n.v.", "v.o.f.", "c.v.", "b.v", "n.v", "v.o.f", "c.v")
        record.debtor.trade_names = [
            name if any(s in name.lower() for s in company_suffixes) else _hash_value(name, salt)
            for name in record.debtor.trade_names
        ]
        for addr in record.debtor.addresses:
            addr.street = None
            addr.number = None
            addr.postcode = None

    return record
