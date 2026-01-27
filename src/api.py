from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

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

    def search(self, court: str, periode: str = "Laatste maand") -> list[dict]:
        """Search for insolvency publications for a given court.

        Returns the raw list of result dicts from the API.
        """
        self._ensure_csrf()
        self._throttle()

        # Compute startDate from periode, matching the frontend logic
        now = datetime.utcnow()
        if periode == "Laatste maand":
            start_date = (now - timedelta(days=31)).isoformat()
        elif periode == "Laatste week":
            start_date = (now - timedelta(days=8)).isoformat()
        elif periode == "Vandaag":
            start_date = now.isoformat()
        else:
            start_date = (now - timedelta(days=31)).isoformat()

        inner_model = {
            "periode": periode,
            "rechtbank": [court],
            "publicatiesoort": [],  # empty = all types
            "publicatiekenmerk": "",
            "insolventienummer": "",
            "startDate": start_date,
        }

        payload = {"model": json.dumps(inner_model)}

        logger.info("Searching court=%s periode=%s", court, periode)
        try:
            resp = self._post_search(payload)
        except requests.RequestException:
            self._csrf_token = None
            self._ensure_csrf()
            resp = self._post_search(payload)

        resp.raise_for_status()
        return resp.json()

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

    def get_reports(self, kenmerk: str) -> list[dict]:
        """Fetch approved reports list for a case."""
        self._throttle()
        kenmerk_underscored = kenmerk.replace(".", "_")
        logger.info("Fetching reports for %s", kenmerk)
        resp = self.session.get(
            f"{BASE_URL}/Services/VerslagenService/findGoedgekeurdeVerslagen/{kenmerk_underscored}",
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
