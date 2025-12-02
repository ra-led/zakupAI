"""Unit tests for contact extraction and finder orchestration."""

from __future__ import annotations

from zakupai.aggregators import is_aggregator
from zakupai.contact_extraction import extract_contacts_from_html, pair_contacts
from zakupai.contact_finder import ContactFinder
from zakupai.models import ContactSearchInput, SearchResult


class FakeSearchClient:
    """Stub Yandex search client returning predefined results."""

    def __init__(self, results: list[SearchResult]):
        self._results = results

    def search(self, query: ContactSearchInput) -> list[SearchResult]:
        return self._results


def test_aggregator_detection() -> None:
    assert is_aggregator("https://www.amazon.com/product/123")
    assert not is_aggregator("https://supplier.example.com")


def test_extract_contacts_from_html() -> None:
    html = (
        "<html><body>Contact us at sales@example.com or +1 (555) 123-4567"
        "<a href='mailto:info@example.com'>Email</a></body></html>"
    )
    emails, phones = extract_contacts_from_html(html)
    assert "sales@example.com" in emails
    assert "info@example.com" in emails
    assert "+15551234567" in phones

    pairs = pair_contacts(emails, phones)
    assert ("sales@example.com", "+15551234567") in pairs


def test_contact_finder_skips_aggregators_and_collects_contacts() -> None:
    results = [
        SearchResult(title="Marketplace", url="https://amazon.com/product/123"),
        SearchResult(title="Supplier", url="https://supplier.example.com/contact"),
    ]
    finder = ContactFinder(FakeSearchClient(results), minimum_contacts=2)
    finder._fetch_page = lambda url: (
        "<html>sales@example.com +1 555 123 4567 support@example.com +1 555 987 6543</html>"
    )

    output = finder.find_contacts(
        ContactSearchInput(product_name="Widget", description="industrial")
    )

    assert output.skipped_urls[0].startswith("https://amazon.com")
    assert len(output.contacts) >= 2
