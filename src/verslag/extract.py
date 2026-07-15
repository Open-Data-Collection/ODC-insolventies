"""Orchestrator: turn a company's verslagen into one structured record.

Inputs (any may be None):
  first_narrative  - earliest openbaar verslag  (algemene gegevens: activiteiten)
  latest_narrative - most recent openbaar verslag (cumulative: most complete
                     omzet table + oorzaak, doorstart outcome, andere activa)
  latest_financieel- most recent Tussentijds financieel verslag (asset sale
                     prices, boedel, creditors, debt load)

Deterministic parsers do the numbers; E4B micro-prompts do the free-text
fields, each on a single Recofa section.

PRIVACY: no cleartext natural-person name leaves this module. Persons found
in 1.1 Directie en organisatie are returned as salted hashes (`hash_person`,
same ANONYMIZATION_SALT mechanism as src/privacy.py, so the same person hashes
identically across cases). Free-text fields are run through
llm.redact_persons; related-company names must be legal entities AND literally
present in the source section (llm.grounded) or they are dropped.
"""
from __future__ import annotations

import hashlib
import os
import re

from . import deterministic as D
from . import llm
from .sections import split_sections, clean


def hash_person(name: str) -> str:
    salt = os.environ.get("ANONYMIZATION_SALT", "")
    return hashlib.sha256((salt + name.strip().lower()).encode()).hexdigest()[:12]


def _pick(section_key, *texts):
    """First non-empty section_key across the given texts (already section-split dicts)."""
    for secs in texts:
        if secs and secs.get(section_key):
            return secs[section_key]
    return ""


_RELATIES = {"bestuurder", "aandeelhouder", "moeder", "dochter", "gelieerd"}

_BANKISH = re.compile(r"bank|rabo|abn|amro|bunq|knab|triodos|volksbank|factor|"
                      r"lease|financ|krediet|agricole", re.I)

_NOT_A_NAME = re.compile(r"(?i)natuurlijk|persoon|bestuurder|aandeelhouder|"
                         r"onbekend|gegevens|curator|privacy|anoniem|directie")


def _is_person_name(p: str) -> bool:
    """True for an actual name; False for anonymization placeholders like
    'een natuurlijk persoon' that would hash to a shared meaningless digest."""
    return len(p) >= 4 and not _NOT_A_NAME.search(p) and bool(re.search(r"[A-Z]", p))


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def extract_company(first_narrative=None, latest_narrative=None,
                    latest_financieel=None, use_llm=True, own_name=None):
    first = split_sections(clean(first_narrative)) if first_narrative else {}
    latest = split_sections(clean(latest_narrative)) if latest_narrative else {}
    fin = clean(latest_financieel) if latest_financieel else ""

    # latest verslag is cumulative -> most complete table; first fills gaps
    omzet = D.merge_omzet(D.parse_omzet(latest_narrative or ""),
                          D.parse_omzet(first_narrative or ""))

    # debt: narrative 8.x fills the components, but fin-verslag totals WIN
    # where present — the financieel verslag can be newer than the latest
    # narrative, and its H-table is kept current
    cred_section = _pick("crediteuren", latest, first)
    debt = D.parse_debt_sections(cred_section)
    concurrent_aantal = debt.pop("concurrent_aantal", None)
    debt.update(D.parse_debt(fin, ""))

    rec = {
        # --- deterministic (exact) ---
        "omzet_historie": omzet,
        "asset_realizations": D.parse_asset_realizations(fin),
        "boedel": D.parse_boedel(fin),
        "creditors": D.parse_creditors(fin),
        "debt": debt,
        "personeel": (D.parse_personeel(_pick("personeel", latest, first))
                      or D.parse_personeel(clean(latest_narrative or first_narrative or ""))),
        # --- llm (free text), filled below ---
        "activiteiten": None, "sector": None,
        "oorzaak": None, "oorzaak_categorie": None,
        "doorstart": None, "overnemer": None,
        "koopsom": None, "koopsom_toelichting": None,
        "domeinnamen": [], "ie_rechten": None,
        "bestuurders_gehasht": [], "relations": [],
        "personeel_ttv": None, "personeel_jaar_voor": None,
        "onroerend_goed": None, "huurpand": None,
        "boekhoudplicht_voldaan": None, "depot_jaarrekeningen_ok": None,
        "onbehoorlijk_bestuur": None, "paulianeus_handelen": None,
        "afwikkeling": "", "concurrent_aantal": concurrent_aantal,
    }
    if not use_llm:
        return rec

    if (s := llm.sector(_pick("activiteiten", first, latest))):
        rec["activiteiten"] = llm.redact_persons(s.get("activiteiten"))
        rec["sector"] = s.get("sector")

    # oorzaak from the LATEST verslag: early verslagen often say "vooralsnog
    # onbekend" while the final one states the established cause
    if (o := llm.oorzaak(_pick("oorzaak", latest, first))):
        rec["oorzaak"] = llm.redact_persons(o.get("oorzaak"))
        rec["oorzaak_categorie"] = o.get("categorie")

    doorstart_text = _pick("doorstart", latest, first)
    if (d := llm.doorstart(doorstart_text)):
        rec["doorstart"] = d.get("doorstart")
        overnemer = (d.get("overnemer") or "").strip().strip('"\'')
        # natural persons and the report's own anonymization placeholders
        # ("Koper", "de overnemer", ...) are not acquirer names
        if not overnemer or llm._PERSON_RE.search(overnemer) or overnemer.lower() in {
                "koper", "de koper", "overnemer", "de overnemer", "gegadigde",
                "de gegadigde", "doorstarter", "de doorstarter", "partij",
                "particulier", "onbekend", "n.v.t."}:
            overnemer = None
        rec["overnemer"] = overnemer
        if overnemer and llm.grounded(overnemer, doorstart_text):
            rec["relations"].append({"relatie": "overnemer", "company_name": overnemer,
                                     "detail": ""})
    if rec["doorstart"] and (k := llm.koopsom(doorstart_text)):
        rec["koopsom"] = k.get("koopsom")
        rec["koopsom_toelichting"] = llm.redact_persons(k.get("toelichting"))

    andere_activa_text = _pick("andere_activa", latest, first)
    if (a := llm.domeinnamen(andere_activa_text)):
        rec["domeinnamen"] = a.get("domeinnamen") or []
        ie = a.get("ie_rechten")
        # grounding: an IE claim must be anchored by an IE keyword that
        # actually occurs in the section (kills 'merkrechten' hallucinations)
        if ie:
            kws = re.findall(r"\b(merk|handelsna|octrooi|patent|auteursrecht|licentie)",
                             ie, re.I)
            if kws and not any(re.search(r"\b" + re.escape(kw), andere_activa_text, re.I)
                               for kw in kws):
                ie = None
        rec["ie_rechten"] = llm.redact_persons(ie)

    inv_text = _pick("inventarisatie", first, latest) or _pick("head", first, latest)
    known_persons: list[str] = []
    if (dir_ := llm.directie(inv_text)):
        # verslagen often anonymize directors ("een natuurlijk persoon, waarvan
        # de gegevens bij de curator bekend zijn") — hashing that placeholder
        # produced one identical meaningless digest across companies
        known_persons = [p.strip() for p in (dir_.get("personen") or [])
                         if p and p.strip() and _is_person_name(p)]
        rec["bestuurders_gehasht"] = sorted({hash_person(p) for p in known_persons})
        for b in dir_.get("bedrijven") or []:
            naam, relatie = (b.get("naam") or "").strip(), b.get("relatie") or "gelieerd"
            if (naam and relatie in _RELATIES and llm.looks_like_company(naam)
                    and llm.grounded(naam, inv_text)):
                rec["relations"].append({"relatie": relatie, "company_name": naam,
                                         "detail": ""})

    zekerheden_text = _pick("zekerheden", latest, first)
    if (bk := llm.bank(zekerheden_text)):
        for b in bk.get("banken") or []:
            naam = (b.get("naam") or "").strip()
            # must be an actual entity name, not a generic reference
            # ("de betrokken bank"), and grounded in the section text
            if (naam and llm.grounded(naam, zekerheden_text)
                    and not re.match(r"(?i)(de |een |betrokken |desbetreffende |huis)?bank$", naam)
                    and (llm.looks_like_company(naam) or _BANKISH.search(naam))):
                detail = f"vordering {b['vordering']:.0f}" if b.get("vordering") else ""
                rec["relations"].append({"relatie": "bank", "company_name": naam,
                                         "detail": detail})

    # cumulative section: the newest entries sit at the BOTTOM — feed the tail
    # so the head-clamp can't cut off the curator's final conclusions
    if (r := llm.rechtmatigheid(_pick("rechtmatigheid", latest, first)[-4400:])):
        for key in ("boekhoudplicht_voldaan", "depot_jaarrekeningen_ok",
                    "onbehoorlijk_bestuur", "paulianeus_handelen"):
            v = r.get(key)
            rec[key] = v if isinstance(v, bool) else None

    # personeel is deterministic-only: the LLM reliably grabs the layout's
    # verslag-number column instead of the headcount (read '1' for a 69-FTE firm)
    rec["personeel_ttv"] = rec["personeel"].get("personeel_ttv")
    rec["personeel_jaar_voor"] = rec["personeel"].get("personeel_jaar_voor")

    if (vg := llm.vastgoed(_pick("activa", latest, first),
                           inv_text)):
        for key in ("onroerend_goed", "huurpand"):
            v = vg.get(key)
            rec[key] = v if isinstance(v, bool) else None

    # 8.7 sits at the END of the crediteuren section — the head-clamp in
    # _call was cutting it off, so hand the LLM the 8.7-onward tail
    m87 = re.search(r"8\.7[\s\S]*", cred_section)
    if (af := llm.afwikkeling(m87.group(0)[:4000] if m87 else cred_section[-4000:])):
        w = af.get("afwikkeling")
        if w in ("gebrek_aan_baten", "vereenvoudigd", "uitdeling", "akkoord",
                 "voortgezet", "onbekend"):
            rec["afwikkeling"] = w
        if rec["concurrent_aantal"] is None:
            n = af.get("aantal_concurrente_crediteuren")
            if isinstance(n, int) and 0 <= n < 100000:
                rec["concurrent_aantal"] = n

    # dedupe relations (same company may surface in multiple sections) and
    # drop the debtor itself — compare punctuation-insensitively so
    # "Galahad Holding BV" still matches debtor "Galahad Holding B.V."
    own = _norm_name(own_name)
    seen, uniq = set(), []
    for rel in rec["relations"]:
        name_norm = _norm_name(rel["company_name"])
        key = (rel["relatie"], name_norm)
        if own and name_norm and (name_norm == own or name_norm in own or own in name_norm):
            continue
        if key not in seen:
            seen.add(key)
            uniq.append(rel)
    rec["relations"] = uniq

    # second PII net: the honorific regex can't catch bare names ("J. de
    # Vries"), but directie() told us this case's actual persons — scrub any
    # literal occurrence (full name or bare surname) from every free-text
    # field. Company-name fields are exempt by design.
    if known_persons:
        pats = []
        for p in known_persons:
            pats.append(re.escape(p))
            surname = re.sub(r"^(?:[A-Z]\.\s*)+", "", p).strip()  # drop initials
            if len(surname) > 3:
                pats.append(re.escape(surname))
        scrub = re.compile("|".join(sorted(pats, key=len, reverse=True)), re.I)
        for field in ("oorzaak", "activiteiten", "ie_rechten", "koopsom_toelichting"):
            if rec.get(field):
                rec[field] = scrub.sub("[persoon]", rec[field])
    return rec
