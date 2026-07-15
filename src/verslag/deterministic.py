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
    don't misalign). Lives in the 'Financiële gegevens' block of a narrative
    verslag.

    Consolidated multi-BV verslagen (one verslag covering a whole group)
    repeat the same year once per BV — those rows can't be attributed to THIS
    company, so a duplicate year aborts the parse and returns []."""
    rows, lines, hdr = [], text.splitlines(), None
    for i, ln in enumerate(lines):
        if re.search(r"Jaar\s+Omzet", ln) and "Balanstotaal" in ln:
            hdr = i
            break
    if hdr is None:
        return rows
    h = lines[hdr]
    if "Winst" not in h:  # header variant without a Winst/verlies column
        return rows
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
        jaar = int(m.group(1) + m.group(2))
        if any(r["jaar"] == jaar for r in rows):
            return []  # consolidated multi-BV table — not attributable
        row = {"jaar": jaar, "omzet": None,
               "winst_verlies": None, "balanstotaal": None}
        for cm in re.finditer(EUR, ln):
            v = parse_euro(cm.group(0))
            row["omzet" if cm.start() < b1 else "winst_verlies" if cm.start() < b2 else "balanstotaal"] = v
        rows.append(row)
    return rows


def merge_omzet(primary, secondary):
    """Merge two omzet tables by year; primary (latest verslag — cumulative,
    most complete) wins per non-null cell, secondary fills the gaps."""
    by_year = {r["jaar"]: dict(r) for r in secondary}
    for r in primary:
        cur = by_year.setdefault(r["jaar"], dict(r))
        for k in ("omzet", "winst_verlies", "balanstotaal"):
            if r.get(k) is not None:
                cur[k] = r[k]
    return [by_year[y] for y in sorted(by_year)]


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
    # voortzetten and doorstart split out (their combined section total would
    # overstate doorstart proceeds); the "totaal\s+" anchor in the finditer
    # keeps "doorstart onderneming" from matching the combined line.
    ("voortzetten", r"voortzetten onderneming"),
    ("doorstart", r"doorstart onderneming"),
    ("voortzetten_doorstart_totaal", r"voortzetten / doorstart"),
    ("rechtmatigheid", r"rechtmatigheid"),
    ("procedures", r"procedures"),
    # (?![a-z]) so "overige gebonden activa" (usually € 0,00) can't shadow the
    # real "Subtotaal overig" restituties/rente line via last-match-wins
    ("overig", r"overig(?![a-z])"),
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


_CRED_LABELS = {
    # uitdelingslijst label variants seen in the wild (spot-check: some
    # verslagen use bare "Concurrente crediteuren" instead of "Totaal ...
    # schuldeisers", which lost the 0%-recovery signal)
    "preferente": r"(?:Totaal preferente schuldeisers|Preferente crediteuren)",
    "concurrente": r"(?:Totaal concurrente schuldeisers|Concurrente crediteuren)",
}


def parse_creditors(text):
    """Preferente/concurrente schuldeisers: claimed vs paid + recovery %."""
    out = {}
    for kind, label in _CRED_LABELS.items():
        m = re.search(label + r"\s+(" + EUR + r")\s+(" + EUR + r")\s+([\d.,]+)\s*%", text)
        if m:
            out[kind] = {
                "ingediend": parse_euro(m.group(1)),
                "uitkering": parse_euro(m.group(2)),
                "recovery_pct": float(m.group(3).replace(".", "").replace(",", ".")),
            }
    return out


# debt-load labels; matched in the financieel verslag's crediteuren block
# first, then the narrative 8.x section as fallback. First € on the line is
# the claimed (ingediend) amount.
_DEBT_LABELS = [
    ("boedelvorderingen", r"Totaal boedelvorderingen"),
    ("pref_fiscus", r"Preferente vordering(?:en)? van de fiscus"),
    ("pref_uwv", r"Preferente vordering(?:en)? van het UWV"),
    ("pref_overig", r"Andere preferente crediteuren"),
    ("pref_totaal", r"Totaal preferente schuldeisers"),
    ("concurrent_bedrag", r"(?:Totaal concurrente schuldeisers|Bedrag concurrente crediteuren|Concurrente crediteuren)"),
]


def parse_debt(fin_text, crediteuren_section=""):
    """Debt amounts (what was claimed, not what was paid).
    concurrent_aantal (8.5) is left to the LLM — its form layout is erratic."""
    out = {}
    for key, label in _DEBT_LABELS:
        for source in (fin_text, crediteuren_section):
            if not source or key in out:
                continue
            m = re.search(label + r"[^\n€]*(" + EUR + r")", source, re.I)
            if m:
                v = parse_euro(m.group(1))
                if v is not None:
                    out[key] = v
    return out


_PERSONEEL_HDRS = [
    ("personeel_ttv", r"Aantal ten tijde van faill|Personeel gemiddeld aantal"),
    ("personeel_jaar_voor", r"Aantal in jaar voor faill"),
]


def parse_personeel(text):
    """Headcount from section 2 (KEI layout). The value sits on its own line
    in the LEFT column within a few lines of the header; the right-hand column
    holds verslag numbers/dates — so only accept a standalone integer that
    starts before column 40. (An LLM reliably grabs the wrong column here:
    it read '1' for a 69-employee firm.)"""
    out = {}
    lines = text.splitlines()
    for key, hdr in _PERSONEEL_HDRS:
        rx = re.compile(hdr, re.I)
        for i, ln in enumerate(lines):
            if not rx.search(ln):
                continue
            for cand in lines[i + 1: i + 5]:
                m = re.match(r"^(\s*)(\d{1,5})\s*$", cand)
                if m and len(m.group(1)) < 40:
                    out[key] = int(m.group(2))
                    break
            if key in out:
                break
    return out


# narrative section 8 (Crediteuren) subsections -> debt keys
_SEC8_KEY = {"1": "boedelvorderingen", "2": "pref_fiscus", "3": "pref_uwv",
             "4": "pref_overig", "5": "concurrent_aantal", "6": "concurrent_bedrag"}
# a value line: left-column (col<40) bare amount, optionally followed by the
# right-hand verslag-number column. Lines with text before the amount are
# itemized claims — skipped on purpose.
_SEC8_EUR = re.compile(r"^(\s{0,39})(€\s*-?\s*[\d.]+,\d{2})\s*\d{0,4}\s*$")
_SEC8_INT = re.compile(r"^(\s{0,39})(\d{1,6})(\s+\d{1,4})?\s*$")


def parse_debt_sections(text):
    """Debt amounts from a narrative verslag's section 8. The layout is
    cumulative — each subsection repeats one value line per verslag-period —
    so the LAST standalone value in a subsection window is the current one."""
    lines = text.splitlines()
    hdrs = []
    for i, ln in enumerate(lines):
        m = re.match(r"\s{0,8}8\.(\d)\s", ln)
        if m:
            hdrs.append((i, m.group(1)))
        elif re.match(r"\s{0,8}(9\.|10\.)\s", ln):
            hdrs.append((i, None))
    out = {}
    for j, (start, digit) in enumerate(hdrs):
        key = _SEC8_KEY.get(digit or "")
        if not key:
            continue
        end = hdrs[j + 1][0] if j + 1 < len(hdrs) else min(len(lines), start + 40)
        last = None
        for ln in lines[start + 1: end]:
            if key == "concurrent_aantal":
                m = _SEC8_INT.match(ln)
                if m:
                    v = int(m.group(2))
                    # a bare 4-digit line could be a year; require the
                    # verslag-number column for year-like values
                    if 1990 <= v <= 2035 and not m.group(3):
                        continue
                    last = v
            else:
                m = _SEC8_EUR.match(ln)
                if m:
                    v = parse_euro(m.group(2))
                    if v is not None:
                        last = v
        if last is not None:
            out[key] = last
    return out
