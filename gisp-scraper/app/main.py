"""GISP scraper microservice for zakupAI Нацрежим (M4).

Two responsibilities:

1. Look up a product registry number in the PP-719v2 registry
   (gisp.gov.ru/pp719v2/pub/prod/b/) and decide whether the number is valid
   and currently in force. Done with a single HTTP POST, no browser.

2. Fetch product characteristics from the GISP catalog
   (gisp.gov.ru/goods/#/product/{id}). The catalog is an Angular SPA with
   no public JSON API, so this requires a real headless browser. We use
   Selenium with Chromium managed by Selenium Manager (no separate hub).

Endpoints:

    GET /pp719/{registry_number}
        Fast registry lookup. Returns the active record (or expired one if
        nothing is current), all matching records, and a stable status
        ("found_actual" | "found_expired" | "not_found").

    GET /catalog/{product_id}
        Heavy. Walks every tab in the GISP catalog card and parses every
        .product-characteristic key/value row. Returns by_tab + flat dicts.

    GET /details/{registry_number}
        Backward-compat shim that does both steps in one call.

    GET /health
        Liveness probe.

Concurrency: catalog scraping is serialized to GISP_MAX_CONCURRENT (default 3)
to keep memory under the container limit. Registry lookups are not throttled —
they are cheap HTTP calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger("gisp_scraper")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PP719_API_URL = "https://gisp.gov.ru/pp719v2/pub/prod/b/"
# РЭП (Реестр радиоэлектронной продукции) — undocumented, derived from the
# ГИСП UI URL https://gisp.gov.ru/pp719v2/pub/prod/rep . Same JSON shape as
# PP-719v2; carries an additional level field. If ГИСП moves it, override
# via the REP_API_URL env var.
REP_API_URL = os.getenv(
    "REP_API_URL", "https://gisp.gov.ru/pp719v2/pub/prod/rep/b/"
)
CATALOG_URL_TEMPLATE = "https://gisp.gov.ru/goods/#/product/{product_id}"

# Browser concurrency cap. Each headless Chromium ~250 MB peak.
# Default 3 → ~750 MB peak when fully loaded, fits comfortably in a 1 GB limit.
GISP_MAX_CONCURRENT = int(os.getenv("GISP_MAX_CONCURRENT", "3"))

# Per-call timeouts
PP719_HTTP_TIMEOUT = float(os.getenv("PP719_HTTP_TIMEOUT", "30"))
CATALOG_PAGE_TIMEOUT = float(os.getenv("CATALOG_PAGE_TIMEOUT", "30"))

# Selenium semaphore — initialized in lifespan so it binds to the right loop
_browser_semaphore: Optional[asyncio.Semaphore] = None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class Pp719Record(BaseModel):
    """One row from the PP-719v2 registry response."""

    product_reg_number_2023: Optional[str] = None
    product_reg_number_2022: Optional[str] = None
    product_name: Optional[str] = None
    org_name: Optional[str] = None
    org_inn: Optional[str] = None
    org_ogrn: Optional[str] = None
    product_okpd2: Optional[str] = None
    product_tnved: Optional[str] = None
    product_spec: Optional[str] = None
    res_date: Optional[str] = None
    res_valid_till: Optional[str] = None
    res_end_date: Optional[str] = None
    res_number: Optional[str] = None
    res_scan_url: Optional[str] = None
    product_score_value: Optional[float] = None
    product_percentage: Optional[float] = None
    product_score_desc: Optional[str] = None
    is_ai_tpp: Optional[bool] = None
    high_tech: Optional[bool] = None
    is_pak: Optional[bool] = None
    product_gisp_url: Optional[str] = None
    product_gisp_id: Optional[str] = None  # extracted from product_gisp_url
    # РЭП-only field: «Уровень 1» / «Уровень 2» / «Нет уровня». Always None
    # for РРПП (PP-719) records. We expose it on the shared schema because
    # both registries share the same row shape in ГИСП's response.
    product_level: Optional[str] = None


class Pp719LookupResponse(BaseModel):
    registry_number: str
    status: str  # found_actual | found_expired | not_found
    matched_count: int
    active_record: Optional[Pp719Record] = None
    all_records: List[Pp719Record] = []


class CatalogResponse(BaseModel):
    product_id: str
    url: str
    tabs_seen: List[str] = []
    by_tab: Dict[str, Dict[str, str]] = {}
    flat: Dict[str, str] = {}
    warnings: List[str] = []
    # Set when the underlying Selenium scrape failed (driver crash, page-load
    # timeout, stale DOM). Callers should treat by_tab as *unreliable* and
    # prefer to retry rather than show the user "card has no characteristics".
    error: Optional[str] = None
    attempts: int = 1


# ---------------------------------------------------------------------------
# PP-719v2 registry lookup
# ---------------------------------------------------------------------------

_REGISTRY_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://gisp.gov.ru",
    "Referer": "https://gisp.gov.ru/pp719v2/pub/prod/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _normalize_registry_number(raw: str) -> str:
    """Strip everything but digits. PP-719v2 numbers are always digits.

    Accepts inputs like '10085920', 'РПП-10085920', '№ 10085920', '10085920 '.
    """
    return re.sub(r"\D", "", raw or "")


_PRODUCT_ID_RE = re.compile(r"/product/(\d+)")


def _extract_product_id(gisp_url: Optional[str]) -> Optional[str]:
    """product_gisp_url looks like 'https://gisp.gov.ru/goods/#/product/1769855'."""
    if not gisp_url:
        return None
    m = _PRODUCT_ID_RE.search(gisp_url)
    return m.group(1) if m else None


def _fetch_registry_records_sync(api_url: str, registry_number: str) -> List[Dict[str, Any]]:
    """POST to a ГИСП grid endpoint via the stdlib urllib.

    Used by both /pp719/ and /rep/ — the request shape is identical, only
    the URL differs.

    Why urllib instead of httpx: empirically, httpx's POST against ГИСП's
    grid endpoints hangs forever on read inside the deployed container,
    while urllib's request returns in ~120 ms with the same headers and
    body. We wrap this sync function with run_in_executor to keep FastAPI's
    event loop unblocked.
    """
    payload = {
        "opt": {
            "filter": ["product_reg_number_2023", "contains", registry_number]
        }
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=body,
        headers=_REGISTRY_HEADERS,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PP719_HTTP_TIMEOUT) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        logger.warning("Registry POST %s returned HTTP %s", api_url, exc.code)
        raise HTTPException(status_code=502, detail=f"GISP returned HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Registry POST %s failed: %s", api_url, exc)
        raise HTTPException(status_code=503, detail=f"GISP unreachable: {exc}")

    if status != 200:
        raise HTTPException(status_code=502, detail=f"GISP returned HTTP {status}")

    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=502, detail="GISP returned non-JSON body")

    items = data.get("items") if isinstance(data, dict) else None
    return items or []


async def _fetch_pp719_records(registry_number: str) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _fetch_registry_records_sync, PP719_API_URL, registry_number
    )


async def _fetch_rep_records(registry_number: str) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _fetch_registry_records_sync, REP_API_URL, registry_number
    )


def _select_active_record(
    items: List[Dict[str, Any]], requested_number: str
) -> tuple[str, Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Apply exact match + actuality logic.

    Returns: (status, active_record_or_none, all_exact_matches)

    Status semantics:
        not_found     — no item has product_reg_number_2023 exactly equal to
                        requested_number (so 'contains' matches with longer
                        numbers are filtered out).
        found_expired — there are exact matches but every one is past its
                        res_valid_till or has res_end_date set.
        found_actual  — at least one exact match is currently in force.

    When multiple records are valid, the one with the most recent res_date wins.
    """
    today = date.today().isoformat()

    exact = [
        it for it in items
        if (it.get("product_reg_number_2023") or "") == requested_number
    ]
    if not exact:
        return "not_found", None, []

    def is_active(rec: Dict[str, Any]) -> bool:
        end = rec.get("res_end_date")
        if end and end <= today:
            return False
        valid_till = rec.get("res_valid_till")
        if valid_till and valid_till < today:
            return False
        return True

    active = [it for it in exact if is_active(it)]

    def res_date_sort_key(rec: Dict[str, Any]) -> str:
        return rec.get("res_date") or ""

    if active:
        best = max(active, key=res_date_sort_key)
        return "found_actual", best, exact

    # Exact matches exist but all expired — return the freshest expired one
    best_expired = max(exact, key=res_date_sort_key)
    return "found_expired", best_expired, exact


def _extract_product_level(raw: Dict[str, Any]) -> Optional[str]:
    """Pull the РЭП level value out of the raw item dict.

    Field name in ГИСП's response is undocumented. We probe the most likely
    keys and accept any string value that looks like «Уровень N» or «Нет уровня».
    """
    for key in (
        "product_level",
        "rep_level",
        "level",
        "product_rep_level",
        "rel_level_value",
    ):
        val = raw.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            return s
    return None


def _record_to_model(raw: Dict[str, Any]) -> Pp719Record:
    """Pluck the fields we publish out of the raw GISP item dict."""
    return Pp719Record(
        product_reg_number_2023=raw.get("product_reg_number_2023"),
        product_reg_number_2022=raw.get("product_reg_number_2022"),
        product_name=raw.get("product_name"),
        org_name=raw.get("org_name"),
        org_inn=raw.get("org_inn"),
        org_ogrn=raw.get("org_ogrn"),
        product_okpd2=raw.get("product_okpd2"),
        product_tnved=raw.get("product_tnved"),
        product_spec=raw.get("product_spec"),
        res_date=raw.get("res_date"),
        res_valid_till=raw.get("res_valid_till"),
        res_end_date=raw.get("res_end_date"),
        res_number=raw.get("res_number"),
        res_scan_url=raw.get("res_scan_url"),
        product_score_value=raw.get("product_score_value"),
        product_percentage=raw.get("product_percentage"),
        product_score_desc=raw.get("product_score_desc"),
        is_ai_tpp=raw.get("is_ai_tpp"),
        high_tech=raw.get("high_tech"),
        is_pak=raw.get("is_pak"),
        product_gisp_url=raw.get("product_gisp_url"),
        product_gisp_id=_extract_product_id(raw.get("product_gisp_url")),
        product_level=_extract_product_level(raw),
    )


# ---------------------------------------------------------------------------
# Catalog scraping (Selenium)
# ---------------------------------------------------------------------------


def _build_driver() -> webdriver.Chrome:
    """Build a fresh headless Chromium. Selenium Manager auto-resolves the binary."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-plugins")
    # Memory hygiene — Chromium spawns helper processes that we don't need
    opts.add_argument("--no-zygote")
    opts.add_argument("--single-process")
    opts.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


def _parse_active_pane(html: str) -> Dict[str, str]:
    """Parse .product-characteristic rows scoped to the active tab pane only.

    Scoping to .ant-tabs-tabpane-active prevents picking up rows that the
    previous (now-inactive) tab may have left in the DOM.
    """
    soup = BeautifulSoup(html, "html.parser")
    active = soup.find(class_="ant-tabs-tabpane-active")
    if not active:
        return {}
    out: Dict[str, str] = {}
    for row in active.find_all(class_="product-characteristic"):
        name_el = row.find(class_="product-characteristic__name")
        value_el = row.find(class_="product-characteristic__value")
        if not name_el or not value_el:
            continue
        name = name_el.get_text(" ", strip=True)
        val = value_el.get_text(" ", strip=True)
        if name:
            out[name] = val
    return out


def _expand_collapsibles(driver) -> int:
    """Expand any 'Дополнительные характеристики' / accordion sections in the active pane.

    For most products the rows are already in the DOM (just visually hidden), but
    some catalog cards mount them lazily. We click any collapsed expander.
    """
    clicked = 0
    selectors = [
        ".ant-tabs-tabpane-active .ant-collapse-header",
        ".ant-tabs-tabpane-active [aria-expanded='false']",
    ]
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
        except WebDriverException:
            continue
        for el in els:
            try:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    clicked += 1
            except StaleElementReferenceException:
                continue
            except WebDriverException:
                continue
    if clicked:
        time.sleep(0.4)  # short settle for accordion animation
    return clicked


def _wait_pane_settled(driver, prev_count: int, timeout: float = 6.0) -> int:
    """Wait until characteristic count in the active pane changes and stays put.

    Replaces a fixed time.sleep — finishes as soon as Angular re-renders the new tab.
    """
    end = time.monotonic() + timeout
    last = -1
    stable_since: Optional[float] = None
    while time.monotonic() < end:
        try:
            cnt = len(driver.find_elements(
                By.CSS_SELECTOR, ".ant-tabs-tabpane-active .product-characteristic"
            ))
        except WebDriverException:
            cnt = -1
        if cnt != prev_count and cnt == last:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 0.6:
                return cnt
        else:
            stable_since = None
            last = cnt
        time.sleep(0.15)
    return last if last >= 0 else 0


def _scrape_catalog_sync(product_id: str) -> Dict[str, Any]:
    """Blocking implementation. Wrap with run_in_executor when calling from async."""
    url = CATALOG_URL_TEMPLATE.format(product_id=product_id)
    driver = None
    warnings: List[str] = []
    try:
        driver = _build_driver()
        driver.get(url)
        wait = WebDriverWait(driver, CATALOG_PAGE_TIMEOUT)

        try:
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "ant-tabs-tab")))
        except TimeoutException:
            return {
                "product_id": product_id,
                "url": url,
                "tabs_seen": [],
                "by_tab": {},
                "flat": {},
                "warnings": [f"GISP catalog tabs did not appear within {CATALOG_PAGE_TIMEOUT}s"],
                "error": "tabs_not_loaded",
            }

        try:
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".ant-tabs-tabpane-active .product-characteristic")
            ))
        except TimeoutException:
            warnings.append("no .product-characteristic on initial render")

        # Snapshot tab names up front; elements may go stale during clicks
        tab_elements = driver.find_elements(By.CLASS_NAME, "ant-tabs-tab")
        tab_names: List[str] = []
        for el in tab_elements:
            try:
                tab_names.append((el.text or "").strip())
            except WebDriverException:
                tab_names.append("")

        by_tab: Dict[str, Dict[str, str]] = {}

        for idx, name in enumerate(tab_names):
            if not name:
                continue
            try:
                tabs_now = driver.find_elements(By.CLASS_NAME, "ant-tabs-tab")
                if idx >= len(tabs_now):
                    warnings.append(f"tab[{idx}] '{name}' disappeared")
                    continue
                target = tabs_now[idx]
                prev_count = len(driver.find_elements(
                    By.CSS_SELECTOR, ".ant-tabs-tabpane-active .product-characteristic"
                ))
                driver.execute_script("arguments[0].click();", target)
            except WebDriverException as exc:
                warnings.append(f"click on '{name}' failed: {exc}")
                continue

            _wait_pane_settled(driver, prev_count, timeout=5.0)
            if _expand_collapsibles(driver):
                time.sleep(0.3)

            chars = _parse_active_pane(driver.page_source)
            if chars:
                by_tab[name] = chars

        flat: Dict[str, str] = {}
        for tab_name, chars in by_tab.items():
            for k, v in chars.items():
                if k in flat and flat[k] != v:
                    flat[f"{tab_name} / {k}"] = v
                elif k not in flat:
                    flat[k] = v

        return {
            "product_id": product_id,
            "url": url,
            "tabs_seen": [t for t in tab_names if t],
            "by_tab": by_tab,
            "flat": flat,
            "warnings": warnings,
        }
    except WebDriverException as exc:
        logger.warning("Selenium failed for product_id=%s: %s", product_id, exc)
        return {
            "product_id": product_id,
            "url": url,
            "tabs_seen": [],
            "by_tab": {},
            "flat": {},
            "warnings": warnings,
            "error": f"selenium_error: {exc}",
        }
    finally:
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                pass


CATALOG_RETRY_ATTEMPTS = int(os.getenv("GISP_CATALOG_RETRY_ATTEMPTS", "2"))


async def _scrape_catalog(product_id: str) -> Dict[str, Any]:
    """Throttled async wrapper around the blocking scraper.

    Retries once when Selenium crashes (``invalid session id`` / render
    disconnects) — these are transient, cost us only the latency of one extra
    page load, and turn a silent 200+empty response into a recovered success
    most of the time. See incident 2026-04-19 with product_id=4053367 for the
    motivation.
    """
    assert _browser_semaphore is not None, "lifespan must initialize the semaphore"
    loop = asyncio.get_running_loop()
    last_result: Dict[str, Any] = {}
    for attempt in range(1, CATALOG_RETRY_ATTEMPTS + 1):
        async with _browser_semaphore:
            result = await loop.run_in_executor(None, _scrape_catalog_sync, product_id)
        result["attempts"] = attempt
        # A scrape is "good" if Selenium didn't explode AND we got at least
        # one tab with data. Anything else is retried.
        if not result.get("error") and result.get("by_tab"):
            return result
        last_result = result
        if attempt < CATALOG_RETRY_ATTEMPTS:
            logger.warning(
                "Retrying catalog scrape product_id=%s (attempt %d/%d, reason=%s)",
                product_id, attempt, CATALOG_RETRY_ATTEMPTS,
                result.get("error") or "empty_by_tab",
            )
    return last_result


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser_semaphore
    _browser_semaphore = asyncio.Semaphore(GISP_MAX_CONCURRENT)
    logger.info(
        "gisp-scraper starting: max_concurrent=%d, page_timeout=%.0fs",
        GISP_MAX_CONCURRENT, CATALOG_PAGE_TIMEOUT,
    )
    yield


app = FastAPI(title="GISP scraper for zakupAI Нацрежим", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "max_concurrent": GISP_MAX_CONCURRENT,
        "available_browser_slots": (
            _browser_semaphore._value if _browser_semaphore is not None else None
        ),
    }


@app.get("/pp719/{registry_number}", response_model=Pp719LookupResponse)
async def lookup_pp719(registry_number: str) -> Pp719LookupResponse:
    """Look up an exact registry number in the PP-719v2 registry."""
    clean = _normalize_registry_number(registry_number)
    if not clean:
        raise HTTPException(status_code=400, detail="registry_number must contain digits")

    raw_items = await _fetch_pp719_records(clean)
    status, active, exact_matches = _select_active_record(raw_items, clean)

    return Pp719LookupResponse(
        registry_number=clean,
        status=status,
        matched_count=len(exact_matches),
        active_record=_record_to_model(active) if active else None,
        all_records=[_record_to_model(it) for it in exact_matches],
    )


@app.get("/rep/{registry_number}", response_model=Pp719LookupResponse)
async def lookup_rep(registry_number: str) -> Pp719LookupResponse:
    """Look up an exact registry number in the РЭП (Реестр радиоэлектронной продукции).

    Same response shape as /pp719 — the only meaningful difference for the
    caller is that ``active_record.product_level`` is populated for РЭП
    records (it stays None for РРПП). If ГИСП moved the РЭП endpoint, set
    REP_API_URL env var.
    """
    clean = _normalize_registry_number(registry_number)
    if not clean:
        raise HTTPException(status_code=400, detail="registry_number must contain digits")

    raw_items = await _fetch_rep_records(clean)
    status, active, exact_matches = _select_active_record(raw_items, clean)

    return Pp719LookupResponse(
        registry_number=clean,
        status=status,
        matched_count=len(exact_matches),
        active_record=_record_to_model(active) if active else None,
        all_records=[_record_to_model(it) for it in exact_matches],
    )


@app.get("/catalog/{product_id}", response_model=CatalogResponse)
async def fetch_catalog(product_id: str) -> CatalogResponse:
    """Scrape characteristics for a single product card."""
    if not product_id.isdigit():
        raise HTTPException(status_code=400, detail="product_id must be numeric")
    result = await _scrape_catalog(product_id)
    return CatalogResponse(
        product_id=result["product_id"],
        url=result["url"],
        tabs_seen=result.get("tabs_seen", []),
        by_tab=result.get("by_tab", {}),
        flat=result.get("flat", {}),
        warnings=result.get("warnings", []),
        error=result.get("error"),
        attempts=int(result.get("attempts", 1)),
    )


@app.get("/details/{registry_number}")
async def get_details(registry_number: str) -> Dict[str, Any]:
    """Backward-compat: lookup + catalog in one call.

    Useful for ad-hoc testing. Production callers in zakupAI use /pp719 and /catalog
    separately so they can decide whether to spend a Selenium slot.
    """
    pp = await lookup_pp719(registry_number)
    payload: Dict[str, Any] = {
        "registry_number": pp.registry_number,
        "status": pp.status,
        "matched_count": pp.matched_count,
        "active_record": pp.active_record.model_dump() if pp.active_record else None,
        "characteristics": None,
    }
    if pp.active_record and pp.active_record.product_gisp_id:
        cat = await fetch_catalog(pp.active_record.product_gisp_id)
        payload["characteristics"] = cat.model_dump()
    return payload
