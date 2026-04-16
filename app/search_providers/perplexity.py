import os
from typing import Any, Dict

from openai import OpenAI

from app.llm_metrics import record_llm_usage
from app.llm_openai import extract_structured_contacts_from_perplexity


def _build_prompt(terms_text: str, min_contacts: int) -> str:
    return (
        "Найди поставщиков и их веб-сайты "
        f"(не менее {min_contacts}) для следующей закупки:\n"
        f"{terms_text}"
    )


def search_suppliers_with_perplexity(terms_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    min_contacts_raw = (os.getenv("PERPLEXITY_MIN_CONTACTS") or "").strip()
    try:
        min_contacts = int(min_contacts_raw) if min_contacts_raw else 10
    except ValueError:
        min_contacts = 10
    prompt = _build_prompt(terms_text or "", min_contacts)

    client = OpenAI(
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=api_key,
    )
    model = os.getenv("PERPLEXITY_MODEL", "perplexity/sonar-pro-search")

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"reasoning": {"enabled": True}},
    )
    try:
        record_llm_usage(
            response,
            provider="perplexity",
            model=model,
            operation="supplier_search_perplexity",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[metrics] failed to record perplexity usage: {exc}")
    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise RuntimeError("Empty response from Perplexity")

    structured = extract_structured_contacts_from_perplexity(content, terms_text)
    return {
        "queries": [prompt],
        "tech_task_excerpt": (terms_text or "")[:160],
        "note": f"Поиск выполнен через Perplexity ({model})",
        "raw_response": content,
        "search_output": structured.get("search_output", []),
        "processed_contacts": [],
    }
