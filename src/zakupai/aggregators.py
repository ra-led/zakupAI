"""Utilities for filtering out aggregator and marketplace domains."""

from __future__ import annotations

from urllib.parse import urlparse

AGGREGATOR_KEYWORDS = {
    "alibaba",
    "aliexpress",
    "amazon",
    "ebay",
    "etsy",
    "market.yandex",
    "market",
    "wildberries",
    "ozon",
    "leroymerlin",
    "mercadolibre",
    "rakuten",
    "flipkart",
    "etsy",
    "cdiscount",
    "shopify",
    "allegro",
    "daraz",
    "indiamart",
    "walmart",
    "target",
    "bestbuy",
    "ebay-kleinanzeigen",
    "avito",
}


def is_aggregator(url: str) -> bool:
    """Return True if the url looks like an aggregator or marketplace domain."""

    hostname = urlparse(url).hostname or ""
    normalized = hostname.lower()
    return any(keyword in normalized for keyword in AGGREGATOR_KEYWORDS)
