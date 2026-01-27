from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from typing import Optional, TextIO

from src.api import ApiClient, COURTS

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredCase:
    kenmerk: str
    court: str
    description: str

    def to_json(self) -> str:
        return json.dumps({
            "kenmerk": self.kenmerk,
            "court": self.court,
            "description": self.description,
        }, ensure_ascii=False)


def discover_court(client: ApiClient, court: str) -> list[DiscoveredCase]:
    """Search a single court and return discovered cases."""
    response = client.search(court)
    cases = []

    # API returns {result: {model: {items: [...], aantalResultaten: N}, status: 1}}
    items = response
    if isinstance(response, dict):
        result = response.get("result", response)
        if isinstance(result, dict):
            model = result.get("model", result)
            if isinstance(model, dict):
                items = model.get("items", [])
            else:
                items = model
        else:
            items = result
    if not isinstance(items, list):
        items = []

    for item in items:
        kenmerk = (
            item.get("publicatiekenmerk")
            or item.get("publicatieKenmerk")
            or item.get("kenmerk", "")
        )
        description = (
            item.get("publicatieomschrijving")
            or item.get("publicatieOmschrijving")
            or item.get("omschrijving", "")
        )
        if kenmerk:
            cases.append(DiscoveredCase(
                kenmerk=kenmerk,
                court=court,
                description=description,
            ))

    logger.info("Court %s: found %d cases", court, len(cases))
    return cases


def discover_all(
    client: ApiClient,
    courts: Optional[list[str]] = None,
    output: TextIO = sys.stdout,
) -> list[DiscoveredCase]:
    """Run discovery for all (or specified) courts, writing JSONL to output."""
    courts = courts or COURTS
    all_cases = []
    seen_kenmerks = set()

    for court in courts:
        try:
            cases = discover_court(client, court)
        except Exception:
            logger.exception("Failed to search court %s", court)
            continue

        for case in cases:
            if case.kenmerk not in seen_kenmerks:
                seen_kenmerks.add(case.kenmerk)
                all_cases.append(case)
                output.write(case.to_json() + "\n")

    logger.info("Discovery complete: %d unique cases from %d courts", len(all_cases), len(courts))
    return all_cases
