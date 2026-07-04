"""Deterministic (regex / column-position) extractors — no LLM, exact.

  parse_omzet(narrative_text)          -> [{jaar, omzet, winst_verlies, balanstotaal}]
  parse_asset_realizations(fin_text)   -> {category: euro}   (what each sold for)
  parse_boedel(fin_text)               -> {saldo_boedelrekening, definitief_saldo, ...}
  parse_creditors(fin_text)            -> {preferente:{...}, concurrente:{...}}
"""
from __future__ import annotations

import re

EUR = r"€\s*-?\s*[\d.]+,\d{2}"


def parse_euro(s):
    if not s:
        return None
    s = s.replace("€", "").replace(" ", "").strip()
    if not s or s in ("-", "--"):
        return None
    neg = s.startswith("-")
    s = s.lstrip("-").replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def parse_omzet(text):
    """Revenue/P&L/balance history table (column-position aware so empty cells
    don't misalign). Lives in the first verslag's 'Financiële gegevens'."""
    rows, lines, hdr = [], text.splitlines(), None
    for i, ln in enumerate(lines):
        if re.search(r"Jaar\s+Omzet", ln) and "Balanstotaal" in ln:
            hdr = i
            break
    if hdr is None:
        return rows
    h = lines[hdr]
    b1 = (h.index("Winst") + h.index("Omzet")) // 2
    b2 = (h.index("Balanstotaal") + h.index("Winst")) // 2
    for ln in lines[hdr + 1: hdr + 14]:
        m = re.match(r"\s*(19|20)(\d{2})\b", ln)
        if not m:
            if rows and not ln.strip():
                continue
            if rows:
                break
            continue
        row = {"jaar": int(m.group(1) + m.group(2)), "omzet": None,
               "winst_verlies": None, "balanstotaal": None}
        for cm in re.finditer(EUR, ln):
            v = parse_euro(cm.group(0))
            row["omzet" if cm.start() < b1 else "winst_verlies" if cm.start() < b2 else "balanstotaal"] = v
        rows.append(row)
    return rows


# canonical asset category -> pattern matched on a "Subtotaal|Totaal <x>" line
_ASSET_CATS = [
    ("onroerende_zaken", r"onroerende zaken"),
    ("vervoersmiddelen", r"vervoersmiddelen"),
    ("bedrijfsmiddelen", r"bedrijfsmiddelen"),
    ("deelnemingen", r"deelnemingen"),
    ("intercompany", r"intercompanyvorderingen"),
    ("voorraden", r"voorraden / onderhanden"),
    ("liquide_middelen", r"liquide middelen"),
    ("debiteuren", r"debiteuren"),
    ("bank_zekerheden", r"bank / zekerheden"),
    ("doorstart", r"voortzetten / doorstart"),
    ("rechtmatigheid", r"rechtmatigheid"),
    ("procedures", r"procedures"),
    ("overig", r"overig"),
    ("vrij_actief_totaal", r"vrij actief"),
]


def parse_asset_realizations(text):
    """From the financieel verslag A. Baten: what each asset category was
    realized (sold) for. Takes the last 'Subtotaal|Totaal <cat>' line per
    category and its first € (incl. btw column)."""
    out = {}
    for key, pat in _ASSET_CATS:
        best = None
        for m in re.finditer(r"(?:Sub)?[Tt]otaal\s+" + pat + r"[^\n€]*(" + EUR + r")", text, re.I):
            best = parse_euro(m.group(1))  # last match wins (aggregate line)
        if best is not None:
            out[key] = best
    return {k: v for k, v in out.items() if v}  # drop zero/None


def parse_boedel(text):
    out = {}
    for label, key in [(r"Definitief Saldo", "definitief_saldo"),
                       (r"Saldo Boedelrekening", "saldo_boedelrekening"),
                       (r"Saldo beschikbaar voor uitdeling", "beschikbaar_voor_uitdeling")]:
        m = re.search(label + r"\s+(" + EUR + r")", text)
        if m:
            out[key] = parse_euro(m.group(1))
    return out


def parse_creditors(text):
    """Preferente/concurrente schuldeisers: claimed vs paid + recovery %."""
    out = {}
    for kind in ("preferente", "concurrente"):
        m = re.search(r"Totaal " + kind + r" schuldeisers\s+(" + EUR + r")\s+(" + EUR + r")\s+([\d.,]+)\s*%", text)
        if m:
            out[kind] = {
                "ingediend": parse_euro(m.group(1)),
                "uitkering": parse_euro(m.group(2)),
                "recovery_pct": float(m.group(3).replace(".", "").replace(",", ".")),
            }
    return out
