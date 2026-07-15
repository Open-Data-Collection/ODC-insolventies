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
    """One chat completion; parse the {...} JSON block from the reply.

    Degrades instead of raising: a gateway box lane has ctx_size/parallel
    tokens (2048 at parallel=16), so an over-long section 400s — retry once
    with a harder truncation, then give up on this FIELD (returning None)
    rather than failing the whole company."""
    if not user or not user.strip():
        return None
    for clamp in (4500, 1800):
        body = json.dumps({
            "model": _MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user[:clamp]}],
            "temperature": 0.1, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        req = urllib.request.Request(_URL + "/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
            method="POST")
        try:
            txt = json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 400:
                continue  # context overflow — retry clamped harder, then None
            raise
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


_SECTORS = '["bouw","zorg","horeca","detailhandel","groothandel","ICT","energie","transport","industrie","vastgoed","financieel","landbouw","zakelijke dienstverlening","overig"]'


def sector(activiteiten_text: str) -> dict | None:
    return _call(
        "Uit de sectie 'Activiteiten onderneming' van een faillissementsverslag. "
        "Antwoord met UITSLUITEND JSON: "
        '{"activiteiten": korte omschrijving (max 12 woorden), '
        f'"sector": exact één van {_SECTORS}}}. Baseer je op de tekst. '
        "Installatiebedrijven, schilders, loodgieters, dakdekkers en klusbedrijven "
        "vallen onder 'bouw'; kies 'overig' alleen als geen sector past.",
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


def koopsom(doorstart_text: str) -> dict | None:
    """Purchase price of the doorstart/overname, if the curator stated it.

    The financieel verslag only records estate proceeds by category (goodwill,
    assets) — the *agreed koopsom* is usually only in this narrative section
    (e.g. 'een koopprijs overeengekomen van € 55.000'), and can be far higher
    than the goodwill line."""
    return _call(
        "Uit de sectie 'Voortzetten / doorstart onderneming'. Bepaal de KOOPSOM/KOOPPRIJS "
        "die is overeengekomen voor de doorstart of overname van (activa van) de onderneming. "
        "Antwoord met UITSLUITEND JSON: "
        '{"koopsom": totaalbedrag in hele euro als getal, of null als geen bedrag genoemd; '
        '"toelichting": korte toelichting bij het bedrag (bv. "onvoorwaardelijk deel", '
        '"plus variabel/earn-out", "boedelbijdrage per klant") of null}. '
        "Neem alleen een expliciet genoemd overeengekomen bedrag; verzin niets. "
        "Is de koopsom later gecorrigeerd of aangepast, geef dan het gecorrigeerde "
        "(meest recente) bedrag.",
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


# --------------------------------------------------------------- v2 prompts

# honorific matched case-insensitively; the name part stays case-sensitive
# (capitalized tokens + lowercase tussenvoegsels) so the sentence tail survives
_PERSON_RE = re.compile(
    r"(?i:de her(?:en)?|de heer|mevrouw|mevr\.|dhr\.|mw\.)\s+"
    r"(?:(?:van|der|den|de|het|ter|te|op|in|'t|v\.d\.)\s+|[A-Z][\w'.-]*\s*){1,7}",
    re.UNICODE)


def redact_persons(text: str | None) -> str | None:
    """Replace honorific + name spans with a neutral role token. Free-text LLM
    output must never carry a natural person's name into the dataset."""
    if not text:
        return text
    return _PERSON_RE.sub("[persoon] ", text).replace("[persoon]  ", "[persoon] ").strip()


_LEGAL_FORM_RE = re.compile(
    r"\b(B\.?V\.?|N\.?V\.?|V\.?O\.?F\.?|C\.?V\.?|Holding|Beheer|Stichting|"
    r"Coöperatie|U\.?A\.?|GmbH|Ltd|Inc|S\.?A\.?|Group|Groep)\b", re.I)


def looks_like_company(name: str) -> bool:
    return bool(name) and bool(_LEGAL_FORM_RE.search(name))


def grounded(name: str, source_text: str) -> bool:
    """A company name is only trusted if it literally appears in the source
    section (case-insensitive, whitespace-collapsed) — kills hallucinations."""
    if not name or not source_text:
        return False
    canon = re.sub(r"\s+", " ", source_text.lower())
    return re.sub(r"\s+", " ", name.lower()).strip() in canon


def directie(inventarisatie_text: str) -> dict | None:
    """1.1 Directie en organisatie -> persons (to be hashed) + related companies.

    PRIVACY: person names returned here are hashed by the caller before
    storage and never written in cleartext."""
    return _call(
        "Uit de sectie '1.1 Directie en organisatie' van een faillissementsverslag. "
        "Antwoord met UITSLUITEND JSON: "
        '{"personen": [namen van natuurlijke personen die bestuurder/aandeelhouder zijn], '
        '"bedrijven": [{"naam": exacte bedrijfsnaam zoals in de tekst, '
        '"relatie": exact één van ["bestuurder","aandeelhouder","moeder","dochter","gelieerd"]}]}. '
        "Bedrijven zijn rechtspersonen (B.V., N.V., Stichting, Holding, v.o.f. etc). "
        "Neem alleen namen op die letterlijk in de tekst staan. Verhuurders, "
        "leasemaatschappijen en gewone leveranciers/crediteuren zijn GEEN gelieerde "
        "bedrijven — neem die niet op. De gefailleerde onderneming zelf ook niet.",
        inventarisatie_text, max_tokens=350)


def bank(zekerheden_text: str) -> dict | None:
    """5.1/5.3 Bank / Zekerheden -> lender(s) and claim size."""
    return _call(
        "Uit de sectie 'Bank / Zekerheden'. Antwoord met UITSLUITEND JSON: "
        '{"banken": [{"naam": naam van de bank/financier zoals in de tekst, '
        '"vordering": bedrag in euro als getal of null}]}. '
        "Alleen banken/financiers met een vordering of zekerheid; lege lijst indien geen.",
        zekerheden_text, max_tokens=250)


def rechtmatigheid(rechtmatigheid_text: str) -> dict | None:
    """7.x governance red flags as curator-asserted booleans (null = onbekend/nog in onderzoek)."""
    return _call(
        "Uit de sectie '7. Rechtmatigheid'. Antwoord met UITSLUITEND JSON: "
        '{"boekhoudplicht_voldaan": true/false/null, '
        '"depot_jaarrekeningen_ok": true/false/null (jaarrekeningen tijdig gedeponeerd), '
        '"onbehoorlijk_bestuur": true/false/null (heeft de curator onbehoorlijk bestuur vastgesteld), '
        '"paulianeus_handelen": true/false/null}. '
        "De sectie is cumulatief per verslagperiode: de ONDERSTE/meest recente "
        "vermelding geldt. "
        "STRIKT: antwoord null tenzij de curator een expliciete conclusie trekt. "
        "'In onderzoek', 'wordt onderzocht', 'zal onderzocht worden', 'nog niet bekend', "
        "'administratie nog niet ontvangen' -> null. "
        "depot_jaarrekeningen_ok=false ALLEEN als expliciet staat dat te laat of niet "
        "is gedeponeerd; genoemde deponeringsdata zonder oordeel -> true alleen als "
        "de curator zegt dat tijdig is gedeponeerd, anders null.",
        rechtmatigheid_text)


def vastgoed(activa_text: str, inventarisatie_text: str) -> dict | None:
    """3.1 onroerende zaken + huur -> owned/rented premises booleans."""
    return _call(
        "Uit secties van een faillissementsverslag (activa + inventarisatie). "
        "Antwoord met UITSLUITEND JSON: "
        '{"onroerend_goed": true/false/null (bezit de onderneming onroerende zaken/vastgoed), '
        '"huurpand": true/false/null (huurt de onderneming een bedrijfsruimte/pand)}. '
        "null bij niet vermeld.",
        (activa_text or "")[:3000] + "\n---\n" + (inventarisatie_text or "")[:3000])


_AFWIKKELING = '["gebrek_aan_baten","vereenvoudigd","uitdeling","akkoord","voortgezet","onbekend"]'


def afwikkeling(crediteuren_text: str) -> dict | None:
    """8.7 Verwachte wijze van afwikkeling + 8.5 aantal concurrente crediteuren."""
    return _call(
        "Uit de sectie '8. Crediteuren' van een faillissementsverslag. "
        "Antwoord met UITSLUITEND JSON: "
        f'{{"afwikkeling": exact één van {_AFWIKKELING} (verwachte wijze van afwikkeling, 8.7), '
        '"aantal_concurrente_crediteuren": getal uit 8.5 of null}. '
        "'gebrek_aan_baten' = opheffing wegens gebrek aan baten; 'vereenvoudigd' = vereenvoudigde "
        "afwikkeling; 'uitdeling' = uitdeling aan (concurrente) crediteuren; 'onbekend' indien nog onbekend. "
        "De tekst is cumulatief per verslagperiode: de ONDERSTE/meest recente vermelding geldt.",
        crediteuren_text)
