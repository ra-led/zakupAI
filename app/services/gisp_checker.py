"""Compare a supplier's product characteristics against the GISP catalog.

This module talks to the in-cluster ``gisp-scraper`` microservice (see
``gisp-scraper/`` at the repo root) instead of trying to hit the GISP REST API
directly — the GISP catalog is an Angular SPA without a public JSON API, so
real characteristics can only be obtained via a headless browser.

Flow per item:

1. POST to scraper /pp719/{registry_number}.
   - status="found_actual"  → keep going, we know which product card to scrape.
   - status="found_expired" → don't scrape characteristics; the registry number
     is real but no longer in force; surface a warning.
   - status="not_found"     → return not_found; no characteristics to compare.

2. If active, GET scraper /catalog/{product_id} to get key/value characteristics
   organized by tab.

3. Hand the supplier's chars + GISP's chars to the LLM comparator
   (``compare_characteristics`` in ``llm_tasks``).

4. Roll up to a single status: ok | warning | mismatch | not_found |
   gisp_unavailable | not_actual | skipped.

The scraper URL is configurable via ``GISP_SCRAPER_URL`` (default
``http://gisp-scraper:8000`` for docker-compose). If the scraper itself is
unreachable, we degrade gracefully to ``gisp_unavailable``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .llm_tasks import compare_characteristics

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GISP_SCRAPER_URL = os.getenv("GISP_SCRAPER_URL", "http://gisp-scraper:8000").rstrip("/")
SCRAPER_LOOKUP_TIMEOUT = float(os.getenv("GISP_SCRAPER_LOOKUP_TIMEOUT", "30"))
SCRAPER_CATALOG_TIMEOUT = float(os.getenv("GISP_SCRAPER_CATALOG_TIMEOUT", "60"))

# Tabs we consider authoritative for characteristic comparison. Order matters —
# we walk them in priority order. Anything in "Технические характеристики" wins
# over anything in "Описание".
_TECH_TAB_PRIORITY = [
    "Технические характеристики",
    "Описание",
    "Сведения о стандартизации",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class GispResult:
    """What ``check_runner`` writes into a RegimeCheckItem.

    Status semantics (intentionally a subset of what check_runner._compute_overall
    already understands, so we don't have to touch its rollup logic):

        ok                         — every supplier characteristic agrees with GISP
        warning                    — wording/missing_in_gisp issues OR registry entry
                                     is expired (the scraper saw found_expired even
                                     though the local snapshot still considered it
                                     active)
        mismatch                   — at least one numeric/material disagreement
        wrong_registry_suspected   — 3+ characteristics compared and fewer than ~15%
                                     agreed: the КП is almost certainly quoting a
                                     registry number that belongs to a different
                                     product (typo, copy-paste from another item)
        not_found                  — registry number is not in PP-719v2 (or no exact match)
        gisp_unavailable           — scraper or upstream GISP is down; check inconclusive
        skipped                    — supplier didn't provide characteristics; nothing
                                     to compare
    """

    status: str
    gisp_characteristics: list[dict] = field(default_factory=list)
    comparison: list[dict] = field(default_factory=list)
    gisp_url: Optional[str] = None
    product_id: Optional[str] = None
    gisp_product_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_gisp_characteristics(
    registry_number: str,
    product_name: str,
    supplier_characteristics: list[dict],
    client: Optional[httpx.AsyncClient] = None,
) -> GispResult:
    """Look up the product in GISP and compare characteristics with what the supplier sent.

    Caller may pass a shared httpx client; otherwise we create a per-call one.
    Network errors against the scraper degrade to ``gisp_unavailable`` (warning),
    not an exception, so a single check can finish all items in a file.
    """
    if not supplier_characteristics:
        return GispResult(status="skipped")

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=SCRAPER_CATALOG_TIMEOUT)

    try:
        # Step 1: registry lookup
        try:
            lookup = await _scraper_pp719(client, registry_number)
        except _ScraperUnavailable as exc:
            logger.warning("gisp-scraper /pp719 failed for %s: %s", registry_number, exc)
            return GispResult(status="gisp_unavailable")

        if lookup is None or lookup.get("status") == "not_found":
            return GispResult(status="not_found")

        active = lookup.get("active_record") or {}
        gisp_url = active.get("product_gisp_url")
        product_id = active.get("product_gisp_id")
        gisp_product_name = active.get("product_name")

        if lookup.get("status") == "found_expired":
            # Registry entry exists but is no longer in force. The local
            # registry_checker may still have called this number "ok" because
            # its snapshot is stale. Surface as warning with an explicit
            # comparison row so the user sees why.
            return GispResult(
                status="warning",
                gisp_url=gisp_url,
                product_id=product_id,
                comparison=[{
                    "name": "Срок действия записи ПП-719",
                    "supplier_value": "—",
                    "gisp_value": "истёк",
                    "status": "missing_in_gisp",
                    "comment": "Реестровый номер найден в ПП-719v2, но запись не действует",
                }],
            )

        # Step 2: catalog scrape (only if we know which card)
        if not product_id:
            return GispResult(
                status="warning",
                gisp_url=gisp_url,
                comparison=[{
                    "name": "—",
                    "supplier_value": "",
                    "gisp_value": None,
                    "status": "missing_in_gisp",
                    "comment": "Запись ПП-719 найдена, но в ней нет ссылки на каталог ГИСП",
                }],
            )

        try:
            catalog = await _scraper_catalog(client, product_id)
        except _ScraperUnavailable as exc:
            logger.warning("gisp-scraper /catalog failed for %s: %s", product_id, exc)
            return GispResult(
                status="gisp_unavailable",
                gisp_url=gisp_url,
                product_id=product_id,
            )

        gisp_chars = _select_characteristics(catalog or {})

        if not gisp_chars:
            return GispResult(
                status="warning",
                gisp_characteristics=[],
                gisp_url=gisp_url,
                product_id=product_id,
                comparison=[
                    {
                        "name": c.get("name", ""),
                        "supplier_value": c.get("value", ""),
                        "gisp_value": None,
                        "status": "missing_in_gisp",
                        "comment": "Карточка ГИСП не содержит структурированных характеристик",
                    }
                    for c in supplier_characteristics
                ],
            )

        # Step 3: LLM comparison (existing function — model swappable via env)
        comparison = await compare_characteristics(
            supplier_chars=supplier_characteristics,
            gisp_chars=gisp_chars,
            product_name=product_name,
        )

        status = _rollup(comparison)
        # Heuristic: if we compared at least 3 characteristics and fewer than
        # ~15% of them agreed, the registry number likely points at a completely
        # different product (the classic case: supplier put a thermal-insulation
        # registry number on a PC). Promote status so the UI can call it out
        # instead of burying the signal in "несоответствие характеристик".
        if _looks_like_wrong_registry(comparison):
            status = "wrong_registry_suspected"
            comparison = _prepend_wrong_registry_note(
                comparison, product_name, gisp_product_name
            )

        return GispResult(
            status=status,
            gisp_characteristics=gisp_chars,
            comparison=comparison,
            gisp_url=gisp_url,
            product_id=product_id,
            gisp_product_name=gisp_product_name,
        )
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Scraper transport
# ---------------------------------------------------------------------------


class _ScraperUnavailable(Exception):
    """Raised when the gisp-scraper microservice can't be reached or responds non-OK."""


async def _scraper_pp719(client: httpx.AsyncClient, registry_number: str) -> Optional[dict[str, Any]]:
    url = f"{GISP_SCRAPER_URL}/pp719/{registry_number.strip()}"
    try:
        resp = await client.get(url, timeout=SCRAPER_LOOKUP_TIMEOUT)
    except httpx.RequestError as exc:
        raise _ScraperUnavailable(f"connection error: {exc}")

    if resp.status_code == 404:
        # Scraper itself returned 404 — registry-not-found, not transport failure
        return {"status": "not_found"}
    if resp.status_code == 400:
        # We sent a malformed registry number; treat as not_found, not unavailable
        return {"status": "not_found"}
    if resp.status_code >= 500:
        raise _ScraperUnavailable(f"upstream HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise _ScraperUnavailable(f"unexpected HTTP {resp.status_code}")

    try:
        return resp.json()
    except ValueError as exc:
        raise _ScraperUnavailable(f"non-JSON response: {exc}")


async def _scraper_catalog(client: httpx.AsyncClient, product_id: str) -> Optional[dict[str, Any]]:
    url = f"{GISP_SCRAPER_URL}/catalog/{product_id}"
    try:
        resp = await client.get(url, timeout=SCRAPER_CATALOG_TIMEOUT)
    except httpx.RequestError as exc:
        raise _ScraperUnavailable(f"connection error: {exc}")

    if resp.status_code >= 500:
        raise _ScraperUnavailable(f"upstream HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise _ScraperUnavailable(f"unexpected HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise _ScraperUnavailable(f"non-JSON response: {exc}")

    # Scraper returns 200 even when Selenium crashed mid-scrape (see incident
    # 2026-04-19, product_id=4053367). The `error` field is the authoritative
    # signal; treat it as transport failure so the checker surfaces
    # gisp_unavailable (retryable) instead of falsely claiming the card is empty.
    err = payload.get("error")
    if err:
        attempts = payload.get("attempts", 1)
        raise _ScraperUnavailable(f"scraper error after {attempts} attempt(s): {err}")
    return payload


# ---------------------------------------------------------------------------
# Catalog → flat characteristic list
# ---------------------------------------------------------------------------


def _select_characteristics(catalog: dict[str, Any]) -> list[dict]:
    """Pick the most useful characteristic set out of the scraper's by_tab map.

    GISP catalog cards usually have one tab with technical specs (Высота,
    Ширина, Цвет, …) and one with descriptive marketing copy. The technical
    tab is what the LLM should compare against. If the technical tab is
    missing, fall back to the flat union.
    """
    by_tab = catalog.get("by_tab") or {}

    for preferred in _TECH_TAB_PRIORITY:
        chars = by_tab.get(preferred)
        if chars:
            return [{"name": str(k), "value": str(v)} for k, v in chars.items()]

    flat = catalog.get("flat") or {}
    return [{"name": str(k), "value": str(v)} for k, v in flat.items()]


# ---------------------------------------------------------------------------
# Comparison → single status
# ---------------------------------------------------------------------------


def _rollup(comparison: list[dict]) -> str:
    """Reduce per-characteristic statuses to one item-level status."""
    statuses = {c.get("status") for c in comparison}
    if "mismatch" in statuses:
        return "mismatch"
    if "wording" in statuses or "missing_in_gisp" in statuses:
        return "warning"
    return "ok"


_WRONG_REGISTRY_MATCH_RATIO = 0.15
_WRONG_REGISTRY_MIN_ROWS = 3


def _looks_like_wrong_registry(comparison: list[dict]) -> bool:
    """True when almost nothing agreed — almost certainly a mis-quoted registry number.

    We only fire when at least _WRONG_REGISTRY_MIN_ROWS characteristics were
    compared so tiny catalogs (1-2 rows) don't produce false positives.
    """
    if len(comparison) < _WRONG_REGISTRY_MIN_ROWS:
        return False
    ok_count = sum(
        1 for c in comparison if c.get("status") in ("ok", "wording")
    )
    return (ok_count / len(comparison)) < _WRONG_REGISTRY_MATCH_RATIO


def _prepend_wrong_registry_note(
    comparison: list[dict], kp_name: Optional[str], gisp_name: Optional[str],
) -> list[dict]:
    """Stick a diagnostic row at the top of the comparison list.

    Shown in the expandable details so the user sees why we flagged the item —
    "you uploaded Моноблок but that registry number belongs to Теплоизоляция".
    """
    note = {
        "name": "Проверка товара",
        "supplier_value": kp_name or "—",
        "gisp_value": gisp_name or "—",
        "status": "mismatch",
        "comment": "Ни одна характеристика не совпала с карточкой ГИСП — "
                   "вероятно, указан неверный реестровый номер",
    }
    return [note] + comparison
