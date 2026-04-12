"""Business-level LLM tasks for zakupAI.

Each public function here corresponds to one logical job that some part of
the app wants done by an LLM (parse supplier file, extract lots from a
TZ, generate search queries, compare characteristics, ...).

These functions live above ``app.services.llm`` (the unified transport)
and below the routers/services that use them. They:

  * own the prompt for that job;
  * pick a stable ``task`` name so per-task model overrides work;
  * own the JSON schema (when one exists);
  * decide whether the result should be sync or async;
  * normalize the LLM output into something the rest of the app can use.

Replaces the two pre-existing chat-completion clients
``app/llm_openai.py`` and ``app/services/llm_client.py``. Existing imports
of those modules are migrated to import from here instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from app.lots_extraction_prompting import (
        build_bid_lots_prompt_and_schema,
        build_kp_prompt_and_schema,
        build_lots_prompt_and_schema,
    )
except ImportError:  # pragma: no cover — alt import path used by some scripts
    from lots_extraction_prompting import (  # type: ignore[no-redef]
        build_bid_lots_prompt_and_schema,
        build_kp_prompt_and_schema,
        build_lots_prompt_and_schema,
    )

from . import llm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task names — kept as constants so env-var lookups stay grep-able
# ---------------------------------------------------------------------------

# Search-queries generation (M1)
TASK_SEARCH_QUERIES = "search_queries"
# TZ → lots (M1/M3)
TASK_LOTS_EXTRACTION = "lots_extraction"
# Unified KP parser. Used by both M2/M3 (extract_bid_lots) and M4
# (extract_items_from_text). The legacy task names below stay as aliases so
# operators that pinned a model via LLM_MODEL_BID_LOTS_EXTRACTION or
# LLM_MODEL_KP_ITEMS_EXTRACTION keep working.
TASK_KP_EXTRACTION = "kp_extraction"
TASK_BID_LOTS_EXTRACTION = "bid_lots_extraction"  # alias, kept for backward compat
TASK_KP_ITEMS_EXTRACTION = "kp_items_extraction"  # alias, kept for backward compat
# Perplexity post-processing (M1)
TASK_PERPLEXITY_POSTPROCESS = "perplexity_postprocess"
# GISP characteristic comparison (M4)
TASK_COMPARE_CHARACTERISTICS = "compare_characteristics"


# ---------------------------------------------------------------------------
# JSON schemas (carried over verbatim from the old llm_openai.py)
# ---------------------------------------------------------------------------


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


# Schemas reused from app.lots_extraction_prompting — keep them addressable
# from this module so callers don't need a third import.
_LOTS_PROMPT_STUB, LOTS_SCHEMA = build_lots_prompt_and_schema("")
_BID_LOTS_PROMPT_STUB, LOTS_WITH_PRICE_SCHEMA = build_bid_lots_prompt_and_schema("")


# ---------------------------------------------------------------------------
# Search queries generation (M1)
# ---------------------------------------------------------------------------


@dataclass
class GeneratedSearchPlan:
    queries: List[str]
    note: str


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


def build_search_queries(
    terms_text: str,
    hints: List[str] | None = None,
    *,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> GeneratedSearchPlan:
    """Generate Yandex search queries for a TZ via LLM."""
    messages = _build_search_queries_prompt(terms_text or "", hints or [])
    payload = llm.chat_json(
        messages,
        task=TASK_SEARCH_QUERIES,
        json_schema=SEARCH_QUERIES_SCHEMA,
        max_completion_tokens=1200,
        timeout=120.0,
        usage_ctx=usage_ctx,
    )

    queries = _deduplicate_queries(payload.get("queries") or [])
    if len(queries) < 5:
        raise RuntimeError("LLM returned too few search queries")

    cfg = llm.resolve_config(TASK_SEARCH_QUERIES)
    return GeneratedSearchPlan(
        queries=queries[:10],
        note=f"Запросы сгенерированы LLM ({cfg.model}).",
    )


# ---------------------------------------------------------------------------
# Lots extraction from TZ (M1/M3)
# ---------------------------------------------------------------------------


def _check_truncated(response: Any, tag: str, output_text: str) -> None:
    """Raise a clear error if the model hit max_completion_tokens.

    Without this we get a JSONDecodeError ~25k chars in and the user has no
    idea what happened.
    """
    finish_reason = None
    try:
        finish_reason = response.choices[0].finish_reason if response.choices else None
    except Exception:  # noqa: BLE001
        pass
    logger.info("[%s] finish_reason=%s output_chars=%d", tag, finish_reason, len(output_text or ""))
    if finish_reason == "length":
        raise RuntimeError(
            f"Модель оборвала ответ по лимиту токенов (output={len(output_text or '')} chars). "
            f"ТЗ слишком длинное для разовой обработки. Поднимите max_completion_tokens "
            f"или разбейте ТЗ на части."
        )


def _build_lots_prompt(terms_text: str) -> List[Dict[str, str]]:
    prompt, _ = build_lots_prompt_and_schema(terms_text or "")
    return [{"role": "user", "content": prompt}]


def extract_lots(
    terms_text: str,
    *,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extract structured lots from a Технические условия / ТЗ document."""
    messages = _build_lots_prompt(terms_text)
    response = llm.chat_completion(
        messages,
        task=TASK_LOTS_EXTRACTION,
        response_format={"type": "json_schema", "json_schema": LOTS_SCHEMA},
        # 16000 covers ~50k chars of structured JSON output, which handles
        # long specs like the radiodetali TZ (29k char input, ~12k token
        # output). 8000 was the previous cap and got truncated mid-string.
        max_completion_tokens=16000,
        timeout=180.0,
        usage_ctx=usage_ctx,
    )

    output_text = response.choices[0].message.content if response.choices else None
    if not output_text:
        raise RuntimeError("Empty response from LLM (lots_extraction)")
    _check_truncated(response, "lots_extraction", output_text)

    try:
        return json.loads(output_text)
    except json.JSONDecodeError:
        logger.error(
            "[lots_extraction] json_parse_failed; raw_output_len=%d tail=%r",
            len(output_text), output_text[-500:],
        )
        raise


# ---------------------------------------------------------------------------
# Unified KP parser (M2/M3 and M4 share this)
# ---------------------------------------------------------------------------
#
# Before PR-3 there were two separate parsers — extract_bid_lots in
# llm_openai.py (for M2/M3 BidLot) and extract_items_from_text in
# llm_client.py (for M4 RegimeCheckItem) — with two different prompts
# and two different output shapes that drifted apart whenever someone
# tweaked one without the other. They now share kp_extraction_prompt.j2
# and KpExtractionResult, with a superset schema: every consumer takes
# only the fields it needs.
#
# Output shape (per lot):
#   {
#     "name": str,            # required
#     "units": str,
#     "count": str,
#     "price": str,           # used by M2/M3 — empty string when absent
#     "registry_number": str, # used by M4 — empty string when absent
#     "okpd2_code": str,      # used by M4 — empty string when absent
#     "parameters": [{"name", "value", "units"}, ...],
#   }
# All fields are present on every lot (the JSON schema enforces it),
# missing values are empty strings — never null. Consumers must treat
# "" as "absent".


def parse_kp(
    terms_text: str,
    *,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the unified KP parser. Sync — call via run_in_executor for async sites.

    Returns ``{"lots": [...]}``. Raises on truncation or invalid JSON.
    """
    prompt, schema = build_kp_prompt_and_schema(terms_text or "")
    response = llm.chat_completion(
        [{"role": "user", "content": prompt}],
        task=TASK_KP_EXTRACTION,
        response_format={"type": "json_schema", "json_schema": schema},
        max_completion_tokens=16000,
        timeout=180.0,
        usage_ctx=usage_ctx,
    )

    output_text = response.choices[0].message.content if response.choices else None
    if not output_text:
        raise RuntimeError("Empty response from LLM (kp_extraction)")
    _check_truncated(response, "kp_extraction", output_text)

    try:
        return json.loads(output_text)
    except json.JSONDecodeError:
        logger.error(
            "[kp_extraction] json_parse_failed; raw_output_len=%d tail=%r",
            len(output_text), output_text[-500:],
        )
        raise


def extract_bid_lots(
    terms_text: str,
    *,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Backward-compat alias used by ``task_queue._run`` for M2/M3 КП ingestion.

    The shape returned matches what ``task_queue._sync_bid_lots`` already expects
    (``{"lots": [...]}``). The new fields ``registry_number`` and ``okpd2_code``
    are present on every lot — ``_sync_bid_lots`` was updated in PR-3 to persist
    them onto BidLot.
    """
    return parse_kp(terms_text, usage_ctx=usage_ctx)


# ---------------------------------------------------------------------------
# Perplexity post-processing (M1)
# ---------------------------------------------------------------------------


def extract_structured_contacts_from_perplexity(
    raw_answer: str,
    terms_text: str,
    *,
    usage_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Take a raw Perplexity answer string and turn it into structured supplier hits."""
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
    payload = llm.chat_json(
        messages,
        task=TASK_PERPLEXITY_POSTPROCESS,
        json_schema=PERPLEXITY_SUPPLIERS_SCHEMA,
        max_completion_tokens=2200,
        timeout=120.0,
        usage_ctx=usage_ctx,
    )
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


# ---------------------------------------------------------------------------
# КП → items extraction for M4 Нацрежим
# ---------------------------------------------------------------------------


def kp_lots_to_check_items(lots: list[dict]) -> list[dict]:
    """Project unified KP lots onto the shape that ``check_runner`` expects.

    The check pipeline reads ``name``, ``registry_number``, ``okpd2_code``,
    ``characteristics`` and ignores ``count``/``units``/``price``. We also
    drop empty-string ``registry_number``/``okpd2_code`` to None so the
    downstream "is this set?" checks behave as before.
    """
    items: list[dict] = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        name = (lot.get("name") or "").strip()
        if not name:
            continue
        rn = (lot.get("registry_number") or "").strip() or None
        okpd2 = (lot.get("okpd2_code") or "").strip() or None
        chars: list[dict] = []
        for p in lot.get("parameters") or []:
            if not isinstance(p, dict):
                continue
            pname = (p.get("name") or "").strip()
            if not pname:
                continue
            pvalue = p.get("value") or ""
            punits = (p.get("units") or "").strip()
            # Concatenate units onto the value the same way the old M4 parser did,
            # so the LLM comparator sees "300 мм" instead of just "300".
            if punits:
                pvalue = f"{pvalue} {punits}".strip()
            chars.append({"name": pname, "value": pvalue})
        items.append({
            "name": name,
            "registry_number": rn,
            "okpd2_code": okpd2,
            "characteristics": chars,
        })
    return items


async def extract_items_from_text(raw_text: str) -> list[dict]:
    """Async wrapper for ``check_runner.run_check`` (M4 file path).

    Calls the unified ``parse_kp`` in a worker thread (sync openai SDK)
    and reshapes the result to the M4 dict format.
    """
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, parse_kp, raw_text)
    lots = payload.get("lots") or []
    return kp_lots_to_check_items(lots)


# ---------------------------------------------------------------------------
# GISP characteristic comparison (M4 — async)
# ---------------------------------------------------------------------------


async def compare_characteristics(
    supplier_chars: list[dict],
    gisp_chars: list[dict],
    product_name: str,
) -> list[dict]:
    """Сравнивает характеристики поставщика с ГИСП.

    Возвращает массив с per-характеристика статусами:
    ok | mismatch | wording | missing_in_gisp.
    """
    prompt = f"""Сравни характеристики товара «{product_name}» из заявки поставщика с данными ГИСП.

Поставщик:
{json.dumps(supplier_chars, ensure_ascii=False, indent=2)}

ГИСП:
{json.dumps(gisp_chars, ensure_ascii=False, indent=2)}

Для каждой характеристики поставщика верни:
- name: название (из заявки)
- supplier_value: значение поставщика
- gisp_value: значение из ГИСП (null если нет)
- status: "ok" | "mismatch" | "wording" | "missing_in_gisp"
  * ok — совпадают или эквивалентны
  * mismatch — отличаются, несовместимы
  * wording — эквивалентны, но записаны по-разному (единицы, синонимы)
  * missing_in_gisp — в ГИСП нет этой характеристики
- comment: пояснение (только если status != "ok")

Верни JSON: {{"comparison": [...]}}. Только JSON."""

    parsed = await llm.achat_json(
        [{"role": "user", "content": prompt}],
        task=TASK_COMPARE_CHARACTERISTICS,
        max_completion_tokens=4000,
        timeout=180.0,
    )
    return parsed.get("comparison", [])
