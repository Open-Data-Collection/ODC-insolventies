"""Processor for insolventies.

Reads status='ok' rows from insolventies.raw_cases, parses the embedded record
JSON, and fans each case out into three analytics tables:
  - processed_cases         (one row per case)
  - processed_publications  (one row per publication event)
  - processed_documents     (one row per verslag)

Checkpoint on scraped_at (DateTime64 UTC). Runs on odc-storage next to CH.
"""
from __future__ import annotations

import json
from datetime import date

from odc.processor import Processor
from odc.logging import warn


# ClickHouse `Date` supports 1970-01-01 .. 2149-06-06. Dutch descriptions
# occasionally yield a mis-parsed year (e.g. event_date 3026-07-06), and a
# single out-of-range value makes clickhouse-connect fail the whole column
# insert ("Unable to create native array for column ..."). Clamp to None.
CH_DATE_MIN = date(1970, 1, 1)
CH_DATE_MAX = date(2149, 6, 6)


def _to_date(value):
    """ISO 'YYYY-MM-DD' (or None) → date | None. Tolerant of junk and clamps
    values outside ClickHouse's supported Date range to None."""
    if not value:
        return None
    try:
        d = date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None
    if d < CH_DATE_MIN or d > CH_DATE_MAX:
        return None
    return d


def _pick_city(addresses: list[dict]) -> str | None:
    """First non-empty city, preferring a vestiging address."""
    for want in ("vestiging", "woon", "correspondentie", None):
        for addr in addresses:
            if (want is None or addr.get("type") == want) and addr.get("city"):
                return addr["city"]
    return None


class InsolventiesProcessor(Processor):
    name               = "insolventies-processor"
    source_table       = "insolventies.raw_cases"
    target_table       = "insolventies.processed_cases"
    checkpoint_column  = "scraped_at"
    initial_checkpoint = "1970-01-01 00:00:00.000"
    max_batch          = 20_000

    def fetch_batch(self, last_checkpoint, limit):
        # scraped_at is DateTime64(_, 'UTC'). Parse the checkpoint explicitly as
        # UTC — a bare `scraped_at > %(c)s` would parse it in the CH session tz
        # (Europe/Amsterdam) and can silently pin the checkpoint (#19). (Inlined
        # rather than using Processor.checkpoint_predicate() so we don't depend
        # on that odc-lib method being present in the deployed image.)
        return self.ch.query(
            f"""
            SELECT kenmerk, scraped_at, record
            FROM {self.source_table}
            WHERE scraped_at > parseDateTime64BestEffort(%(c)s, 3, 'UTC')
              AND status = 'ok'
              AND record != ''
            ORDER BY scraped_at
            LIMIT %(n)s
            """,
            parameters={"c": last_checkpoint, "n": limit},
        ).named_results()

    def process(self, row):
        """Parse one raw_cases row into case + publication + document rows."""
        try:
            rec = json.loads(row["record"])
        except (json.JSONDecodeError, TypeError) as e:
            warn("bad record json", kenmerk=row.get("kenmerk"), err=str(e),
                 **self._log_kwargs())
            return None

        scraped_at = row["scraped_at"]
        kenmerk = rec.get("kenmerk") or row["kenmerk"]
        debtor = rec.get("debtor") or {}
        publications = rec.get("publications") or []
        documents = rec.get("documents") or []

        case = {
            "kenmerk":            kenmerk,
            "insolventienummer":  rec.get("insolventienummer", ""),
            "toezichtzaaknummer": rec.get("toezichtzaaknummer", ""),
            "type":               rec.get("type", ""),
            "court":              rec.get("court", ""),
            "judge":              rec.get("judge", ""),
            "is_anonymized":      bool(rec.get("is_anonymized", False)),
            "debtor_name":        debtor.get("name", ""),
            "kvk_nummer":         debtor.get("kvk_nummer"),
            "city":               _pick_city(debtor.get("addresses") or []),
            "curator_names":      [c.get("name", "") for c in (rec.get("curators") or [])],
            "publication_count":  len(publications),
            "document_count":     len(documents),
            "scraped_at":         scraped_at,
        }

        pub_rows = [{
            "kenmerk":            kenmerk,
            "publicatie_kenmerk": p.get("kenmerk", ""),
            "publicatie_datum":   _to_date(p.get("date")),
            "description":        p.get("description", ""),
            "event_type":         p.get("event_type", ""),
            "event_subtype":      p.get("event_subtype"),
            "event_date":         _to_date(p.get("event_date")),
            "insolvency_type":    p.get("insolvency_type", ""),
            "scraped_at":         scraped_at,
        } for p in publications]

        doc_rows = [{
            "kenmerk":          kenmerk,
            "document_kenmerk": d.get("kenmerk", ""),
            "document_date":    _to_date(d.get("date")),
            "document_type":    d.get("type", ""),
            "pdf_path":         d.get("pdf_path"),
            "scraped_at":       scraped_at,
        } for d in documents]

        return {"case": case, "publications": pub_rows, "documents": doc_rows}

    def write(self, transformed):
        """Fan out the parsed bundles into the three processed_* tables."""
        cases = [t["case"] for t in transformed]
        pubs = [p for t in transformed for p in t["publications"]]
        docs = [d for t in transformed for d in t["documents"]]

        self._insert("insolventies.processed_cases", cases)
        self._insert("insolventies.processed_publications", pubs)
        self._insert("insolventies.processed_documents", docs)
        return len(cases)

    def _insert(self, table, rows):
        if not rows:
            return
        cols = list(rows[0].keys())
        data = [[r[c] for c in cols] for r in rows]
        self.ch.insert(table, data, column_names=cols)

    # ------------------------------------------------------------ enrichment

    # A bankrupt rechtspersoon stays in the Handelsregister until liquidation
    # completes, then vanishes from registry extracts — so KvK data (SBI codes,
    # activity text, trade names) must be SNAPSHOTTED while the case is fresh
    # (2026 cohort joins at ~83%; two-year-old cases at <10%). The anti-join
    # makes this idempotent and append-only: an existing snapshot is never
    # overwritten. See schema/enrichment.sql.
    KVK_SNAPSHOT_SQL = """
    INSERT INTO insolventies.kvk_snapshot
        (kvk_nummer, sbi_hoofd, sbi_codes, sbi_descriptions,
         activiteit_omschrijving, handelsnamen, rechtsvorm, bron)
    WITH new_kvks AS (
        SELECT DISTINCT kvk_nummer FROM insolventies.processed_cases FINAL
        WHERE type IN ('company', 'eenmanszaak')
          AND kvk_nummer IS NOT NULL AND kvk_nummer != ''
          AND kvk_nummer NOT IN (SELECT kvk_nummer FROM insolventies.kvk_snapshot)
    )
    SELECT
        k.kvk_nummer,
        coalesce(s.hoofd_code, ''),
        coalesce(s.codes, []),
        coalesce(s.descriptions, []),
        coalesce(b.act_oms, ''),
        coalesce(b.namen, []),
        coalesce(b.rv, ''),
        'kvk_scrape_2026'
    FROM new_kvks k
    LEFT JOIN (
        SELECT kvk_nummer,
               anyIf(sbi_code, is_main_activity) AS hoofd_code,
               groupArray(sbi_code) AS codes,
               groupArray(sbi_description) AS descriptions
        FROM kvk.sbi
        WHERE kvk_nummer IN (SELECT kvk_nummer FROM new_kvks)
        GROUP BY kvk_nummer
    ) s ON k.kvk_nummer = s.kvk_nummer
    LEFT JOIN (
        SELECT kvk_nummer,
               argMax(activiteit_omschrijving, length(activiteit_omschrijving)) AS act_oms,
               argMax(huidige_handelsnamen, length(activiteit_omschrijving)) AS namen,
               any(rechtsvorm_omschrijving) AS rv
        FROM kvk.businesses
        WHERE kvk_nummer IN (SELECT kvk_nummer FROM new_kvks)
        GROUP BY kvk_nummer
    ) b ON k.kvk_nummer = b.kvk_nummer
    WHERE s.kvk_nummer != '' OR b.kvk_nummer != ''
    """

    def enrich_kvk_snapshot(self):
        self.ch.command(self.KVK_SNAPSHOT_SQL)

    def run(self, argv=None):
        import sys
        rc = super().run(argv)
        args = argv if argv is not None else sys.argv[1:]
        if rc == 0 and "--dry-run" not in args:
            try:
                self.enrich_kvk_snapshot()
            except Exception as e:
                warn("kvk snapshot enrichment failed",
                     err=f"{type(e).__name__}: {e}", **self._log_kwargs())
        return rc


if __name__ == "__main__":
    raise SystemExit(InsolventiesProcessor().run())
