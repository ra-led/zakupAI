"""Utilities for extracting contact details from HTML content."""

from __future__ import annotations

import re
from typing import Iterable, Set

from bs4 import BeautifulSoup

EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_REGEX = re.compile(r"(?:\+?\d[\s\-().]?){7,}\d")


def _normalize_phone(raw_phone: str) -> str | None:
    """Clean and normalize phone numbers to keep digits and leading plus sign."""

    digits = re.sub(r"[^0-9+]", "", raw_phone)
    digits = digits.replace("++", "+")
    digit_count = len(re.sub(r"\D", "", digits))
    if digit_count < 10 or digit_count > 15:
        return None
    return digits


def extract_emails(text: str) -> Set[str]:
    """Extract unique email addresses from text."""

    return {match.group(0).lower() for match in EMAIL_REGEX.finditer(text)}


def extract_phones(text: str) -> Set[str]:
    """Extract normalized phone numbers from text."""

    phones: Set[str] = set()
    for match in PHONE_REGEX.finditer(text):
        normalized = _normalize_phone(match.group(0))
        if normalized:
            phones.add(normalized)
    return phones


def extract_contacts_from_html(html_text: str) -> tuple[Set[str], Set[str]]:
    """Parse HTML text and extract emails and phone numbers."""

    soup = BeautifulSoup(html_text, "lxml")
    text_content = soup.get_text(" ", strip=True)

    # Include mailto links even if not present in visible text.
    mailto_links = {a.get("href", "") for a in soup.find_all("a", href=True)}
    mailto_emails = {
        href.removeprefix("mailto:").strip()
        for href in mailto_links
        if href.startswith("mailto:")
    }

    emails = extract_emails(text_content) | extract_emails(" ".join(mailto_emails))
    phones = extract_phones(text_content)
    return emails, phones


def pair_contacts(emails: Iterable[str], phones: Iterable[str]) -> list[tuple[str, str]]:
    """Pair emails and phones to maximize combinations with unique pairs."""

    email_list = list(dict.fromkeys(emails))
    phone_list = list(dict.fromkeys(phones))
    pairs: list[tuple[str, str]] = []

    for email in email_list:
        for phone in phone_list:
            pairs.append((email, phone))
    return pairs
