"""Command line entry point for locating supplier contacts."""

from __future__ import annotations

import argparse
import os
import sys

from pydantic import ValidationError

from .contact_finder import ContactFinder
from .models import ContactSearchInput
from .search_client import YandexSearchClient


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    parser = argparse.ArgumentParser(description="Find supplier contacts via Yandex search")
    parser.add_argument("product_name", help="Product name to search for")
    parser.add_argument("description", help="Product description or keywords")
    parser.add_argument(
        "--region",
        type=int,
        default=213,
        help="Yandex region identifier (default: 213 for Moscow)",
    )
    parser.add_argument("--page", type=int, default=0, help="Search results page to request")
    parser.add_argument(
        "--minimum-contacts",
        type=int,
        default=5,
        help="Minimum number of contact pairs (email + phone) to collect",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)

    iam_token = os.getenv("YANDEX_SEARCH_IAM_TOKEN")
    folder_id = os.getenv("YANDEX_SEARCH_FOLDER_ID")
    if not iam_token or not folder_id:
        parser.error(
            "Environment variables YANDEX_SEARCH_IAM_TOKEN and YANDEX_SEARCH_FOLDER_ID are required."
        )

    try:
        query = ContactSearchInput(
            product_name=args.product_name,
            description=args.description,
            region=args.region,
            page=args.page,
        )
    except ValidationError as exc:
        parser.error(f"Invalid input: {exc}")

    search_client = YandexSearchClient(iam_token=iam_token, folder_id=folder_id)
    finder = ContactFinder(search_client, minimum_contacts=args.minimum_contacts)
    result = finder.find_contacts(query)

    if not result.contacts:
        print("No contacts found.")
        return 1

    for contact in result.contacts:
        print(f"Email: {contact.email}\tPhone: {contact.phone}\tSource: {contact.source_url}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
