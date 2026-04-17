import json
import logging
import os
import time
import math
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from openai import OpenAI
from sqlmodel import Session, select

from app.database import create_db_and_tables, engine
from app.models import BidLot, BidLotParameter, LLMTask, Lot, LotParameter, Purchase, SiteCrawlMetric, Supplier, SupplierContact
from app.search_providers.perplexity import search_suppliers_with_perplexity
from app.supplier_import import merge_contacts
from app.task_queue import TaskQueue
from app.usage_tracking import record_usage, set_usage_context
from suppliers_contacts import (
    collect_contacts_from_websites,
    collect_yandex_search_output_from_text,
    shutdown_driver,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = float(os.getenv("ETL_POLL_INTERVAL", "5"))
MAX_WORKERS = int(os.getenv("ETL_MAX_WORKERS", "5"))
DEFAULT_WORKER_TASK_TYPES = ["supplier_search", "supplier_search_perplexity", "lot_comparison"]


def _parse_worker_task_types() -> List[str]:
    task_types_raw = (os.getenv("WORKER_TASK_TYPES") or "").strip()
    task_types = (
        [item.strip() for item in task_types_raw.split(",") if item.strip()]
        if task_types_raw
        else DEFAULT_WORKER_TASK_TYPES
    )
    if not task_types:
        raise RuntimeError("WORKER_TASK_TYPES is empty")
    return task_types


WORKER_TASK_TYPES = _parse_worker_task_types()

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "perplexity/pplx-embed-v1-4b")
OPENROUTER_MATCH_MODEL = os.getenv("OPENROUTER_MATCH_MODEL", "openai/gpt-4o-mini")
LOT_MATCH_MIN_CONFIDENCE = float(os.getenv("LOT_MATCH_MIN_CONFIDENCE", "0.45"))
LOT_PARAM_MATCH_MIN_CONFIDENCE = float(os.getenv("LOT_PARAM_MATCH_MIN_CONFIDENCE", "0.45"))


def _upsert_suppliers(session: Session, task: LLMTask, merged_contacts: List[Dict]) -> List[Dict]:
    created: List[Dict] = []

    for contact in merged_contacts:
        if not contact.get("is_relevant"):
            continue

        website = contact.get("website")
        if not website or not task.purchase_id:
            continue

        supplier = session.exec(
            select(Supplier).where(
                Supplier.purchase_id == task.purchase_id, Supplier.website_url == website
            )
        ).first()

        if not supplier:
            supplier = Supplier(
                purchase_id=task.purchase_id,
                company_name=contact.get("name") or website,
                website_url=website,
                relevance_score=contact.get("confidence") if contact.get("confidence") is not None else 1.0,
                reason=contact.get("reason"),
            )
            session.add(supplier)
            session.commit()
            session.refresh(supplier)
        elif not supplier.reason:
            supplier.reason = contact.get("reason")

        for email in contact.get("emails", []):
            existing_contact = session.exec(
                select(SupplierContact).where(
                    SupplierContact.supplier_id == supplier.id, SupplierContact.email == email
                )
            ).first()
            if not existing_contact:
                session.add(
                    SupplierContact(
                        supplier_id=supplier.id,
                        email=email,
                        source_url=website,
                        source=contact.get("source"),
                        confidence=contact.get("confidence"),
                        dedup_key=contact.get("dedup_key"),
                        reason=contact.get("reason"),
                        is_selected_for_request=False,
                    )
                )
            else:
                if not existing_contact.source and contact.get("source"):
                    existing_contact.source = contact.get("source")
                if existing_contact.confidence is None and contact.get("confidence") is not None:
                    existing_contact.confidence = contact.get("confidence")
                if not existing_contact.dedup_key and contact.get("dedup_key"):
                    existing_contact.dedup_key = contact.get("dedup_key")
                session.add(existing_contact)

        created.append({"supplier_id": supplier.id, "website": website, "emails": contact.get("emails", [])})

    session.commit()
    return created


ProgressCallback = Optional[Callable[[Dict], None]]


def _collect_combined_contacts(
    terms_text: str,
    task_type: str,
    progress_cb: ProgressCallback = None,
    usage_ctx: Optional[Dict] = None,
    metrics_cb: Optional[Callable[[Dict], None]] = None,
) -> Dict:
    """Run the supplier-discovery pipeline.

    progress_cb is invoked after each milestone with a partial result dict
    so callers (the worker) can write intermediate progress to the DB.
    The pipeline takes 5-15 minutes; without intermediate updates the
    frontend cannot tell "stuck" from "in progress".
    """
    yandex_result: Dict = {"queries": [], "search_output": [], "processed_contacts": [], "tz_summary": None}
    perplexity_result: Dict = {"queries": [], "search_output": [], "processed_contacts": []}
    notes: List[str] = []

    def _emit(extra: Optional[Dict] = None) -> None:
        if not progress_cb:
            return
        partial = {
            "queries": (yandex_result.get("queries") or []) + (perplexity_result.get("queries") or []),
            "tech_task_excerpt": terms_text[:160],
            "note": "; ".join(notes) if notes else "Поиск поставщиков выполняется",
            "search_output": [],
            "processed_contacts": [],
        }
        if extra:
            partial.update(extra)
        try:
            progress_cb(partial)
        except Exception:  # noqa: BLE001
            logger.exception("progress_cb failed (non-fatal)")

    # Stage 0: kicked off
    notes.append("Запуск пайплайна поиска")
    _emit()
    notes.pop()  # remove the temp marker so it doesn't pollute final note

    if task_type == "supplier_search":
        try:
            logger.info("[supplier_search] starting Yandex stage")
            yandex_result = collect_yandex_search_output_from_text(terms_text)
            notes.append("Yandex поиск обработан")
            logger.info(
                "[supplier_search] Yandex done: queries=%s sites=%s",
                len(yandex_result.get("queries") or []),
                len(yandex_result.get("search_output") or []),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Yandex provider failed")
            notes.append(f"Yandex недоступен: {exc}")
        _emit()

    try:
        logger.info("[supplier_search] starting Perplexity stage")
        perplexity_result = search_suppliers_with_perplexity(terms_text, usage_ctx=usage_ctx)
        notes.append("Perplexity обработан")
        logger.info(
            "[supplier_search] Perplexity done: queries=%s sites=%s",
            len(perplexity_result.get("queries") or []),
            len(perplexity_result.get("search_output") or []),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Perplexity provider failed")
        notes.append(f"Perplexity недоступен: {exc}")
        if task_type == "supplier_search_perplexity":
            _emit()
            raise
    _emit()

    # 1) Merge only search websites (without crawling contacts yet).
    combined_search_output = (yandex_result.get("search_output") or []) + (perplexity_result.get("search_output") or [])
    merged_websites = merge_contacts([], combined_search_output)

    websites_to_crawl = [
        {
            "website": item.get("website"),
            "source": item.get("source"),
            "confidence": item.get("confidence"),
            "dedup_key": item.get("dedup_key"),
            "reason": item.get("reason"),
        }
        for item in merged_websites
        if item.get("website")
    ]

    total_sites = len(websites_to_crawl)
    notes.append(f"Найдено сайтов для обхода: {total_sites}")
    _emit()
    notes.pop()  # remove temp marker

    # 2) Crawl merged websites and collect contacts.
    crawl_start = time.time()
    logger.info(
        "[supplier_search] starting crawl of %s websites",
        total_sites,
    )

    # Per-site progress: replace the trailing crawl note in-place after each site
    # so the frontend can show "Краулинг сайтов: 12/47" instead of a single
    # spinner that lasts 15 minutes. We use a closure to reach _emit + notes.
    def _crawl_progress(processed: int, total: int, current_url: str) -> None:
        # Remove any existing crawl-progress note we added previously, then push fresh.
        while notes and notes[-1].startswith("Краулинг сайтов:"):
            notes.pop()
        # Show host only — full URLs make the note unreadable
        host = current_url
        try:
            from urllib.parse import urlparse
            parsed = urlparse(current_url if "://" in current_url else "http://" + current_url)
            host = parsed.netloc or current_url
        except Exception:
            pass
        elapsed_s = int(time.time() - crawl_start)
        eta_str = ""
        if processed > 0 and processed < total:
            avg = elapsed_s / processed
            remaining = int(avg * (total - processed))
            eta_str = f", осталось ~{remaining // 60}м {remaining % 60}с"
        notes.append(
            f"Краулинг сайтов: {processed}/{total} (текущий: {host[:60]}{eta_str})"
        )
        _emit()

    # Emit initial 0/N note so the UI progress bar shows immediately —
    # the first Selenium page load can take minutes on slow sites, and
    # without this the user sees "Найдено сайтов: N" frozen for a while.
    if total_sites > 0:
        first_url = websites_to_crawl[0].get("website") or ""
        _crawl_progress(0, total_sites, first_url)

    try:
        crawled = collect_contacts_from_websites(
            technical_task_text=terms_text,
            websites=websites_to_crawl,
            tz_summary=yandex_result.get("tz_summary"),
            progress_cb=_crawl_progress,
            metrics_cb=metrics_cb,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Website crawl failed")
        notes.append(f"Обход сайтов завершился с ошибкой: {exc}")
        crawled = {"processed_contacts": [], "search_output": []}

    # Strip any leftover crawl-progress note before writing the final state
    while notes and notes[-1].startswith("Краулинг сайтов:"):
        notes.pop()
    crawl_elapsed = int(time.time() - crawl_start)
    logger.info("[supplier_search] crawl finished in %ss", crawl_elapsed)
    merged_contacts = merge_contacts(crawled.get("processed_contacts") or [], crawled.get("search_output") or [])

    merged_search_output = [
        {
            "website": item.get("website"),
            "emails": item.get("emails", []),
            "source": item.get("source"),
            "confidence": item.get("confidence"),
            "dedup_key": item.get("dedup_key"),
        }
        for item in merged_contacts
    ]
    merged_processed_contacts = [
        {
            "website": item.get("website"),
            "is_relevant": item.get("is_relevant", True),
            "reason": item.get("reason"),
            "name": item.get("name"),
            "emails": item.get("emails", []),
            "source": item.get("source"),
            "confidence": item.get("confidence"),
            "dedup_key": item.get("dedup_key"),
        }
        for item in merged_contacts
    ]

    notes.append(f"Обход сайтов выполнен: {len(websites_to_crawl)} шт.")
    final_result = {
        "queries": (yandex_result.get("queries") or []) + (perplexity_result.get("queries") or []),
        "tech_task_excerpt": terms_text[:160],
        "note": "; ".join(notes),
        "search_output": merged_search_output,
        "processed_contacts": merged_processed_contacts,
    }
    _emit(final_result)
    return final_result


def _build_openrouter_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)


def _lot_to_text(name: str, parameters: List[Dict]) -> str:
    params_text = "; ".join(
        [
            f"{item.get('name', '').strip()}: {item.get('value', '').strip()} {item.get('units', '').strip()}".strip()
            for item in parameters
        ]
    )
    return f"Лот: {name.strip()}\nПараметры: {params_text}".strip()


def _param_to_text(param: Dict) -> str:
    return (
        f"{param.get('name', '').strip()}: {param.get('value', '').strip()}"
        f"{(' ' + param.get('units', '').strip()) if param.get('units', '').strip() else ''}"
    ).strip()


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b:
        return -1.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return -1.0
    return dot / (norm_a * norm_b)


def _extract_json_payload(raw_content: str) -> Dict:
    text = (raw_content or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def _classify_match(
    client: OpenAI,
    target_lot: Dict,
    candidate_lots: List[Dict],
) -> Tuple[Optional[int], float, str]:
    target_text = _lot_to_text(target_lot.get("name", ""), target_lot.get("parameters", []))
    candidate_lines = [
        f"{candidate['id']}: {_lot_to_text(candidate.get('name', ''), candidate.get('parameters', []))}"
        for candidate in candidate_lots
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "Ты сопоставляешь лот ТЗ с лотом коммерческого предложения. "
                "Выбери только один id из списка кандидатов или null, если явного соответствия нет. "
                "Ответ только JSON."
            ),
        },
        {
            "role": "user",
            "content": (
                "Лот ТЗ:\n"
                f"{target_text}\n\n"
                "Кандидаты из КП:\n"
                f"{chr(10).join(candidate_lines)}\n\n"
                "Верни JSON формата: "
                '{"matched_candidate_id": <int|null>, "confidence": <0..1>, "reason": "<коротко>"}'
            ),
        },
    ]

    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            extra_body={"usage": {"include": True}},
        )
    except Exception:
        response = client.chat.completions.create(
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            temperature=0,
            extra_body={"usage": {"include": True}},
        )
    record_usage(
        channel="openrouter",
        operation="lot_match_classify",
        model=OPENROUTER_MATCH_MODEL,
        response=response,
    )
    content = response.choices[0].message.content if response.choices else ""
    payload = _extract_json_payload(content or "")
    candidate_ids = {candidate["id"] for candidate in candidate_lots}

    matched_id = payload.get("matched_candidate_id")
    if not isinstance(matched_id, int) or matched_id not in candidate_ids:
        matched_id = None

    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(payload.get("reason") or "")
    return matched_id, confidence, reason


def _classify_param_match(
    client: OpenAI,
    target_param: Dict,
    candidate_params: List[Dict],
) -> Tuple[Optional[int], float, str]:
    target_text = _param_to_text(target_param)
    candidate_lines = [f"{candidate['id']}: {_param_to_text(candidate)}" for candidate in candidate_params]

    messages = [
        {
            "role": "system",
            "content": (
                "Ты сопоставляешь характеристику из технического задания (ТЗ) с характеристикой из коммерческого предложения (КП). "
                "Выбери один id из списка кандидатов, который описывает ту же характеристику, или null если соответствия нет. "
                "Учитывай синонимы: Вес=Масса, ОЗУ=Оперативная память=RAM, Диагональ=Размер экрана, Ёмкость=Объём и т.д. "
                "Характеристики совпадают если описывают одно и то же свойство, даже если значения различаются. "
                "Ответ только JSON."
            ),
        },
        {
            "role": "user",
            "content": (
                "Характеристика ТЗ:\n"
                f"{target_text}\n\n"
                "Кандидаты из КП:\n"
                f"{chr(10).join(candidate_lines)}\n\n"
                "Верни JSON формата: "
                '{"matched_candidate_id": <int|null>, "confidence": <0..1>, "reason": "<коротко>"}'
            ),
        },
    ]

    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            extra_body={"usage": {"include": True}},
        )
    except Exception:
        response = client.chat.completions.create(
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            temperature=0,
            extra_body={"usage": {"include": True}},
        )
    record_usage(
        channel="openrouter",
        operation="param_match_classify",
        model=OPENROUTER_MATCH_MODEL,
        response=response,
    )
    content = response.choices[0].message.content if response.choices else ""
    payload = _extract_json_payload(content or "")
    candidate_ids = {candidate["id"] for candidate in candidate_params}

    matched_id = payload.get("matched_candidate_id")
    if not isinstance(matched_id, int) or matched_id not in candidate_ids:
        matched_id = None

    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(payload.get("reason") or "")
    return matched_id, confidence, reason


def _build_characteristic_rows(
    client: OpenAI,
    lot_params: List[Dict],
    bid_lot_params: List[Dict],
) -> List[Dict]:
    if not lot_params and not bid_lot_params:
        return []
    if not lot_params:
        return [{"left_text": "", "right_text": _param_to_text(param), "status": "unmatched_kp"} for param in bid_lot_params]
    if not bid_lot_params:
        return [{"left_text": _param_to_text(param), "right_text": "", "status": "unmatched_tz"} for param in lot_params]

    lot_params_indexed = [{"id": idx, **param} for idx, param in enumerate(lot_params)]
    bid_params_indexed = [{"id": idx, **param} for idx, param in enumerate(bid_lot_params)]

    all_texts = [_param_to_text(item) for item in lot_params_indexed] + [_param_to_text(item) for item in bid_params_indexed]
    embeddings_response = client.embeddings.create(
        model=OPENROUTER_EMBEDDING_MODEL,
        input=all_texts,
        encoding_format="float",
    )
    indexed_vectors = sorted(embeddings_response.data, key=lambda item: item.index)
    vectors = [item.embedding for item in indexed_vectors]
    lot_vectors = vectors[: len(lot_params_indexed)]
    bid_vectors = vectors[len(lot_params_indexed) :]

    bid_by_id = {item["id"]: item for item in bid_params_indexed}
    # matched_map: lot_param_id → bid_param
    matched_map: Dict[int, Dict] = {}
    used_bid_ids: set[int] = set()

    # Phase 1: Exact name matching (no LLM needed)
    # Normalize name for comparison: lowercase, strip whitespace
    bid_by_name: Dict[str, list] = {}
    for bp in bid_params_indexed:
        norm = (bp.get("name") or "").strip().lower()
        if norm:
            bid_by_name.setdefault(norm, []).append(bp)

    for lot_param in lot_params_indexed:
        lot_name_norm = (lot_param.get("name") or "").strip().lower()
        if not lot_name_norm:
            continue
        candidates = bid_by_name.get(lot_name_norm, [])
        for bp in candidates:
            if bp["id"] not in used_bid_ids:
                matched_map[lot_param["id"]] = bp
                used_bid_ids.add(bp["id"])
                break

    # Phase 2: Embedding + LLM for remaining unmatched params
    unmatched_lot_ids = [lp["id"] for lp in lot_params_indexed if lp["id"] not in matched_map]

    for idx, lot_param in enumerate(lot_params_indexed):
        if lot_param["id"] in matched_map:
            continue  # Already matched in phase 1
        scored = []
        for bid_idx, bid_param in enumerate(bid_params_indexed):
            if bid_param["id"] in used_bid_ids:
                continue
            similarity = _cosine_similarity(lot_vectors[idx], bid_vectors[bid_idx])
            scored.append((similarity, bid_param["id"]))
        scored.sort(key=lambda item: item[0], reverse=True)
        top_candidate_ids = [item[1] for item in scored[:5]]
        top_candidates = [bid_by_id[candidate_id] for candidate_id in top_candidate_ids]
        if not top_candidates:
            continue

        matched_id, confidence, _ = _classify_param_match(client, lot_param, top_candidates)
        if matched_id is None or confidence < LOT_PARAM_MATCH_MIN_CONFIDENCE or matched_id in used_bid_ids:
            continue

        matched_map[lot_param["id"]] = bid_by_id[matched_id]
        used_bid_ids.add(matched_id)

    unmatched_bid_params = [param for param in bid_params_indexed if param["id"] not in used_bid_ids]

    # Phase 3: Value compliance check via single batch LLM call
    matched_pairs = []
    for lot_param in lot_params_indexed:
        bid_param = matched_map.get(lot_param["id"])
        if bid_param:
            matched_pairs.append((lot_param, bid_param))

    compliance = _check_value_compliance(client, matched_pairs)

    # Build rows in TZ parameter order, then unmatched KP at the end
    rows: List[Dict] = []
    pair_idx = 0
    for lot_param in lot_params_indexed:
        bid_param = matched_map.get(lot_param["id"])
        if bid_param:
            status = compliance[pair_idx] if pair_idx < len(compliance) else "matched"
            pair_idx += 1
            rows.append({
                "left_text": _param_to_text(lot_param),
                "right_text": _param_to_text(bid_param),
                "status": status,
            })
        else:
            rows.append({
                "left_text": _param_to_text(lot_param),
                "right_text": "",
                "status": "unmatched_tz",
            })
    rows.extend(
        {
            "left_text": "",
            "right_text": _param_to_text(param),
            "status": "unmatched_kp",
        }
        for param in unmatched_bid_params
    )
    return rows


def _check_value_compliance(
    client: OpenAI,
    matched_pairs: List[Tuple[Dict, Dict]],
) -> List[str]:
    """Check if KP values satisfy TZ requirements. Returns list of statuses:
    'matched' (compliant), 'mismatch' (violates TZ), 'partial' (ambiguous).
    Single batch LLM call for all pairs in a lot.
    """
    if not matched_pairs:
        return []

    lines = []
    for i, (tz, kp) in enumerate(matched_pairs):
        lines.append(f"{i}: ТЗ: {_param_to_text(tz)} | КП: {_param_to_text(kp)}")

    messages = [
        {
            "role": "system",
            "content": (
                "Ты проверяешь, удовлетворяет ли значение из КП (коммерческого предложения) "
                "требованию из ТЗ (технического задания).\n"
                "Правила:\n"
                "- Если ТЗ указывает '≥ X', значение КП должно быть >= X\n"
                "- Если ТЗ указывает '≤ X', значение КП должно быть <= X\n"
                "- Если ТЗ указывает точное значение, КП должно совпадать или быть эквивалентным\n"
                "- Если ТЗ указывает 'Да/Нет', КП должно совпадать\n"
                "- Единицы измерения могут отличаться (кг=Килограмм, ГБ=Гигабайт и т.д.)\n"
                "- Если невозможно однозначно определить — ставь 'partial'\n"
                "Ответ строго JSON."
            ),
        },
        {
            "role": "user",
            "content": (
                "Проверь каждую пару. Для каждого номера верни статус.\n\n"
                + "\n".join(lines)
                + "\n\nВерни JSON: {\"results\": [{\"id\": 0, \"status\": \"ok|mismatch|partial\", \"reason\": \"коротко\"}]}"
            ),
        },
    ]

    try:
        response = client.chat.completions.create(
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            max_completion_tokens=4000,
            extra_body={"usage": {"include": True}},
        )
    except Exception:
        response = client.chat.completions.create(
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            temperature=0,
            max_completion_tokens=4000,
            extra_body={"usage": {"include": True}},
        )
    record_usage(
        channel="openrouter",
        operation="value_compliance_check",
        model=OPENROUTER_MATCH_MODEL,
        response=response,
    )
    content = response.choices[0].message.content if response.choices else ""
    payload = _extract_json_payload(content or "")
    results_list = payload.get("results", [])

    # Map results by id
    result_by_id = {}
    for r in results_list:
        if isinstance(r, dict) and "id" in r:
            result_by_id[r["id"]] = r.get("status", "matched")

    statuses = []
    for i in range(len(matched_pairs)):
        raw = result_by_id.get(i, "matched")
        if raw == "ok":
            statuses.append("matched")
        elif raw == "mismatch":
            statuses.append("mismatch")
        elif raw == "partial":
            statuses.append("partial")
        else:
            statuses.append("matched")
    return statuses


def _build_lot_comparison_rows(session: Session, purchase_id: int, bid_id: int,
                               progress_cb=None) -> Dict:
    def _progress(stages):
        if progress_cb:
            progress_cb({"stages": stages, "note": ""})

    stages = [
        {"name": "Загрузка данных", "status": "in_progress", "detail": ""},
        {"name": "Эмбеддинги лотов", "status": "pending", "detail": ""},
        {"name": "Сопоставление лотов (LLM)", "status": "pending", "detail": ""},
        {"name": "Сопоставление характеристик", "status": "pending", "detail": ""},
        {"name": "Формирование результата", "status": "pending", "detail": ""},
    ]
    _progress(stages)

    purchase_lots = session.exec(select(Lot).where(Lot.purchase_id == purchase_id).order_by(Lot.id)).all()
    bid_lots = session.exec(select(BidLot).where(BidLot.bid_id == bid_id).order_by(BidLot.id)).all()

    purchase_items = []
    for lot in purchase_lots:
        params = session.exec(select(LotParameter).where(LotParameter.lot_id == lot.id).order_by(LotParameter.id)).all()
        purchase_items.append(
            {
                "id": lot.id,
                "name": lot.name,
                "parameters": [
                    {"name": param.name, "value": param.value, "units": param.units}
                    for param in params
                ],
            }
        )

    bid_items = []
    for lot in bid_lots:
        params = session.exec(select(BidLotParameter).where(BidLotParameter.bid_lot_id == lot.id).order_by(BidLotParameter.id)).all()
        bid_items.append(
            {
                "id": lot.id,
                "name": lot.name,
                "price": lot.price,
                "parameters": [
                    {"name": param.name, "value": param.value, "units": param.units}
                    for param in params
                ],
            }
        )

    stages[0]["status"] = "done"
    stages[0]["detail"] = f"ТЗ: {len(purchase_items)} лотов, КП: {len(bid_items)} лотов"
    _progress(stages)

    if not purchase_items:
        return {"rows": [], "note": "Лоты ТЗ не найдены", "stages": stages}
    if not bid_items:
        return {
            "rows": [
                {
                    "lot_id": item["id"],
                    "lot_name": item["name"],
                    "lot_parameters": item["parameters"],
                    "bid_lot_id": None,
                    "bid_lot_name": None,
                    "bid_lot_price": None,
                    "bid_lot_parameters": [],
                    "confidence": None,
                    "reason": "Лоты КП не найдены",
                    "characteristic_rows": [
                        {
                            "left_text": _param_to_text(param),
                            "right_text": "",
                            "status": "unmatched_tz",
                        }
                        for param in item["parameters"]
                    ],
                }
                for item in purchase_items
            ],
            "note": "Лоты КП не найдены",
        }

    # Stage 2: Embeddings
    stages[1]["status"] = "in_progress"
    _progress(stages)

    client = _build_openrouter_client()
    all_texts = [_lot_to_text(item["name"], item["parameters"]) for item in purchase_items] + [
        _lot_to_text(item["name"], item["parameters"]) for item in bid_items
    ]
    embeddings_response = client.embeddings.create(
        model=OPENROUTER_EMBEDDING_MODEL,
        input=all_texts,
        encoding_format="float",
    )
    indexed_vectors = sorted(embeddings_response.data, key=lambda item: item.index)
    vectors = [item.embedding for item in indexed_vectors]
    purchase_vectors = vectors[: len(purchase_items)]
    bid_vectors = vectors[len(purchase_items) :]

    stages[1]["status"] = "done"
    stages[1]["detail"] = f"{len(all_texts)} векторов"
    stages[2]["status"] = "in_progress"
    _progress(stages)

    # Stage 3: Lot matching (LLM)
    bid_by_id = {item["id"]: item for item in bid_items}
    rows = []
    matched_count = 0

    for idx, purchase_item in enumerate(purchase_items):
        scored = []
        for bid_idx, bid_item in enumerate(bid_items):
            similarity = _cosine_similarity(purchase_vectors[idx], bid_vectors[bid_idx])
            scored.append((similarity, bid_item["id"]))
        scored.sort(key=lambda item: item[0], reverse=True)
        top_candidates_ids = [item[1] for item in scored[:3]]
        top_candidates = [bid_by_id[candidate_id] for candidate_id in top_candidates_ids]

        matched_id, confidence, reason = _classify_match(client, purchase_item, top_candidates)
        if confidence < LOT_MATCH_MIN_CONFIDENCE:
            matched_id = None
        matched_item = bid_by_id.get(matched_id) if matched_id is not None else None
        if matched_item:
            matched_count += 1

        stages[2]["detail"] = f"{idx + 1} из {len(purchase_items)} лотов"
        _progress(stages)

        rows.append(
            {
                "lot_id": purchase_item["id"],
                "lot_name": purchase_item["name"],
                "lot_parameters": purchase_item["parameters"],
                "bid_lot_id": matched_item["id"] if matched_item else None,
                "bid_lot_name": matched_item["name"] if matched_item else None,
                "bid_lot_price": matched_item.get("price") if matched_item else None,
                "bid_lot_parameters": matched_item["parameters"] if matched_item else [],
                "confidence": confidence if matched_item else None,
                "reason": reason or None,
                "matched_item_ref": matched_item,  # temp ref for stage 4
            }
        )

    stages[2]["status"] = "done"
    stages[2]["detail"] = f"Сопоставлено {matched_count} из {len(purchase_items)}"
    stages[3]["status"] = "in_progress"
    _progress(stages)

    # Stage 4: Characteristic matching
    chars_done = 0
    chars_total = sum(1 for r in rows if r.get("matched_item_ref"))
    for row in rows:
        matched_item = row.pop("matched_item_ref", None)
        if matched_item:
            row["characteristic_rows"] = _build_characteristic_rows(
                client,
                row["lot_parameters"],
                matched_item["parameters"],
            )
            chars_done += 1
            stages[3]["detail"] = f"{chars_done} из {chars_total} лотов"
            _progress(stages)
        else:
            row["characteristic_rows"] = [
                {
                    "left_text": _param_to_text(param),
                    "right_text": "",
                    "status": "unmatched_tz",
                }
                for param in row["lot_parameters"]
            ]

    stages[3]["status"] = "done"
    stages[4]["status"] = "done"
    stages[4]["detail"] = f"{len(rows)} лотов"
    _progress(stages)

    return {
        "rows": rows,
        "note": f"Сопоставлено лотов: {matched_count} из {len(purchase_items)}",
        "stages": stages,
    }


def _process_lot_comparison_task(task: LLMTask) -> None:
    payload = TaskQueue._load_payload(task.input_text)
    try:
        purchase_id = int(payload.get("purchase_id") or task.purchase_id or 0)
        bid_id = int(payload.get("bid_id") or task.bid_id or 0)
    except (TypeError, ValueError):
        purchase_id = 0
        bid_id = 0
    if not purchase_id or not bid_id:
        raise RuntimeError("lot_comparison task requires purchase_id and bid_id")

    task_id = task.id
    set_usage_context({"purchase_id": purchase_id, "task_id": task_id})
    try:
        with Session(engine) as session:
            task_in_db = session.get(LLMTask, task_id)
            if not task_in_db:
                return

            result = _build_lot_comparison_rows(
                session, purchase_id, bid_id,
                progress_cb=lambda partial: _write_progress(task_id, partial),
            )
            task_in_db.output_text = json.dumps(result, ensure_ascii=False)
            task_in_db.status = "completed"
            session.add(task_in_db)
            session.commit()
    finally:
        set_usage_context(None)


def _write_progress(task_id: int, partial: Dict) -> None:
    """Persist intermediate progress so the frontend polling can see it."""
    from datetime import datetime
    with Session(engine) as session:
        task_in_db = session.get(LLMTask, task_id)
        if not task_in_db:
            return
        # Keep status as in_progress; we just want to surface the note.
        task_in_db.output_text = json.dumps(partial, ensure_ascii=False)
        task_in_db.updated_at = datetime.utcnow()
        session.add(task_in_db)
        session.commit()
    logger.info("[progress] task=%s note=%r", task_id, partial.get("note"))


def _process_task_safe(task: LLMTask) -> None:
    """Thread-safe wrapper: catches all exceptions and marks task as failed."""
    try:
        _process_task(task)
    except Exception as exc:
        logger.exception("[etl] unhandled error in task %s", task.id)
        try:
            with Session(engine) as session:
                t = session.get(LLMTask, task.id)
                if t and t.status != "completed":
                    t.status = "failed"
                    t.output_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    t.updated_at = datetime.utcnow()
                    session.add(t)
                    session.commit()
        except Exception:
            logger.exception("[etl] failed to mark task %s as failed", task.id)


def _process_task(task: LLMTask) -> None:
    if task.task_type == "lot_comparison":
        _process_lot_comparison_task(task)
        return

    payload = TaskQueue._load_payload(task.input_text)
    terms_text = payload.get("terms_text", "")

    logger.info("Starting supplier search task %s", task.id)
    task_id = task.id  # capture for closure
    purchase_id = task.purchase_id
    # Установим контекст для всех LLM/Yandex вызовов внутри пайплайна,
    # чтобы record_usage автоматически связывал записи с этой задачей.
    usage_ctx = {"purchase_id": task.purchase_id, "task_id": task.id}
    set_usage_context(usage_ctx)

    def _write_site_metric(metric: Dict) -> None:
        try:
            with Session(engine) as session:
                row = SiteCrawlMetric(
                    task_id=task_id,
                    purchase_id=purchase_id,
                    url=metric.get("url") or "",
                    domain=metric.get("domain"),
                    http_status=metric.get("http_status"),
                    http_text_len=metric.get("http_text_len"),
                    http_emails_count=metric.get("http_emails_count") or 0,
                    http_duration_ms=metric.get("http_duration_ms"),
                    http_error=metric.get("http_error"),
                    render_type=metric.get("render_type"),
                    selenium_duration_ms=metric.get("selenium_duration_ms"),
                    selenium_emails_count=metric.get("selenium_emails_count") or 0,
                    is_relevant=metric.get("is_relevant"),
                    total_duration_ms=metric.get("total_duration_ms"),
                )
                session.add(row)
                session.commit()
        except Exception:
            logger.exception("failed to persist SiteCrawlMetric")

    try:
        result = _collect_combined_contacts(
            terms_text,
            task.task_type,
            progress_cb=lambda partial: _write_progress(task_id, partial),
            usage_ctx=usage_ctx,
            metrics_cb=_write_site_metric,
        )
    finally:
        set_usage_context(None)

    with Session(engine) as session:
        task_in_db = session.get(LLMTask, task.id)
        if not task_in_db:
            return

        from datetime import datetime
        created_suppliers: List[Dict] = []
        try:
            created_suppliers = _upsert_suppliers(session, task_in_db, result.get("processed_contacts", []))
            if task_in_db.purchase_id:
                purchase = session.get(Purchase, task_in_db.purchase_id)
                if purchase and created_suppliers:
                    purchase.status = "suppliers_found"
                    session.add(purchase)

            note = result.get("note") or "Поиск поставщиков завершён"
            payload = result | {"created_suppliers": created_suppliers, "note": note}
            task_in_db.output_text = json.dumps(payload, ensure_ascii=False)
            task_in_db.status = "completed"
            task_in_db.updated_at = datetime.utcnow()
            session.add(task_in_db)
            session.commit()
            logger.info("Finished supplier search task %s", task.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Supplier ETL failed for task %s", task.id)
            task_in_db.status = "failed"
            task_in_db.output_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
            task_in_db.updated_at = datetime.utcnow()
            session.add(task_in_db)
            session.commit()
        finally:
            shutdown_driver()


MAX_RECOVERY_AGE_SECONDS = 30 * 60  # 30 minutes


def _recover_stale_tasks() -> None:
    """Reset tasks left in 'in_progress' on a previous worker run.

    Two cases:

    1. Task was started recently (< 30 min ago) and the container died
       mid-run. Requeue it so the worker picks it up. The user has been
       waiting and will see incremental progress on next pickup.

    2. Task was started > 30 min ago. This usually means we've been
       restarting it across multiple deploys (the supplier_search
       pipeline can take 15+ min, so a deploy in the middle kills it,
       and on restart we requeue it, restarting from scratch — repeat).
       In this case, mark it FAILED with a clear message so the user
       can manually retry instead of being trapped in an infinite
       restart loop.
    """
    from datetime import datetime
    now = datetime.utcnow()
    with Session(engine) as session:
        stale = session.exec(
            select(LLMTask).where(
                LLMTask.status == "in_progress",
                LLMTask.task_type.in_(WORKER_TASK_TYPES),
            )
        ).all()
        for t in stale:
            age = (now - t.created_at).total_seconds() if t.created_at else 0
            if age > MAX_RECOVERY_AGE_SECONDS:
                logger.warning(
                    "[etl] task id=%s type=%s is too old to recover (age=%.0fs > %ds) — marking failed",
                    t.id,
                    t.task_type,
                    age,
                    MAX_RECOVERY_AGE_SECONDS,
                )
                t.status = "failed"
                t.output_text = json.dumps(
                    {
                        "error": (
                            f"Задача отменена: возраст {int(age // 60)} мин превышает лимит "
                            f"восстановления ({MAX_RECOVERY_AGE_SECONDS // 60} мин). "
                            f"Скорее всего пайплайн перезапускался несколько раз из-за деплоев. "
                            f"Запустите поиск заново."
                        )
                    },
                    ensure_ascii=False,
                )
                t.updated_at = now
                session.add(t)
                continue
            logger.warning(
                "[etl] requeueing stale task id=%s type=%s age=%.0fs",
                t.id,
                t.task_type,
                age,
            )
            t.status = "queued"
            t.updated_at = now
            session.add(t)
        if stale:
            session.commit()


def _claim_next_task(exclude_purchase_ids: set[int]) -> Optional[LLMTask]:
    """Claim one queued task using SELECT FOR UPDATE SKIP LOCKED.

    Fair scheduling: round-robin by purchase (user) — tasks from purchases
    already being processed are deprioritised so one user's batch doesn't
    block everyone else.
    """
    with Session(engine) as session:
        # First try a task from a purchase NOT already in-flight
        task = _try_claim(session, exclude_purchase_ids)
        if task:
            return task
        # If all queued tasks belong to already-active purchases, take any
        task = _try_claim(session, set())
        return task


def _try_claim(session: Session, exclude_purchase_ids: set[int]) -> Optional[LLMTask]:
    """Attempt to claim a single queued task, optionally skipping certain purchases."""
    query = (
        select(LLMTask)
        .where(
            LLMTask.status == "queued",
            LLMTask.task_type.in_(WORKER_TASK_TYPES),
        )
    )
    if exclude_purchase_ids:
        query = query.where(LLMTask.purchase_id.notin_(exclude_purchase_ids))
    query = query.order_by(LLMTask.created_at).limit(1)

    # FOR UPDATE SKIP LOCKED prevents multiple threads from claiming the same task
    query = query.with_for_update(skip_locked=True)

    task = session.exec(query).first()
    if not task:
        return None

    task.status = "in_progress"
    task.updated_at = datetime.utcnow()
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def _get_active_purchase_ids(futures: dict[Future, int]) -> set[int]:
    """Return purchase_ids of currently running tasks."""
    return {pid for pid in futures.values() if pid is not None}


def run_worker() -> None:
    create_db_and_tables()
    logger.info(
        "[etl] worker starting (max_workers=%d, task_types=%s)",
        MAX_WORKERS,
        ",".join(WORKER_TASK_TYPES),
    )
    _recover_stale_tasks()

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    # Map: Future -> purchase_id (for fair scheduling)
    futures: dict[Future, int] = {}

    while True:
        # Clean up completed futures
        done = [f for f in futures if f.done()]
        for f in done:
            futures.pop(f)
            exc = f.exception()
            if exc:
                logger.error("[etl] task thread raised: %s", exc)

        # If pool is full, wait a bit
        if len(futures) >= MAX_WORKERS:
            time.sleep(POLL_INTERVAL)
            continue

        active_purchases = _get_active_purchase_ids(futures)
        task = _claim_next_task(active_purchases)

        if not task:
            time.sleep(POLL_INTERVAL)
            continue

        logger.info("[etl] dispatching task id=%s type=%s purchase=%s", task.id, task.task_type, task.purchase_id)
        future = executor.submit(_process_task_safe, task)
        futures[future] = task.purchase_id or 0


def main() -> None:
    try:
        run_worker()
    except KeyboardInterrupt:
        logger.info("ETL worker stopped")


if __name__ == "__main__":
    main()
