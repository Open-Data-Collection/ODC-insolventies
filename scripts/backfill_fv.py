#!/usr/bin/env python3
"""Historical backfill via faillissementsverslagen.com.

The official register (insolventies.rechtspraak.nl) only *searches* ~1 month
back and purges cases ~6 months after they end, so it cannot be enumerated
historically. faillissementsverslagen.com indexes 705k+ verslagen back to
2010 in a plain server-rendered listing whose kenmerk column encodes the
registry insolventienummer (plus KvK, name, city, date). Searching the
register BY insolventienummer bypasses the date window entirely, so every
case still in the register — notably all long-running open cases — can be
recovered into the normal pipeline.

Phases (both resumable; progress lives in ClickHouse — see schema/backfill.sql):

  enumerate        Walk the fv.com listing oldest-first (100 rows/page, stable
                   pages) and write rows to insolventies.fv_listing. Only the
                   listing is fetched — no fv.com detail pages or PDFs.
  probe            For each unique derived insolventienummer not yet probed
                   and not already in processed_cases: search the register.
                   Found -> record + push {"kenmerk": publicatiekenmerk} onto
                   insolventies:tasks (the deployed worker does the rest).
                   Not found -> purged; recorded for a later decision.
  validate-courts  Empirically verify the pre-2013 court-abbrev -> number
                   mapping using findGoedgekeurdeVerslagen (works even for
                   purged cases; the returned VerslagKenmerk echoes the
                   abbreviation).
  status           Progress counters.

Requests to both sites go through the GEONode residential pool (Nomad var
secrets/geonode-residential, or PROXY_POOL_FILE with host:port:user:pass
lines). Run from the repo root: python -m scripts.backfill_fv <cmd>
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CH_HOST     = os.environ.get("CLICKHOUSE_HOST", "100.81.80.15")
REDIS_HOST  = os.environ.get("REDIS_HOST", "100.83.247.12")
NOMAD_ADDR  = os.environ.get("NOMAD_ADDR", "http://100.83.247.12:4646")
QUEUE       = "insolventies:tasks"
FV_BASE     = "https://www.faillissementsverslagen.com"
FV_LIST     = f"{FV_BASE}/faillissement/verslagen"
UA          = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE_SIZE   = 100

# Pre-2013 arrondissement numbers (herziening gerechtelijke kaart merged these
# away; the register keeps the numbering a case was opened under). Verified
# empirically via `validate-courts` — alk=14 confirmed by hand 2026-07-12.
OLD_COURTS = {
    "she": 1,   # 's-Hertogenbosch
    "bre": 2,   # Breda
    "maa": 3,   # Maastricht
    "roe": 4,   # Roermond
    "arn": 5,   # Arnhem
    "zut": 6,   # Zutphen
    "zwo": 7,   # Zwolle-Lelystad
    "alm": 8,   # Almelo
    "gra": 9,   # 's-Gravenhage
    "sgr": 9,   # 's-Gravenhage (fv.com spelling)
    "dha": 9,   # 's-Gravenhage (alt abbrev)
    "rot": 10,  # Rotterdam
    "dor": 11,  # Dordrecht
    "mid": 12,  # Middelburg
    "ams": 13,  # Amsterdam
    "alk": 14,  # Alkmaar
    "haa": 15,  # Haarlem
    "utr": 16,  # Utrecht
    "lee": 17,  # Leeuwarden
    "gro": 18,  # Groningen
    "ass": 19,  # Assen
}

# 10_rot_26_194_F_V_02_B / 08.one.12.388.F.V.03.B — court number included;
# underscored (current) and dotted (2011-2014 transition era, incl. the
# short-lived Oost-Nederland court) spellings
NEW_KENMERK_RE = re.compile(
    r"^(?P<nr>\d{2})[._](?P<abbr>[a-z]{2,4})[._](?P<yy>\d{2})[._](?P<serial>\d+)[._](?P<letter>[A-Z])[._]V[._]"
)
# alk.09.164.F.V.01 / arn_09_80_F_V_01  (pre-2013: abbreviation only; the
# listing carries both dotted and underscored spellings of the same case)
OLD_KENMERK_RE = re.compile(
    r"^(?P<abbr>[a-z]{2,4})[._](?P<yy>\d{2})[._](?P<serial>\d+)[._](?P<letter>[A-Z])[._]V[._]"
)

ROW_RE = re.compile(
    r'<a href="/faillissement/verslagen/verslag/(?P<kenmerk>[^"]+)"[^>]*>Details</a></td>\s*'
    r'<td nowrap="nowrap">(?P<kvk>[^<]*)</td>\s*'
    r'<td nowrap="nowrap" class="sorting_1">(?P<datum>[\d-]+)</td>\s*'
    r'<td nowrap="nowrap"><span title="(?P<name>[^"]*)">.*?</span></td>\s*'
    r'<td nowrap="nowrap">(?P<city>[^<]*)</td>',
    re.DOTALL,
)
INFO_RE = re.compile(r"Resulaten ([\d.]+) tot ([\d.]+) van ([\d.]+)")


def _pass(entry: str) -> str:
    return subprocess.run(
        ["pass", entry], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]


def ch_url() -> str:
    if url := os.environ.get("CLICKHOUSE_URL"):
        return url
    pw = urllib.parse.quote(_pass("projects/ODC-infra/clickhouse"))
    return f"http://{CH_HOST}:8123/?user=odc&password={pw}"


_CH_URL = None


def ch(query: str, data: bytes | None = None) -> str:
    global _CH_URL
    if _CH_URL is None:
        _CH_URL = ch_url()
    url = _CH_URL
    if data is not None:
        url += "&query=" + urllib.parse.quote(query)
    last_err = None
    for attempt in range(8):  # ride out CH restarts (observed: minutes-long)
        try:
            if data is not None:
                resp = requests.post(url, data=data, timeout=120)
            else:
                resp = requests.post(url, data=query.encode(), timeout=120)
            if not resp.ok:
                raise RuntimeError(f"CH error {resp.status_code}: {resp.text[:500]}")
            return resp.text
        except (requests.RequestException, RuntimeError) as e:
            last_err = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"CH failed after retries: {last_err}")


def redis_client():
    import redis
    pw = os.environ.get("REDIS_PASSWORD") or _pass("projects/ODC-infra/redis-services")
    return redis.Redis(host=REDIS_HOST, password=pw)


def load_proxies() -> list[str]:
    """GEONode residential pool as http://user:pass@host:port URLs."""
    blob = None
    path = os.environ.get("PROXY_POOL_FILE")
    if path and os.path.exists(path):
        blob = open(path).read()
    else:
        token = os.environ.get("NOMAD_TOKEN") or _pass("projects/ODC-infra/nomad-bootstrap")
        resp = requests.get(
            f"{NOMAD_ADDR}/v1/var/secrets/geonode-residential",
            headers={"X-Nomad-Token": token}, timeout=30,
        )
        resp.raise_for_status()
        blob = resp.json()["Items"]["proxies"]
    proxies = []
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host, port, user, pw = line.split(":", 3)
        proxies.append(f"http://{user}:{pw}@{host}:{port}")
    if not proxies:
        raise RuntimeError("no proxies loaded")
    return proxies


def derive(verslag_kenmerk: str) -> tuple[str, str]:
    """(case_key, insolventienummer) from a fv.com verslag kenmerk.

    Returns ('', '') shaped-but-unmappable pieces as '' so unknowns are
    visible in fv_listing rather than dropped.
    """
    m = NEW_KENMERK_RE.match(verslag_kenmerk)
    if m:
        # normalized to underscored form so both spellings collapse to one case
        case_key = f"{m['nr']}_{m['abbr']}_{m['yy']}_{m['serial']}_{m['letter']}"
        num = f"{m['letter']}.{int(m['nr']):02d}/{m['yy']}/{int(m['serial'])}"
        return case_key, num
    m = OLD_KENMERK_RE.match(verslag_kenmerk)
    if m:
        # normalized to dotted form so both spellings collapse to one case
        case_key = f"{m['abbr']}.{m['yy']}.{m['serial']}.{m['letter']}"
        nr = OLD_COURTS.get(m["abbr"])
        if nr is None:
            return case_key, ""
        num = f"{m['letter']}.{nr:02d}/{m['yy']}/{int(m['serial'])}"
        return case_key, num
    return "", ""


def parse_datum(s: str) -> str | None:
    if not s or s == "00-00-0000":
        return None
    try:
        return datetime.strptime(s, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


# ---------------------------------------------------------------- enumerate

class FvSession:
    """fv.com listing session: 100 rows/page, oldest-first (stable pages)."""

    def __init__(self, proxies: list[str], delay: float):
        self.proxies = proxies
        self.delay = delay
        self.session = None

    def _new_session(self):
        s = requests.Session()
        s.headers["User-Agent"] = UA
        if self.proxies:
            proxy = random.choice(self.proxies)
            s.proxies = {"http": proxy, "https": proxy}
        # length + sort live in the server-side PHP session (query params on
        # paginated URLs are ignored), so set them once per session.
        r1 = s.get(f"{FV_LIST}/?length={PAGE_SIZE}", timeout=60)
        r1.raise_for_status()
        r2 = s.get(f"{FV_LIST}/?order=2&sort=ASC", timeout=60)
        r2.raise_for_status()
        m = INFO_RE.search(r2.text)
        if not m or m.group(1) != "1":
            raise RuntimeError("session init: unexpected listing state")
        self.session = s

    def fetch_page(self, page: int) -> tuple[list[dict], int, int]:
        """Rows + (last_row_index, total) for a 1-based listing page."""
        last_err = None
        for attempt in range(5):
            try:
                if self.session is None:
                    self._new_session()
                time.sleep(self.delay)
                resp = self.session.get(f"{FV_LIST}/{page}/", timeout=60)
                resp.raise_for_status()
                text = resp.text
                m = INFO_RE.search(text)
                if not m:
                    raise RuntimeError("no result-info line (blocked or empty page?)")
                start = int(m.group(1).replace(".", ""))
                end = int(m.group(2).replace(".", ""))
                total = int(m.group(3).replace(".", ""))
                expected = (page - 1) * PAGE_SIZE + 1
                if start != expected:
                    raise RuntimeError(f"page {page}: offset {start} != {expected} (session lost)")
                rows = []
                for rm in ROW_RE.finditer(text):
                    rows.append({
                        "verslag_kenmerk": html.unescape(rm["kenmerk"]).strip(),
                        "kvk_nummer": rm["kvk"].strip(),
                        "verslag_datum": parse_datum(rm["datum"].strip()),
                        "company_name": html.unescape(rm["name"]).strip(),
                        "city": html.unescape(rm["city"]).strip(),
                    })
                if not rows:
                    raise RuntimeError(f"page {page}: 0 rows parsed from listing")
                if len(rows) != end - start + 1:
                    raise RuntimeError(
                        f"page {page}: parsed {len(rows)} rows, expected {end - start + 1}")
                return rows, end, total
            except Exception as e:
                last_err = e
                self.session = None  # rotate proxy + re-init prefs
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"page {page} failed after retries: {last_err}")


def insert_listing(rows: list[dict], page: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for r in rows:
        case_key, num = derive(r["verslag_kenmerk"])
        out.append(json.dumps({
            "verslag_kenmerk": r["verslag_kenmerk"],
            "case_key": case_key,
            "insolventienummer": num,
            "kvk_nummer": r["kvk_nummer"],
            "verslag_datum": r["verslag_datum"],
            "company_name": r["company_name"],
            "city": r["city"],
            "page": page,
            "fetched_at": now,
        }, ensure_ascii=False))
    ch("INSERT INTO insolventies.fv_listing FORMAT JSONEachRow",
       data="\n".join(out).encode())


def cmd_enumerate(args) -> int:
    if args.reset:
        ch("TRUNCATE TABLE insolventies.fv_listing")
        print("fv_listing truncated")

    start_page = args.start_page
    if start_page is None:
        max_page = ch("SELECT max(page) FROM insolventies.fv_listing").strip()
        # resume ON the last stored page: its fetch may have been partial
        start_page = max(1, int(max_page or 0))
    proxies = [] if args.no_proxy else load_proxies()
    fv = FvSession(proxies, args.delay)

    print(f"enumerating from page {start_page} (100 rows/page, oldest-first, "
          f"{len(proxies)} proxies, delay {args.delay}s)")
    page, done_pages = start_page, 0
    while True:
        rows, end, total = fv.fetch_page(page)
        insert_listing(rows, page)
        done_pages += 1
        if done_pages % 25 == 0 or end >= total:
            print(f"  page {page}: rows {end}/{total}")
        if end >= total:
            print(f"done — reached end of listing ({total} rows)")
            break
        if args.limit and done_pages >= args.limit:
            print(f"stopping after --limit {args.limit} pages (next: {page + 1})")
            break
        page += 1
    return 0


# -------------------------------------------------------------------- probe

def make_api_client(proxies: list[str], delay: float):
    from src.api import ApiClient
    api = ApiClient(delay=delay)
    if proxies:
        proxy = random.choice(proxies)
        api.session.proxies = {"http": proxy, "https": proxy}
    return api


def search_by_number(api, num: str) -> dict:
    api._ensure_csrf()
    api._throttle()
    payload = {"model": json.dumps({
        "periode": "",
        "rechtbank": [],
        "publicatiesoort": [],
        "publicatiekenmerk": "",
        "insolventienummer": num,
        "startDate": "",
    })}
    resp = api._post_search(payload)
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", data)
    if not isinstance(result, dict) or result.get("status") != 1:
        raise RuntimeError(f"search status != 1: {json.dumps(result)[:300]}")
    model = result.get("model") or {}
    return {"aantal": model.get("aantalResultaten", 0),
            "items": model.get("items") or []}


PENDING_PROBE_SQL = """
SELECT DISTINCT insolventienummer
FROM insolventies.fv_listing
WHERE insolventienummer != ''
  AND insolventienummer NOT IN (
      SELECT insolventienummer FROM insolventies.backfill_probe
  )
  AND insolventienummer NOT IN (
      SELECT insolventienummer FROM insolventies.processed_cases
      WHERE insolventienummer != ''
  )
FORMAT TSV
"""


def case_year(num: str) -> int:
    """F.10/26/194 -> 2026 (two-digit years: <=30 is 20xx, else 19xx)."""
    try:
        yy = int(num.split("/")[1])
        return 2000 + yy if yy <= 30 else 1900 + yy
    except (IndexError, ValueError):
        return 0


def flush_probes(rows: list[dict]) -> None:
    if not rows:
        return
    ch("INSERT INTO insolventies.backfill_probe FORMAT JSONEachRow",
       data="\n".join(json.dumps(r) for r in rows).encode())


def cmd_probe(args) -> int:
    if args.reset:
        ch("TRUNCATE TABLE insolventies.backfill_probe")
        print("backfill_probe truncated")

    pending = [n for n in ch(PENDING_PROBE_SQL).splitlines() if n]
    # newest cases first: most likely still open, front-loads the value
    pending.sort(key=case_year, reverse=True)
    if args.limit:
        pending = pending[: args.limit]
    print(f"insolventienummers to probe: {len(pending)}"
          + (" (dry-run: no pushes, no writes)" if args.dry_run else ""))
    if not pending:
        return 0

    proxies = [] if args.no_proxy else load_proxies()
    r = None if args.dry_run else redis_client()
    api = make_api_client(proxies, args.delay)
    since_rotate = 0
    buf: list[dict] = []
    found = notfound = errors = pushed = 0

    for i, num in enumerate(pending, 1):
        if proxies and since_rotate >= args.rotate_every:
            api = make_api_client(proxies, args.delay)
            since_rotate = 0
        since_rotate += 1

        row = {"insolventienummer": num, "status": "error", "aantal": 0,
               "publicatiekenmerk": "", "queued": 0, "error": "",
               "probed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
        try:
            res = None
            for attempt in range(3):
                try:
                    res = search_by_number(api, num)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    api = make_api_client(proxies, args.delay)  # fresh proxy + CSRF
                    since_rotate = 0
            if res["aantal"] > 0 and res["items"]:
                row["status"] = "found"
                row["aantal"] = res["aantal"]
                row["publicatiekenmerk"] = res["items"][0].get("publicatiekenmerk", "")
                found += 1
                if not args.dry_run and row["publicatiekenmerk"]:
                    while r.llen(QUEUE) >= args.max_queue:
                        print(f"  queue >= {args.max_queue}, waiting 60s ...")
                        time.sleep(60)
                    r.rpush(QUEUE, json.dumps(
                        {"kenmerk": row["publicatiekenmerk"], "court": ""}))
                    row["queued"] = 1
                    pushed += 1
            else:
                row["status"] = "not_found"
                notfound += 1
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"[:500]
            errors += 1

        if args.dry_run:
            print(f"  {num}: {row['status']} {row['publicatiekenmerk']}")
        else:
            buf.append(row)
            if len(buf) >= 50:
                flush_probes(buf)
                buf = []
        if i % 200 == 0:
            print(f"  {i}/{len(pending)}  found={found} not_found={notfound} "
                  f"errors={errors} pushed={pushed}")

    if not args.dry_run:
        flush_probes(buf)
    print(f"done. probed={len(pending)} found={found} not_found={notfound} "
          f"errors={errors} pushed={pushed}")
    return 0


# ---------------------------------------------------------- validate-courts

def cmd_validate_courts(args) -> int:
    """Verify OLD_COURTS empirically: per abbreviation pick a sample case and
    check findGoedgekeurdeVerslagen(F.<nr>/yy/serial) echoes the abbreviation
    in the returned VerslagKenmerk. Falls back to scanning 1..19 on miss."""
    samples = ch("""
        SELECT abbr, any(case_key) FROM (
            SELECT extract(case_key, '^([a-z]{2,4})\\\\.') AS abbr, case_key
            FROM insolventies.fv_listing
            WHERE match(case_key, '^[a-z]{2,4}\\\\.')
        ) GROUP BY abbr ORDER BY abbr FORMAT TSV
    """).splitlines()
    if not samples:
        print("no old-format rows in fv_listing yet — run enumerate first")
        return 1
    proxies = [] if args.no_proxy else load_proxies()
    api = make_api_client(proxies, args.delay)
    ok = True
    for line in samples:
        abbr, case_key = line.split("\t")
        _, yy, serial, letter = case_key.split(".")
        mapped = OLD_COURTS.get(abbr)
        hit = None
        candidates = ([mapped] if mapped else []) + [n for n in range(1, 20) if n != mapped]
        for nr in candidates:
            num = f"{letter}.{nr:02d}/{yy}/{int(serial)}"
            try:
                reports = api.get_reports(num)
            except Exception:
                continue
            if any(v.get("VerslagKenmerk", "").startswith(abbr) for v in reports):
                hit = nr
                break
        status = "OK" if hit == mapped else f"MISMATCH (mapped={mapped}, actual={hit})"
        if hit != mapped:
            ok = False
        print(f"  {abbr}: sample {case_key} -> court {hit}  [{status}]")
    return 0 if ok else 1


# -------------------------------------------------------------------- status

def cmd_status(_args) -> int:
    print(ch("""
        SELECT 'fv_listing rows' AS what, toString(count()) AS n FROM insolventies.fv_listing
        UNION ALL SELECT 'fv_listing pages', toString(uniqExact(page)) FROM insolventies.fv_listing
        UNION ALL SELECT 'unique cases (derivable)', toString(uniqExact(insolventienummer))
            FROM insolventies.fv_listing WHERE insolventienummer != ''
        UNION ALL SELECT 'rows underivable', toString(countIf(insolventienummer = ''))
            FROM insolventies.fv_listing
        UNION ALL SELECT 'probed ' || status, toString(count())
            FROM insolventies.backfill_probe GROUP BY status
        UNION ALL SELECT 'tasks pushed', toString(sum(queued)) FROM insolventies.backfill_probe
        FORMAT PrettyCompactMonoBlock
    """))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enumerate", help="scrape fv.com listing into fv_listing")
    e.add_argument("--start-page", type=int, default=None,
                   help="1-based page to start at (default: resume from CH)")
    e.add_argument("--limit", type=int, default=0, help="max pages this run")
    e.add_argument("--delay", type=float, default=1.0)
    e.add_argument("--reset", action="store_true")
    e.add_argument("--no-proxy", action="store_true")
    e.set_defaults(func=cmd_enumerate)

    pr = sub.add_parser("probe", help="probe register by insolventienummer, queue found cases")
    pr.add_argument("--limit", type=int, default=0, help="max probes this run")
    pr.add_argument("--delay", type=float, default=0.7)
    pr.add_argument("--rotate-every", type=int, default=200,
                    help="probes per proxy/CSRF session")
    pr.add_argument("--max-queue", type=int, default=5000,
                    help="pause pushing while queue depth >= this")
    pr.add_argument("--dry-run", action="store_true",
                    help="no queue pushes, no CH writes; print verdicts")
    pr.add_argument("--reset", action="store_true")
    pr.add_argument("--no-proxy", action="store_true")
    pr.set_defaults(func=cmd_probe)

    v = sub.add_parser("validate-courts", help="verify pre-2013 court mapping")
    v.add_argument("--delay", type=float, default=1.0)
    v.add_argument("--no-proxy", action="store_true")
    v.set_defaults(func=cmd_validate_courts)

    st = sub.add_parser("status", help="progress counters")
    st.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
