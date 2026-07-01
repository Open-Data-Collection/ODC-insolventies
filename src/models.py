from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}

DATE_PATTERN = re.compile(r"op (\d{1,2}) (\w+) (\d{4})")

EVENT_TYPE_KEYWORDS = {
    "uitspraak": "uitspraak",
    "opheffing": "opheffing",
    "einde": "einde",
    "beëindiging": "einde",
    "beeindiging": "einde",
    "vernietiging": "vernietiging",
    "vervanging": "vervanging",
    "neerlegging slotuitdelingslijst": "neerlegging_slotuitdelingslijst",
    "neerlegging tussentijdse uitdelingslijst": "neerlegging_tussentijdse_uitdelingslijst",
    "vereenvoudigde afwikkeling": "vereenvoudigde_afwikkeling",
    "zitting": "zitting",
    "rectificatie": "rectificatie",
    "overdracht": "overdracht",
    "overgedragen": "overdracht",
    "rekening en verantwoording": "rekening_en_verantwoording",
}

EVENT_SUBTYPE_KEYWORDS = {
    "gebrek aan baten": "gebrek_aan_baten",
    "verbindende uitdelingslijst": "verbindende_uitdelingslijst",
    "schone lei": "schone_lei",
    "nihil uitdeling": "nihil_uitdeling_schone_lei",
    "omzetting": "omzetting_faillissement",
    "hoger beroep": "hoger_beroep",
    "verzet": "verzet",
    "na surseance": "na_surseance",
    "faillietverklaring": "faillietverklaring",
    "na voorlopige surseance": "na_surseance",
}

INSOLVENCY_TYPE_KEYWORDS = {
    "faillissement": "faillissement",
    "schuldsanering": "schuldsanering",
    "surseance": "surseance",
    "surséance": "surseance",
}


def parse_dutch_date(text: str) -> Optional[date]:
    m = DATE_PATTERN.search(text)
    if not m:
        return None
    day, month_str, year = m.groups()
    month = DUTCH_MONTHS.get(month_str.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def classify_event_type(description: str) -> str:
    desc_lower = description.lower()
    # Check multi-word patterns first (longest match)
    for keyword, event_type in sorted(EVENT_TYPE_KEYWORDS.items(), key=lambda x: -len(x[0])):
        if keyword in desc_lower:
            return event_type
    return "unknown"


def classify_event_subtype(description: str) -> Optional[str]:
    desc_lower = description.lower()
    for keyword, subtype in sorted(EVENT_SUBTYPE_KEYWORDS.items(), key=lambda x: -len(x[0])):
        if keyword in desc_lower:
            return subtype
    return None


def classify_insolvency_type(description: str) -> str:
    desc_lower = description.lower()
    for keyword, ins_type in INSOLVENCY_TYPE_KEYWORDS.items():
        if keyword in desc_lower:
            return ins_type
    return "unknown"


@dataclass
class Address:
    type: str  # vestiging, woon, correspondentie
    street: Optional[str] = None
    number: Optional[str] = None
    postcode: Optional[str] = None
    city: Optional[str] = None


@dataclass
class Debtor:
    name: str
    kvk_nummer: Optional[str] = None
    trade_names: list[str] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)


@dataclass
class Curator:
    name: str
    address: Optional[str] = None
    phone: Optional[str] = None


@dataclass
class Publication:
    date: Optional[str]  # ISO date string
    kenmerk: str
    description: str
    event_type: str
    event_subtype: Optional[str]
    event_date: Optional[str]  # ISO date string
    insolvency_type: str

    @classmethod
    def from_raw(cls, pub_date: Optional[str], kenmerk: str, description: str) -> Publication:
        event_date = parse_dutch_date(description)
        return cls(
            date=pub_date,
            kenmerk=kenmerk,
            description=description,
            event_type=classify_event_type(description),
            event_subtype=classify_event_subtype(description),
            event_date=event_date.isoformat() if event_date else None,
            insolvency_type=classify_insolvency_type(description),
        )


@dataclass
class Document:
    kenmerk: str  # underscored format
    date: Optional[str]
    type: str
    pdf_path: Optional[str] = None  # storage MinIO key, set by the worker after upload


@dataclass
class InsolvencyRecord:
    kenmerk: str
    insolventienummer: str
    toezichtzaaknummer: str
    type: str  # "company" or "person"
    court: str
    judge: str
    is_anonymized: bool
    debtor: Debtor
    curators: list[Curator] = field(default_factory=list)
    publications: list[Publication] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")

    def to_dict(self) -> dict:
        return asdict(self)
