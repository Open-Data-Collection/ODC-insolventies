"""E4B micro-prompts: one narrow task per Recofa section (small context →
higher accuracy on a small model). Each function takes a SINGLE section's
text and returns a small dict.

Endpoint is OpenAI-compatible and env-configurable so the same code runs
against the local llama-server or the odc-llm gateway:
    LLM_URL   (default gateway)   LLM_TOKEN   LLM_MODEL (default gemma-4-E4B)
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

_URL = os.environ.get("LLM_URL", "http://100.83.247.12:8080").rstrip("/")
_MODEL = os.environ.get("LLM_MODEL", "gemma-4-E4B")


def _token():
    t = os.environ.get("LLM_TOKEN")
    if t:
        return t
    try:
        return open(os.path.expanduser("~/Code/odc-llm-gateway/secrets/gateway_token")).read().strip()
    except OSError:
        return ""


def _call(system: str, user: str, max_tokens: int = 220) -> dict | None:
    """One chat completion; parse the {...} JSON block from the reply."""
    if not user or not user.strip():
        return None
    body = json.dumps({
        "model": _MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user[:6000]}],
        "temperature": 0.1, "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(_URL + "/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        method="POST")
    txt = json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


_SECTORS = '["bouw","zorg","horeca","detailhandel","groothandel","ICT","energie","transport","industrie","vastgoed","financieel","landbouw","zakelijke dienstverlening","overig"]'


def sector(activiteiten_text: str) -> dict | None:
    return _call(
        "Uit de sectie 'Activiteiten onderneming' van een faillissementsverslag. "
        "Antwoord met UITSLUITEND JSON: "
        '{"activiteiten": korte omschrijving (max 12 woorden), '
        f'"sector": exact één van {_SECTORS}}}. Baseer je op de tekst.',
        activiteiten_text)


def oorzaak(oorzaak_text: str) -> dict | None:
    return _call(
        "Uit de sectie 'Oorzaak faillissement'. Antwoord met UITSLUITEND JSON: "
        '{"oorzaak": korte omschrijving (max 15 woorden), '
        '"categorie": exact één van ["markt","liquiditeit","wanbeleid","fraude","corona","overig"]}. '
        "Kies 'fraude' alleen bij duidelijke aanwijzing (bv. strafrechtelijk onderzoek, verduistering).",
        oorzaak_text)


def doorstart(doorstart_text: str) -> dict | None:
    return _call(
        "Uit de sectie 'Voortzetten / doorstart onderneming'. Antwoord met UITSLUITEND JSON: "
        '{"doorstart": true of false (is de onderneming of zijn de activa doorgestart/overgenomen), '
        '"overnemer": naam van de overnemende partij of null}. null bij onbekend.',
        doorstart_text)


def domeinnamen(andere_activa_text: str) -> dict | None:
    """Domains / IE assets from '3.8 Andere activa' — feeds the domain-value angle."""
    return _call(
        "Uit de sectie 'Andere activa' (immateriële activa) van een faillissementsverslag. "
        "Antwoord met UITSLUITEND JSON: "
        '{"domeinnamen": [lijst van domeinnamen/websites, of leeg], '
        '"ie_rechten": korte omschrijving van merk/IE/handelsnaam-rechten of null, '
        '"verkocht": true of false (zijn deze immateriële activa verkocht)}. '
        "Neem alleen expliciet genoemde domeinnamen op.",
        andere_activa_text)
