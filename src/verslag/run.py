"""Extraction runner: verslagen -> processed_profile / processed_revenue /
processed_asset_sales.

Per company we read the FIRST narrative (omzet/activiteiten/oorzaak), the LATEST
narrative (doorstart/domeinen) and the LATEST financieel verslag (the money),
pull those PDFs from storage MinIO, pdftotext them, run src.verslag.extract, and
write the structured rows to ClickHouse. Resumable: companies already in
processed_profile are skipped unless --reset.

Runs locally against E4B (set LLM_URL=http://127.0.0.1:8011 LLM_TOKEN=... LLM_MODEL=gemma-4-E4B)
with prod CH + storage MinIO env; or against the gateway once it's healthy.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from odc import OdcClient
from src.verslag.extract import extract_company

PROJECT = "insolventies"
GIT_SHA = os.environ.get("GIT_SHA", "")


def pdf_text(client, vk: str) -> str | None:
    try:
        data = client.get_object("storage", f"{PROJECT}/pdfs/{vk}.pdf")
    except Exception:
        return None
    r = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def worklist(client, reset: bool):
    docs = client.ch_execute("""
        SELECT kenmerk,
               argMinIf(document_kenmerk, document_date, document_type='Verslag')            AS first_nar,
               argMaxIf(document_kenmerk, document_date, document_type='Verslag')            AS last_nar,
               argMaxIf(document_kenmerk, document_date, document_type='Financieel verslag') AS last_fin
        FROM insolventies.processed_documents FINAL
        GROUP BY kenmerk
        HAVING first_nar != '' AND last_fin != ''
    """)
    meta = {r["kenmerk"]: r for r in client.ch_execute(
        "SELECT kenmerk, insolventienummer, kvk_nummer FROM insolventies.processed_cases FINAL WHERE type='company'")}
    done = set()
    if not reset:
        done = {r["kenmerk"] for r in client.ch_execute(
            "SELECT DISTINCT kenmerk FROM insolventies.processed_profile")}
    return [{**d, **meta.get(d["kenmerk"], {})} for d in docs if d["kenmerk"] not in done]


def _f(v):  # None-safe float passthrough for CH Nullable
    return v


def write(client, kenmerk, insno, kvk, rec):
    pref = (rec["creditors"].get("preferente") or {})
    conc = (rec["creditors"].get("concurrente") or {})
    bo = rec["boedel"]
    client.ch_insert("insolventies.processed_profile", [{
        "kenmerk": kenmerk, "insolventienummer": insno or "", "kvk_nummer": kvk,
        "sector": rec.get("sector") or "", "activiteiten": rec.get("activiteiten") or "",
        "oorzaak": rec.get("oorzaak") or "", "oorzaak_categorie": rec.get("oorzaak_categorie") or "",
        "doorstart": rec.get("doorstart"), "overnemer": rec.get("overnemer"),
        "domeinnamen": rec.get("domeinnamen") or [], "ie_rechten": rec.get("ie_rechten"),
        "saldo_boedelrekening": bo.get("saldo_boedelrekening"),
        "definitief_saldo": bo.get("definitief_saldo"),
        "beschikbaar_voor_uitdeling": bo.get("beschikbaar_voor_uitdeling"),
        "pref_recovery_pct": pref.get("recovery_pct"),
        "conc_recovery_pct": conc.get("recovery_pct"),
        "model": os.environ.get("LLM_MODEL", "gemma-4-E4B"),
    }])

    rev = [{"kenmerk": kenmerk, "jaar": y["jaar"], "omzet": y["omzet"],
            "winst_verlies": y["winst_verlies"], "balanstotaal": y["balanstotaal"]}
           for y in rec["omzet_historie"] if y.get("jaar")]
    if rev:
        client.ch_insert("insolventies.processed_revenue", rev)

    sales = [{"kenmerk": kenmerk, "categorie": cat, "verkoopopbrengst": val}
             for cat, val in rec["asset_realizations"].items()]
    if sales:
        client.ch_insert("insolventies.processed_asset_sales", sales)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="max companies (0 = all)")
    p.add_argument("--reset", action="store_true", help="re-extract already-done companies")
    p.add_argument("--no-llm", action="store_true", help="deterministic only (no E4B)")
    args = p.parse_args(argv)

    client = OdcClient(PROJECT)
    wl = worklist(client, args.reset)
    if args.limit:
        wl = wl[: args.limit]
    print(f"companies to extract: {len(wl)}", file=sys.stderr)

    ok = err = 0
    for i, w in enumerate(wl, 1):
        try:
            rec = extract_company(
                pdf_text(client, w["first_nar"]),
                pdf_text(client, w["last_nar"]),
                pdf_text(client, w["last_fin"]),
                use_llm=not args.no_llm)
            write(client, w["kenmerk"], w.get("insolventienummer"), w.get("kvk_nummer"), rec)
            ok += 1
        except Exception as e:
            err += 1
            print(f"  ERR {w['kenmerk']}: {type(e).__name__}: {e}", file=sys.stderr)
        if i % 25 == 0:
            print(f"  {i}/{len(wl)} (ok={ok} err={err})", file=sys.stderr)
    print(f"done: ok={ok} err={err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
