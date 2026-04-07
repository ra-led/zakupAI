import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from openai import OpenAI
try:
    from app.lots_extraction_prompting import (
        build_bid_lots_prompt_and_schema,
        build_lots_prompt_and_schema,
    )
except ImportError:  # pragma: no cover
    from lots_extraction_prompting import (
        build_bid_lots_prompt_and_schema,
        build_lots_prompt_and_schema,
    )


@dataclass
class GeneratedSearchPlan:
    queries: List[str]
    note: str


SEARCH_QUERIES_SCHEMA: Dict[str, Any] = {
    "name": "search_queries_generation",
    "schema": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 5,
                "maxItems": 10,
            }
        },
        "required": ["queries"],
        "additionalProperties": False,
    },
    "strict": True,
}

PERPLEXITY_SUPPLIERS_SCHEMA: Dict[str, Any] = {
    "name": "perplexity_supplier_sites_extraction",
    "schema": {
        "type": "object",
        "properties": {
            "suppliers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "website": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "text": {"type": ["string", "null"]},
                        "reason": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "website",
                        "title",
                        "text",
                        "reason",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["suppliers"],
        "additionalProperties": False,
    },
    "strict": True,
}



def _with_reasoning_disabled(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(kwargs)
    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict):
        merged = dict(extra_body)
        merged["reasoning"] = {"enabled": False}
        payload["extra_body"] = merged
    else:
        payload["extra_body"] = {"reasoning": {"enabled": False}}
    return payload


def _raw_create_chat_completion(client: OpenAI, **kwargs):
    request_payload = _with_reasoning_disabled(kwargs)
    raw_response = client.chat.completions.with_raw_response.create(**request_payload)
    status_code = getattr(raw_response, "status_code", None)
    raw_text = None
    text_attr = getattr(raw_response, "text", None)
    if callable(text_attr):
        try:
            raw_text = text_attr()
        except Exception as exc:  # noqa: BLE001
            raw_text = f"<failed to read raw text: {exc}>"
    elif isinstance(text_attr, str):
        raw_text = text_attr

    if raw_text is None:
        try:
            raw_text = str(raw_response)
        except Exception as exc:  # noqa: BLE001
            raw_text = f"<failed to stringify response: {exc}>"

    print(f"[openai] status_code={status_code}")
    print(f"[openai] raw_response={raw_text}")
    return raw_response.parse()


def _log_prompt(tag: str, messages: List[Dict[str, str]]) -> None:
    print(f"[{tag}] prompt_messages={json.dumps(messages, ensure_ascii=False)}")


def _deduplicate_queries(raw_queries: List[str]) -> List[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in raw_queries:
        query = " ".join((raw or "").split()).strip()
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(query)
    return unique


def _build_search_queries_prompt(terms_text: str, hints: List[str]) -> List[Dict[str, str]]:
    hints_text = ", ".join([h.strip() for h in hints if h and h.strip()]) or "нет"
    system_message = (
        "Вы генерируете поисковые запросы для Яндекса по техническому заданию закупки. "
        "Верните только JSON по схеме. Нужны 5-10 коротких, практичных, коммерчески ориентированных "
        "запросов на русском языке для поиска поставщиков. "
        "Добавляйте вариации: оптовый поставщик, дилер, дистрибьютор, производитель, купить оптом."
    )
    user_message = (
        f"Техническое задание:\n{terms_text}\n\n"
        f"Подсказки пользователя: {hints_text}\n\n"
        "Сформируйте запросы только для поиска потенциальных поставщиков."
    )
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


def build_search_queries(terms_text: str, hints: List[str] | None = None) -> GeneratedSearchPlan:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    base_url = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url)
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")

    messages = _build_search_queries_prompt(terms_text or "", hints or [])
    _log_prompt("search_queries_generation", messages)
    try:
        response = _raw_create_chat_completion(
            client,
            model=model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": SEARCH_QUERIES_SCHEMA},
            max_completion_tokens=1200,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[search_queries_generation] openai_request_failed: {exc}")
        raise

    output_text = response.choices[0].message.content if response.choices else None
    if not output_text:
        raise RuntimeError("Empty response from OpenAI while generating search queries")

    try:
        payload = json.loads(output_text)
    except Exception as exc:  # noqa: BLE001
        print(f"[search_queries_generation] json_parse_failed: {exc}; raw_output={output_text}")
        raise

    queries = _deduplicate_queries(payload.get("queries") or [])
    if len(queries) < 5:
        raise RuntimeError("OpenAI returned too few search queries")

    return GeneratedSearchPlan(
        queries=queries[:10],
        note=f"Запросы сгенерированы LLM ({model}).",
    )


_LOTS_PROMPT_STUB, LOTS_SCHEMA = build_lots_prompt_and_schema("")
_BID_LOTS_PROMPT_STUB, LOTS_WITH_PRICE_SCHEMA = build_bid_lots_prompt_and_schema("")


def _build_lots_prompt(terms_text: str) -> List[Dict[str, str]]:
    prompt, _ = build_lots_prompt_and_schema(terms_text or "")
    return [{"role": "user", "content": prompt}]


def extract_lots(terms_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    base_url = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url)
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")

    messages = _build_lots_prompt(terms_text)
    _log_prompt("lots_extraction", messages)
    try:
        response = _raw_create_chat_completion(
            client,
            model=model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": LOTS_SCHEMA},
            max_completion_tokens=2000,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[lots_extraction] openai_request_failed: {exc}")
        raise

    output_text = response.choices[0].message.content if response.choices else None
    if not output_text:
        raise RuntimeError("Empty response from OpenAI")

    try:
        return json.loads(output_text)
    except Exception as exc:  # noqa: BLE001
        print(f"[lots_extraction] json_parse_failed: {exc}; raw_output={output_text}")
        raise


def _build_bid_lots_prompt(terms_text: str) -> List[Dict[str, str]]:
    prompt, _ = build_bid_lots_prompt_and_schema(terms_text or "")
    return [{"role": "user", "content": prompt}]


def extract_bid_lots(terms_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    base_url = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url)
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")

    messages = _build_bid_lots_prompt(terms_text)
    _log_prompt("bid_lots_extraction", messages)
    try:
        response = _raw_create_chat_completion(
            client,
            model=model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": LOTS_WITH_PRICE_SCHEMA},
            max_completion_tokens=2000,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[bid_lots_extraction] openai_request_failed: {exc}")
        raise

    output_text = response.choices[0].message.content if response.choices else None
    if not output_text:
        raise RuntimeError("Empty response from OpenAI")

    try:
        return json.loads(output_text)
    except Exception as exc:  # noqa: BLE001
        print(f"[bid_lots_extraction] json_parse_failed: {exc}; raw_output={output_text}")
        raise


def extract_structured_contacts_from_perplexity(raw_answer: str, terms_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    base_url = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url)
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")

    messages = [
        {
            "role": "system",
            "content": (
                "Ты извлекаешь только сайты потенциальных поставщиков из результата поиска. "
                "Возвращай только валидный JSON по схеме. "
                "Не выдумывай email-адреса."
            ),
        },
        {
            "role": "user",
            "content": (
                "Техническое задание:\n"
                f"{terms_text}\n\n"
                "Ответ Perplexity:\n"
                f"{raw_answer}\n\n"
                "Выдели только потенциальных поставщиков и их веб-сайты. "
                "Сформируй короткий заголовок и текст-сниппет, максимально близкий к формату search results."
            ),
        },
    ]
    _log_prompt("perplexity_contacts_postprocess", messages)

    response = _raw_create_chat_completion(
        client,
        model=model,
        messages=messages,
        response_format={"type": "json_schema", "json_schema": PERPLEXITY_SUPPLIERS_SCHEMA},
        max_completion_tokens=2200,
    )
    output_text = response.choices[0].message.content if response.choices else None
    if not output_text:
        raise RuntimeError("Empty response from OpenAI while parsing Perplexity output")

    payload = json.loads(output_text)
    suppliers = payload.get("suppliers") or []

    search_output: List[Dict[str, Any]] = []
    seen_sites: set[str] = set()
    for supplier in suppliers:
        website = " ".join((supplier.get("website") or "").split()).strip()
        if not website:
            continue
        site_key = website.lower()
        if site_key in seen_sites:
            continue
        seen_sites.add(site_key)

        confidence = supplier.get("confidence")
        try:
            confidence_value = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence_value = 0.5

        dedup_key = website.lower().rstrip("/")
        search_output.append(
            {
                "title": supplier.get("title"),
                "text": supplier.get("text"),
                "link": website,
                "website": website,
                "reason": supplier.get("reason"),
                "source": "perplexity",
                "confidence": confidence_value,
                "dedup_key": dedup_key,
            }
        )

    return {
        "search_output": search_output,
        "processed_contacts": [],
    }
