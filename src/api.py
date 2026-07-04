from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE_URL = "https://insolventies.rechtspraak.nl"

COURTS = [
    "amsterdam", "den haag", "gelderland", "limburg", "midden-nederland",
    "noord-holland", "noord-nederland", "oost-brabant", "overijssel",
    "rotterdam", "zeeland-west-brabant",
]


class ApiClient:
    def __init__(self, delay: float = None):
        self.delay = delay if delay is not None else float(os.environ.get("REQUEST_DELAY", "1.0"))
        self.session = self._build_session()
        self._csrf_token: Optional[str] = None

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers["User-Agent"] = "OpenDataCollection-Insolventies/1.0 (https://opendatacollection.com)"
        return session

    def _throttle(self):
        if self.delay > 0:
            time.sleep(self.delay)

    def _ensure_csrf(self):
        if self._csrf_token:
            return
        self._fetch_csrf()

    def _fetch_csrf(self):
        logger.info("Fetching CSRF token")
        self._throttle()
        resp = self.session.get(BASE_URL, timeout=30)
        resp.raise_for_status()
        match = re.search(
            r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"',
            resp.text,
        )
        if not match:
            match = re.search(
                r'__RequestVerificationToken[^>]+value="([^"]+)"',
                resp.text,
            )
        if not match:
            raise RuntimeError("Could not find CSRF token on homepage")
        self._csrf_token = match.group(1)
        logger.debug("Got CSRF token: %s...", self._csrf_token[:20])

    def _post_search(self, payload: dict) -> requests.Response:
        """Execute a search POST with CSRF handling and retry on 403."""
        url = f"{BASE_URL}/Services/WebInsolventieService/zoekOpKenmerk"
        headers = {
            "__RequestVerificationToken": self._csrf_token,
            "Content-Type": "application/json",
        }

        resp = self.session.post(url, json=payload, headers=headers, timeout=60)

        if resp.status_code == 403:
            logger.warning("Got 403, refreshing CSRF token and retrying")
            self._csrf_token = None
            self._fetch_csrf()
            self._throttle()
            headers["__RequestVerificationToken"] = self._csrf_token
            resp = self.session.post(url, json=payload, headers=headers, timeout=60)

        return resp

    @staticmethod
    def _month_back(now: datetime) -> datetime:
        """One calendar month before `now`, matching the site's 'Laatste maand'
        lower bound. Day clamped to 28 so we never build an invalid date; the
        server's exact minimum is honoured via the validation-reject retry in
        search()."""
        month = now.month - 1 or 12
        year = now.year - (1 if now.month == 1 else 0)
        return now.replace(year=year, month=month, day=min(now.day, 28))

    def _default_start_date(self, periode: str, now: datetime) -> str:
        if periode == "Laatste week":
            return (now - timedelta(days=7)).isoformat()
        if periode == "Vandaag":
            return now.isoformat()
        # "Laatste maand" (and any unknown periode): one calendar month back.
        return self._month_back(now).isoformat()

    def search(self, court: str, periode: str = "Laatste maand") -> list[dict]:
        """Search for insolvency publications for a given court.

        Returns the raw JSON result dict from the API. The API enforces a
        minimum startDate (~one calendar month back) and rejects anything
        earlier with a validation message; we parse that message and retry once
        with the server-provided minimum so we always request the widest allowed
        window.
        """
        self._ensure_csrf()
        self._throttle()

        now = datetime.utcnow()
        start_date = self._default_start_date(periode, now)

        def _do(sd: str):
            inner_model = {
                "periode": periode,
                "rechtbank": [court],
                "publicatiesoort": [],  # empty = all types
                "publicatiekenmerk": "",
                "insolventienummer": "",
                "startDate": sd,
            }
            payload = {"model": json.dumps(inner_model)}
            try:
                r = self._post_search(payload)
            except requests.RequestException:
                self._csrf_token = None
                self._ensure_csrf()
                r = self._post_search(payload)
            r.raise_for_status()
            return r.json()

        logger.info("Searching court=%s periode=%s startDate=%s", court, periode, start_date[:10])
        data = _do(start_date)

        corrected = self._corrected_start_date(data)
        if corrected:
            logger.info("startDate rejected; retrying court=%s with server minimum %s", court, corrected)
            self._throttle()
            data = _do(corrected)
        return data

    @staticmethod
    def _corrected_start_date(data: dict) -> Optional[str]:
        """If the response is a StartDate validation reject, return the allowed
        minimum as an ISO datetime; otherwise None."""
        if not isinstance(data, dict):
            return None
        result = data.get("result", data)
        if not isinstance(result, dict) or result.get("status") != 3:
            return None
        for vm in result.get("validationMessages") or []:
            if str(vm.get("key", "")).lower() == "startdate" or "StartDate" in str(vm.get("message", "")):
                m = re.search(r"(\d{2})-(\d{2})-(\d{4})", vm.get("message", ""))
                if m:
                    d, mo, y = m.groups()
                    return f"{y}-{mo}-{d}T00:00:00"
        return None

    def get_case(self, kenmerk: str) -> dict:
        """Fetch full case details for a given kenmerk."""
        self._throttle()
        logger.info("Fetching case %s", kenmerk)
        resp = self.session.get(
            f"{BASE_URL}/Services/WebInsolventieService/haalOp/",
            params={"id": kenmerk},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_reports(self, zaaknummer: str) -> list[dict]:
        """Fetch the approved verslagen (public reports) for a case.

        Keyed on the case's `landelijkUniekZaaknummer` (e.g. 'F.13/24/115') —
        the same identifier the site's frontend uses. NOT the publicatiekenmerk
        (passing that returns an empty list, which is why verslagen were never
        captured before). Returns a list of dicts with keys
        `Titel`, `DatumVerslagen` (.NET /Date(...)/), and `VerslagKenmerk`.
        """
        self._throttle()
        ident = quote(zaaknummer, safe="")
        logger.info("Fetching verslagen for %s", zaaknummer)
        resp = self.session.get(
            f"{BASE_URL}/Services/VerslagenService/findGoedgekeurdeVerslagen/{ident}",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def download_pdf(self, kenmerk: str) -> bytes:
        """Download a PDF report by its kenmerk."""
        self._throttle()
        kenmerk_underscored = kenmerk.replace(".", "_")
        logger.info("Downloading PDF %s", kenmerk_underscored)
        resp = self.session.get(
            f"{BASE_URL}/Services/VerslagenService/getPdf/{kenmerk_underscored}",
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content
