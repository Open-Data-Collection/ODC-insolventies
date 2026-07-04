"""Orchestrator: turn a company's verslagen into one structured record.

Inputs (any may be None):
  first_narrative  - earliest openbaar verslag  (algemene gegevens: omzet, activiteiten, oorzaak)
  latest_narrative - most recent openbaar verslag (doorstart outcome, andere activa/domeinen)
  latest_financieel- most recent Tussentijds financieel verslag (asset sale prices, boedel, creditors)

Deterministic parsers do the numbers; E4B micro-prompts do the free-text
fields, each on a single Recofa section.
"""
from __future__ import annotations

from . import deterministic as D
from . import llm
from .sections import split_sections, clean


def _pick(section_key, *texts):
    """First non-empty section_key across the given texts (already section-split dicts)."""
    for secs in texts:
        if secs and secs.get(section_key):
            return secs[section_key]
    return ""


def extract_company(first_narrative=None, latest_narrative=None,
                    latest_financieel=None, use_llm=True):
    first = split_sections(clean(first_narrative)) if first_narrative else {}
    latest = split_sections(clean(latest_narrative)) if latest_narrative else {}
    fin = clean(latest_financieel) if latest_financieel else ""

    rec = {
        # --- deterministic (exact) ---
        "omzet_historie": D.parse_omzet(first_narrative or "") or D.parse_omzet(latest_narrative or ""),
        "asset_realizations": D.parse_asset_realizations(fin),
        "boedel": D.parse_boedel(fin),
        "creditors": D.parse_creditors(fin),
        # --- llm (free text), filled below ---
        "activiteiten": None, "sector": None,
        "oorzaak": None, "oorzaak_categorie": None,
        "doorstart": None, "overnemer": None,
        "domeinnamen": [], "ie_rechten": None,
    }
    if not use_llm:
        return rec

    if (s := llm.sector(_pick("activiteiten", first, latest))):
        rec["activiteiten"] = s.get("activiteiten")
        rec["sector"] = s.get("sector")
    if (o := llm.oorzaak(_pick("oorzaak", first, latest))):
        rec["oorzaak"] = o.get("oorzaak")
        rec["oorzaak_categorie"] = o.get("categorie")
    if (d := llm.doorstart(_pick("doorstart", latest, first))):
        rec["doorstart"] = d.get("doorstart")
        rec["overnemer"] = d.get("overnemer")
    if (a := llm.domeinnamen(_pick("andere_activa", latest, first))):
        rec["domeinnamen"] = a.get("domeinnamen") or []
        rec["ie_rechten"] = a.get("ie_rechten")
    return rec
