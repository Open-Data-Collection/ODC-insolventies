from __future__ import annotations

import argparse
import io
import json
import logging
import sys

from src.api import ApiClient
from src.discovery import discover_all
from src.scraper import scrape_case, scrape_from_file
from src.storage import push_scrape_tasks

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="ODC Insolventies scraper for Dutch insolvency records",
    )
    parser.add_argument(
        "--phase",
        choices=["discover", "scrape", "all"],
        required=True,
        help="Phase to run: discover, scrape, or all",
    )
    parser.add_argument("--court", help="Single court to search (discover phase)")
    parser.add_argument("--input", help="JSONL file with discovered kenmerks (scrape phase)")
    parser.add_argument("--kenmerk", help="Single kenmerk to scrape (scrape phase)")
    parser.add_argument("--delay", type=float, default=None, help="Delay between requests in seconds")
    parser.add_argument("--no-upload", action="store_true", help="Skip MinIO/Redis upload (output to stdout only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    client = ApiClient(delay=args.delay)
    upload = not args.no_upload
    discovered_cases = None

    if args.phase in ("discover", "all"):
        discovered_cases = run_discover(client, args, upload)

    if args.phase in ("scrape", "all"):
        run_scrape(client, args, upload, discovered_cases=discovered_cases)


def run_discover(client: ApiClient, args: argparse.Namespace, upload: bool):
    courts = [args.court] if args.court else None
    output_buf = io.StringIO()

    cases = discover_all(client, courts=courts, output=output_buf)

    # Always write JSONL to stdout
    content = output_buf.getvalue()
    sys.stdout.write(content)

    # Push discovered kenmerks to task queue
    if upload and cases:
        kenmerks = [c.kenmerk for c in cases]
        push_scrape_tasks(kenmerks)

    return cases


def run_scrape(client: ApiClient, args: argparse.Namespace, upload: bool, discovered_cases=None):
    if args.kenmerk:
        record = scrape_case(client, args.kenmerk, upload=upload)
        if record:
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
    elif args.input:
        scrape_from_file(client, args.input, upload=upload)
    elif discovered_cases:
        # "all" mode: scrape the cases we just discovered
        logger.info("Scraping %d discovered cases", len(discovered_cases))
        for case in discovered_cases:
            scrape_case(client, case.kenmerk, upload=upload)
    else:
        print("Error: --input or --kenmerk required for scrape phase", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
