import os
from pathlib import Path
from typing import Any

import requests


class MistralOcrError(RuntimeError):
    pass


def _extract_markdown_from_payload(payload: dict[str, Any]) -> str:
    pages = payload.get("pages")
    if isinstance(pages, list):
        chunks: list[str] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            for key in ("markdown", "md", "text", "content"):
                value = page.get(key)
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())
                    break
        if chunks:
            return "\n\n".join(chunks).strip()

    for key in ("markdown", "md", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_markdown_from_payload(data)

    raise MistralOcrError("Mistral OCR response does not contain markdown/text content")


def run_pipeline(file_path: str, update_status, options, page_range):
    update_status("Mistral OCR pipeline started.")

    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise MistralOcrError("MISTRAL_API_KEY is not configured")

    model = os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-latest")
    base_url = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai").rstrip("/")
    endpoint = f"{base_url}/v1/ocr"

    path = Path(file_path)
    if not path.exists():
        raise MistralOcrError("PDF file does not exist")

    page_start, page_end = page_range
    page_range_value = f"{page_start}-{page_end}"

    headers = {"Authorization": f"Bearer {api_key}"}
    data = {
        "model": model,
        "page_range": page_range_value,
    }

    with path.open("rb") as fd:
        files = {
            "file": (path.name, fd, "application/pdf"),
        }
        response = requests.post(endpoint, headers=headers, data=data, files=files, timeout=180)

    print(f"[mistral-ocr] status_code={response.status_code}")
    print(f"[mistral-ocr] raw_response={response.text}")

    if response.status_code >= 400:
        raise MistralOcrError(
            f"Mistral OCR request failed ({response.status_code}): {response.text[:500]}"
        )

    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[mistral-ocr] json_parse_exception: {exc}")
        raise MistralOcrError(f"Invalid JSON in Mistral OCR response: {exc}") from exc

    markdown = _extract_markdown_from_payload(payload)
    if not markdown:
        raise MistralOcrError("Mistral OCR produced empty markdown")

    update_status("Mistral OCR pipeline completed.")
    return markdown
