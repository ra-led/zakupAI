import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from fastapi import HTTPException, status


def _load_json_list(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return []

    file_path = Path(path)
    if not file_path.exists():
        return []

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File {file_path} is not valid JSON: {exc}",
        ) from exc

    if isinstance(data, list):
        return data

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"File {file_path} does not contain a JSON list",
    )


def _normalize_site(url: Optional[str]) -> str:
    if not url:
        return ""
    normalized = url.strip()
    if not normalized:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", normalized):
        normalized = f"https://{normalized}"
    parsed = urlparse(normalized)
    host = (parsed.netloc or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    return f"https://{host}"


def _extract_domain(url: Optional[str]) -> str:
    normalized = _normalize_site(url)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    return parsed.netloc.lower().strip()


def _normalize_email(email: Any) -> str:
    if not isinstance(email, str):
        return ""
    value = email.strip().lower()
    if "@" not in value:
        return ""
    return value


def _merge_source(current: Optional[str], new_value: Optional[str]) -> Optional[str]:
    current_values = {part.strip() for part in (current or "").split("+") if part.strip()}
    new_values = {part.strip() for part in (new_value or "").split("+") if part.strip()}
    merged = sorted(current_values | new_values)
    return "+".join(merged) if merged else None


def _safe_confidence(value: Any, fallback: float = 0.5) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, confidence))


def _build_dedup_key(domain: str, emails: List[str]) -> str:
    primary_email = emails[0] if emails else ""
    return f"{domain}|{primary_email}" if domain else primary_email


def merge_contacts(
    processed_contacts: Iterable[Dict[str, Any]], search_output: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    aggregated: Dict[str, Dict[str, Any]] = {}

    def _ensure_record(raw_site: Optional[str]) -> Optional[Dict[str, Any]]:
        domain = _extract_domain(raw_site)
        if not domain:
            return None
        if domain not in aggregated:
            aggregated[domain] = {
                "website": _normalize_site(raw_site),
                "domain": domain,
                "is_relevant": True,
                "reason": None,
                "name": None,
                "emails": [],
                "source": None,
                "confidence": 0.0,
                "dedup_key": domain,
            }
        return aggregated[domain]

    for item in search_output:
        record = _ensure_record(item.get("website"))
        if not record:
            continue
        record["source"] = _merge_source(record.get("source"), item.get("source") or "yandex")
        record["confidence"] = max(record["confidence"], _safe_confidence(item.get("confidence"), fallback=0.55))
        if item.get("reason") and not record.get("reason"):
            record["reason"] = item.get("reason")
        emails = item.get("emails") or []
        if isinstance(emails, list):
            for email in emails:
                normalized_email = _normalize_email(email)
                if normalized_email and normalized_email not in record["emails"]:
                    record["emails"].append(normalized_email)

    for contact in processed_contacts:
        record = _ensure_record(contact.get("website"))
        if not record:
            continue

        record["is_relevant"] = bool(record["is_relevant"] and contact.get("is_relevant", True))
        if contact.get("reason"):
            record["reason"] = contact.get("reason")
        if contact.get("name") and not record.get("name"):
            record["name"] = contact.get("name")
        record["source"] = _merge_source(record.get("source"), contact.get("source") or "yandex")
        fallback_confidence = 0.8 if contact.get("is_relevant", True) else 0.2
        record["confidence"] = max(record["confidence"], _safe_confidence(contact.get("confidence"), fallback=fallback_confidence))

        emails = contact.get("emails") or []
        if isinstance(emails, list):
            for email in emails:
                normalized_email = _normalize_email(email)
                if normalized_email and normalized_email not in record["emails"]:
                    record["emails"].append(normalized_email)

    # Deduplicate emails globally across sources/domains.
    seen_emails: set[str] = set()
    merged: List[Dict[str, Any]] = []
    for domain, record in aggregated.items():
        unique_emails: List[str] = []
        for email in record.get("emails", []):
            if email in seen_emails:
                continue
            seen_emails.add(email)
            unique_emails.append(email)

        dedup_key = record.get("dedup_key") or _build_dedup_key(domain, unique_emails)
        merged.append(
            {
                "website": record.get("website"),
                "is_relevant": bool(record.get("is_relevant", True)),
                "reason": record.get("reason"),
                "name": record.get("name"),
                "emails": unique_emails,
                "source": record.get("source") or "unknown",
                "confidence": _safe_confidence(record.get("confidence"), fallback=0.5),
                "dedup_key": dedup_key,
            }
        )

    return merged


def load_contacts_from_files(
    processed_contacts_path: Optional[str], search_output_path: Optional[str]
) -> List[Dict[str, Any]]:
    processed_contacts = _load_json_list(processed_contacts_path)
    search_output = _load_json_list(search_output_path)
    if not processed_contacts and not search_output:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No suppliers_contacts.py output found. Provide JSON payload or paths to processed_contacts.json/search_output.json.",
        )

    return merge_contacts(processed_contacts, search_output)
