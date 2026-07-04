"""Verslag extraction: turn a faillissementsverslag (Recofa model) into
structured financial + firmographic data.

Layers:
  - sections.py      split the pdftotext output into Recofa sections
  - deterministic.py regex/column-position parsers (revenue, asset sale prices,
                     boedel totals, creditor recovery) — no LLM, exact
  - llm.py           E4B micro-prompts on single sections (sector, cause,
                     doorstart buyer, domeinnamen/IE) — narrow tasks, small ctx
  - extract.py       orchestrator: text(s) -> one combined record
"""
