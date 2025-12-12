import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
    return url.rstrip("/")


def merge_contacts(
    processed_contacts: Iterable[Dict[str, Any]], search_output: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    emails_map: Dict[str, List[str]] = {}
    for item in search_output:
        site = _normalize_site(item.get("website"))
        if not site:
            continue
        emails = item.get("emails") or []
        if isinstance(emails, list):
            emails_map[site] = [e for e in emails if isinstance(e, str)]

    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for contact in processed_contacts:
        site = _normalize_site(contact.get("website"))
        if not site or site in seen:
            continue

        merged.append(
            {
                "website": site,
                "is_relevant": bool(contact.get("is_relevant", True)),
                "reason": contact.get("reason"),
                "name": contact.get("name"),
                "emails": emails_map.get(site, []),
            }
        )
        seen.add(site)

    # Add sites that only appeared in search_output
    for site, emails in emails_map.items():
        if site in seen:
            continue
        merged.append({"website": site, "is_relevant": True, "reason": None, "name": None, "emails": emails})

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
