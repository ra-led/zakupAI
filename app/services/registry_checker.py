"""Check products against the 719 PP Russian industrial products registry.

Uses local database (loaded from Minpromtorg opendata CSV) instead of GISP API.
"""
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..models import RegistryProduct


@dataclass
class RegistryResult:
    status: str  # ok | not_actual | not_found
    is_actual: Optional[bool] = None
    cert_end_date: Optional[str] = None
    registry_name: Optional[str] = None
    localization_score: Optional[float] = None
    okpd2_from_registry: Optional[str] = None
    url: Optional[str] = None
    raw_data: Optional[dict] = None


def check_registry_number(registry_number: str, db: Session) -> RegistryResult:
    """
    Look up a registry number in the local PP 719 database.
    Synchronous — no HTTP calls needed.
    """
    if not registry_number or not registry_number.strip():
        return RegistryResult(status="not_found")

    # Normalize: strip РПП- prefix, keep only digits
    clean_number = re.sub(r"[^\d]", "", registry_number)
    if not clean_number:
        return RegistryResult(status="not_found")

    # Search by registry_number (exact match on digits)
    products = db.query(RegistryProduct).filter(
        RegistryProduct.registry_number == clean_number
    ).all()

    if not products:
        # Try original string (in case registry_number has non-digit chars in DB)
        products = db.query(RegistryProduct).filter(
            RegistryProduct.registry_number == registry_number.strip()
        ).all()

    if not products:
        return RegistryResult(status="not_found")

    # If multiple entries for same registry number, pick the most recent valid one
    best = _pick_best_entry(products)

    return _build_result(best)


def _pick_best_entry(products: list[RegistryProduct]) -> RegistryProduct:
    """Pick the most relevant entry when multiple rows share the same registry number.
    Prefer: active (not expired) > highest score > latest doc_date."""

    today = date.today().isoformat()

    def sort_key(p: RegistryProduct):
        is_valid = 1 if (p.doc_valid_till and p.doc_valid_till >= today) else 0
        score = p.score or 0
        doc_date = p.doc_date or ""
        return (is_valid, score, doc_date)

    return max(products, key=sort_key)


def _build_result(product: RegistryProduct) -> RegistryResult:
    """Convert a RegistryProduct DB row into a RegistryResult."""
    today = date.today().isoformat()

    # Determine actuality from dates
    is_actual = True
    if product.end_date and product.end_date <= today:
        is_actual = False
    elif product.doc_valid_till and product.doc_valid_till < today:
        is_actual = False

    # Check score_desc for explicit status
    if product.score_desc:
        desc_lower = product.score_desc.lower()
        if any(w in desc_lower for w in ("аннулир", "недействит", "отказ")):
            is_actual = False

    status = "ok" if is_actual else "not_actual"

    # Build URL to the registry entry page
    url = f"https://gisp.gov.ru/pp719v2/pub/prod/{product.registry_number}/" if product.registry_number else None

    return RegistryResult(
        status=status,
        is_actual=is_actual,
        cert_end_date=product.doc_valid_till,
        registry_name=product.product_name,
        localization_score=product.score,
        okpd2_from_registry=product.okpd2,
        url=url,
        raw_data={
            "org_name": product.org_name,
            "inn": product.inn,
            "ogrn": product.ogrn,
            "tnved": product.tnved,
            "percentage": product.percentage,
            "score_desc": product.score_desc,
            "doc_date": product.doc_date,
            "doc_valid_till": product.doc_valid_till,
            "end_date": product.end_date,
        },
    )
