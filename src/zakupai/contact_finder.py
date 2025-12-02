"""High-level contact finder that coordinates search and extraction."""

from __future__ import annotations

from typing import List

import requests
from requests import Response

from .aggregators import is_aggregator
from .contact_extraction import extract_contacts_from_html, pair_contacts
from .models import ContactSearchInput, ContactSearchResult, SupplierContact
from .search_client import YandexSearchClient

DEFAULT_HEADERS = {"User-Agent": "zakupai-contact-finder/0.1"}


class ContactFinder:
    """Find supplier contacts by combining Yandex search and on-page extraction."""

    def __init__(
        self,
        search_client: YandexSearchClient,
        *,
        http_timeout: int = 20,
        minimum_contacts: int = 5,
    ) -> None:
        self.search_client = search_client
        self.http_timeout = http_timeout
        self.minimum_contacts = minimum_contacts

    def find_contacts(self, query: ContactSearchInput) -> ContactSearchResult:
        """Search and extract at least ``minimum_contacts`` supplier contacts."""

        search_results = self.search_client.search(query)
        collected: List[SupplierContact] = []
        inspected_urls: List[str] = []
        skipped_urls: List[str] = []

        for result in search_results:
            if len(collected) >= self.minimum_contacts:
                break
            if is_aggregator(str(result.url)):
                skipped_urls.append(str(result.url))
                continue

            page_html = self._fetch_page(result.url)
            if page_html is None:
                skipped_urls.append(str(result.url))
                continue

            emails, phones = extract_contacts_from_html(page_html)
            pairs = pair_contacts(emails, phones)
            if not pairs:
                inspected_urls.append(str(result.url))
                continue

            for email, phone in pairs:
                collected.append(
                    SupplierContact(
                        email=email,
                        phone=phone,
                        source_url=result.url,
                        source_title=result.title,
                        notes="Found on supplier page",
                    )
                )
                if len(collected) >= self.minimum_contacts:
                    break
            inspected_urls.append(str(result.url))

        return ContactSearchResult(
            query=query,
            contacts=collected,
            inspected_urls=inspected_urls,
            skipped_urls=skipped_urls,
        )

    def _fetch_page(self, url: str) -> str | None:
        """Fetch raw HTML for a URL with basic error handling."""

        try:
            response: Response = requests.get(url, headers=DEFAULT_HEADERS, timeout=self.http_timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            return None
