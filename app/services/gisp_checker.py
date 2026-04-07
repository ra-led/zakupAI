"""Check product characteristics against GISP catalog (gisp.gov.ru)."""
import os
import httpx
import re
from dataclasses import dataclass, field
from typing import Optional
from .llm_client import compare_characteristics


GISP_CATALOG_SEARCH = "https://gisp.gov.ru/products/api/v1/products/"
GISP_PRODUCT_URL = "https://gisp.gov.ru/products/api/v1/products/{id}/"


@dataclass
class GispResult:
    status: str  # ok | mismatch | warning | skipped | not_found | gisp_unavailable
    gisp_characteristics: list[dict] = field(default_factory=list)
    comparison: list[dict] = field(default_factory=list)
    gisp_url: Optional[str] = None
    product_id: Optional[str] = None


async def check_gisp_characteristics(
    registry_number: str,
    product_name: str,
    supplier_characteristics: list[dict],
    client: Optional[httpx.AsyncClient] = None,
) -> GispResult:
    """
    Find product in GISP catalog by registry number and compare characteristics.
    Accepts optional shared httpx client.
    """
    if not supplier_characteristics:
        return GispResult(status="skipped")

    try:
        gisp_product = await _fetch_gisp_product(registry_number, product_name, client)
    except _GispUnavailable:
        return GispResult(status="gisp_unavailable")

    if not gisp_product:
        return GispResult(status="not_found")

    gisp_chars = _extract_characteristics(gisp_product)
    product_id = str(gisp_product.get("id", ""))
    gisp_url = f"https://gisp.gov.ru/products/{product_id}/" if product_id else None

    if not gisp_chars:
        # GISP card exists but no characteristics filled
        return GispResult(
            status="warning",
            gisp_characteristics=[],
            comparison=[
                {
                    "name": c.get("name", ""),
                    "supplier_value": c.get("value", ""),
                    "gisp_value": None,
                    "status": "missing_in_gisp",
                    "comment": "Карточка ГИСП не содержит характеристик",
                }
                for c in supplier_characteristics
            ],
            gisp_url=gisp_url,
            product_id=product_id,
        )

    # Use LLM to compare
    comparison = await compare_characteristics(
        supplier_chars=supplier_characteristics,
        gisp_chars=gisp_chars,
        product_name=product_name,
    )

    # Determine overall status
    statuses = {c.get("status") for c in comparison}
    if "mismatch" in statuses:
        overall = "mismatch"
    elif "wording" in statuses or "missing_in_gisp" in statuses:
        overall = "warning"
    else:
        overall = "ok"

    return GispResult(
        status=overall,
        gisp_characteristics=gisp_chars,
        comparison=comparison,
        gisp_url=gisp_url,
        product_id=product_id,
    )


class _GispUnavailable(Exception):
    """Raised when GISP catalog API is not reachable."""
    pass


async def _fetch_gisp_product(
    registry_number: str,
    product_name: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[dict]:
    """Fetch product from GISP catalog. Raises _GispUnavailable if API is down."""
    clean_number = re.sub(r"[^\d]", "", registry_number or "")

    own_client = client is None
    if own_client:
        proxy = os.getenv("GISP_PROXY_URL") or None
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, proxy=proxy)

    got_any_response = False

    try:
        # Search by registry number
        if clean_number:
            resp = await client.get(
                GISP_CATALOG_SEARCH,
                params={"reg_number": clean_number, "page_size": 5},
                headers={"Accept": "application/json"},
            )
            got_any_response = True
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", data.get("items", []))
                if results:
                    return results[0]
            elif resp.status_code >= 500:
                raise _GispUnavailable(f"GISP returned {resp.status_code}")

        # Fallback: search by name
        if product_name:
            resp2 = await client.get(
                GISP_CATALOG_SEARCH,
                params={"search": product_name[:100], "page_size": 3},
                headers={"Accept": "application/json"},
            )
            got_any_response = True
            if resp2.status_code == 200:
                data2 = resp2.json()
                results2 = data2.get("results", data2.get("items", []))
                if results2:
                    return results2[0]
            elif resp2.status_code >= 500:
                raise _GispUnavailable(f"GISP returned {resp2.status_code}")
    except httpx.RequestError as e:
        raise _GispUnavailable(f"GISP connection error: {e}")
    finally:
        if own_client:
            await client.aclose()

    return None


def _extract_characteristics(product: dict) -> list[dict]:
    """Extract characteristics list from GISP product JSON."""
    chars = []

    # Various field names in GISP API
    for field_name in ("characteristics", "params", "properties", "attributes", "specs"):
        raw = product.get(field_name)
        if isinstance(raw, list) and raw:
            for item in raw:
                if isinstance(item, dict):
                    name = item.get("name", item.get("title", item.get("param_name", "")))
                    value = item.get("value", item.get("val", item.get("param_value", "")))
                    if name:
                        chars.append({"name": str(name), "value": str(value) if value is not None else ""})
            return chars

    return chars
