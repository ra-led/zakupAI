import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from openai import OpenAI


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



def _raw_create_chat_completion(client: OpenAI, **kwargs):
    raw_response = client.chat.completions.with_raw_response.create(**kwargs)
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


LOTS_SCHEMA: Dict[str, Any] = {
    "name": "lots_extraction",
    "schema": {
        "type": "object",
        "properties": {
            "lots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "parameters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "value": {"type": "string"},
                                    "units": {"type": "string"},
                                },
                                "required": ["name", "value", "units"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["name", "parameters"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["lots"],
        "additionalProperties": False,
    },
    "strict": True,
}


LOTS_WITH_PRICE_SCHEMA: Dict[str, Any] = {
    "name": "bid_lots_extraction",
    "schema": {
        "type": "object",
        "properties": {
            "lots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "price": {"type": "string"},
                        "parameters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "value": {"type": "string"},
                                    "units": {"type": "string"},
                                },
                                "required": ["name", "value", "units"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["name", "price", "parameters"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["lots"],
        "additionalProperties": False,
    },
    "strict": True,
}


def _build_lots_prompt(terms_text: str) -> List[Dict[str, str]]:
    system_message = (
        "Вы извлекаете лоты из технического задания. "
        "Верните только JSON по схеме. "
        "Всегда возвращайте массив lots. "
        "Если нет лотов, верните {\"lots\":[]}. "
        "Если у параметра нет количественного значения, value=\"compliance\" и units=\"\". "
        "Если единицы не указаны, units=\"\"."
    )
    user_message = f"Техническое задание (markdown):\n{terms_text}"
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


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
    system_message = (
        "Вы извлекаете лоты из коммерческого предложения. "
        "Верните только JSON по схеме. "
        "Всегда возвращайте массив lots. "
        "Если нет лотов, верните {\"lots\":[]}. "
        "Поле price должно быть строкой и может включать валюту. "
        "Если цена не указана, price=\"не указано\". "
        "Если у параметра нет количественного значения, value=\"compliance\" и units=\"\". "
        "Если единицы не указаны, units=\"\"."
    )
    user_message = f"Текст предложения (markdown):\n{terms_text}"
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


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
