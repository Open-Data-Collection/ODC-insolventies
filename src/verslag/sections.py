"""Split a narrative verslag (pdftotext -layout output) into Recofa sections.

The openbaar verslag follows the Recofa model with consistent numbered
headers (1. Inventarisatie, 1.5 Oorzaak faillissement, 2. Personeel,
3. Activa, 6. Voortzetten / doorstart onderneming, 8. Crediteuren, ...).
Splitting on those headers lets us hand the LLM one short section at a time
instead of the whole 5-6k-token document.
"""
from __future__ import annotations

import re

# Canonical anchors we care about -> (regex to find the header line).
# Ordered; each section runs until the next matched header.
_ANCHORS = [
    ("activiteiten", r"^\s*Activiteiten\s+onderneming\s*$"),
    ("inventarisatie", r"^\s*1\.\s*Inventarisatie"),
    ("oorzaak", r"^\s*1\.5\s*Oorzaak\s+faillissement"),
    ("personeel", r"^\s*2\.\s*Personeel"),
    ("activa", r"^\s*3\.\s*Activa"),
    ("andere_activa", r"^\s*3\.8\s*Andere\s+activa"),
    ("debiteuren", r"^\s*4\.\s*Debiteuren"),
    ("zekerheden", r"^\s*5\.\s*Bank\s*/\s*Zekerheden"),
    ("doorstart", r"^\s*6\.\s*Voortzetten\s*/\s*doorstart"),
    ("rechtmatigheid", r"^\s*7\.\s*Rechtmatigheid"),
    ("crediteuren", r"^\s*8\.\s*Crediteuren"),
    ("procedures", r"^\s*9\.\s*Procedures"),
    ("overig", r"^\s*10\.\s*Overig"),
]


def split_sections(text: str) -> dict[str, str]:
    """Return {section_key: section_text}. A key is present only if its header
    was found. Text before the first header is stored under 'head'."""
    lines = text.splitlines()
    # find (line_index, key) for every anchor that matches, in document order
    hits: list[tuple[int, str]] = []
    compiled = [(k, re.compile(p, re.I)) for k, p in _ANCHORS]
    seen = set()
    for i, ln in enumerate(lines):
        for key, rx in compiled:
            if key in seen:
                continue
            if rx.match(ln):
                hits.append((i, key))
                seen.add(key)
                break
    hits.sort()

    out: dict[str, str] = {}
    if not hits:
        return {"head": text}
    if hits[0][0] > 0:
        out["head"] = "\n".join(lines[: hits[0][0]]).strip()
    for idx, (start, key) in enumerate(hits):
        end = hits[idx + 1][0] if idx + 1 < len(hits) else len(lines)
        out[key] = "\n".join(lines[start:end]).strip()
    return out


def clean(text: str) -> str:
    """Undo pdftotext quirks + strip control chars (which break JSON bodies).

    pdftotext injects a stray space after some 'w'/'W' glyphs
    ('w orden' -> 'worden'); collapse those and drop control chars."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\b([wW]) ([a-z])", r"\1\2", text)  # 'w orden' -> 'worden'
    return text
