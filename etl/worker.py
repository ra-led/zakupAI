import json
import logging
import os
import time
import math
from typing import Dict, List, Optional, Tuple

from openai import OpenAI
from sqlmodel import Session, select

from app.database import create_db_and_tables, engine
from app.models import BidLot, BidLotParameter, LLMTask, Lot, LotParameter, Purchase, Supplier, SupplierContact
from app.search_providers.perplexity import search_suppliers_with_perplexity
from app.supplier_import import merge_contacts
from app.task_queue import TaskQueue
from suppliers_contacts import (
    collect_contacts_from_websites,
    collect_yandex_search_output_from_text,
    shutdown_driver,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = float(os.getenv("ETL_POLL_INTERVAL", "5"))

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "perplexity/pplx-embed-v1-4b")
OPENROUTER_MATCH_MODEL = os.getenv("OPENROUTER_MATCH_MODEL", "openai/gpt-4o-mini")
LOT_MATCH_MIN_CONFIDENCE = float(os.getenv("LOT_MATCH_MIN_CONFIDENCE", "0.45"))
LOT_PARAM_MATCH_MIN_CONFIDENCE = float(os.getenv("LOT_PARAM_MATCH_MIN_CONFIDENCE", "0.45"))


def _chat_completion_no_reasoning(client: OpenAI, **kwargs):
    payload = dict(kwargs)
    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict):
        merged = dict(extra_body)
        merged["reasoning"] = {"enabled": False}
        payload["extra_body"] = merged
    else:
        payload["extra_body"] = {"reasoning": {"enabled": False}}
    return client.chat.completions.create(**payload)


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


def _collect_combined_contacts(terms_text: str, task_type: str) -> Dict:
    yandex_result: Dict = {"queries": [], "search_output": [], "processed_contacts": [], "tz_summary": None}
    perplexity_result: Dict = {"queries": [], "search_output": [], "processed_contacts": []}
    notes: List[str] = []

    if task_type == "supplier_search":
        try:
            yandex_result = collect_yandex_search_output_from_text(terms_text)
            notes.append("Yandex поиск обработан")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Yandex provider failed")
            notes.append(f"Yandex недоступен: {exc}")

    try:
        perplexity_result = search_suppliers_with_perplexity(terms_text)
        notes.append("Perplexity обработан")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Perplexity provider failed")
        notes.append(f"Perplexity недоступен: {exc}")
        if task_type == "supplier_search_perplexity":
            raise

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

    # 2) Crawl merged websites and collect contacts.
    try:
        crawled = collect_contacts_from_websites(
            technical_task_text=terms_text,
            websites=websites_to_crawl,
            tz_summary=yandex_result.get("tz_summary"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Website crawl failed")
        notes.append(f"Обход сайтов завершился с ошибкой: {exc}")
        crawled = {"processed_contacts": [], "search_output": []}
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
    return {
        "queries": (yandex_result.get("queries") or []) + (perplexity_result.get("queries") or []),
        "tech_task_excerpt": terms_text[:160],
        "note": "; ".join(notes + [f"Обход сайтов выполнен: {len(websites_to_crawl)} шт."]),
        "search_output": merged_search_output,
        "processed_contacts": merged_processed_contacts,
    }


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
        response = _chat_completion_no_reasoning(
            client,
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception:
        response = _chat_completion_no_reasoning(
            client,
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            temperature=0,
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
                "Ты сопоставляешь характеристику из ТЗ с характеристикой из КП. "
                "Выбери один id из списка кандидатов или null, если соответствия нет. "
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
        response = _chat_completion_no_reasoning(
            client,
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception:
        response = _chat_completion_no_reasoning(
            client,
            model=OPENROUTER_MATCH_MODEL,
            messages=messages,
            temperature=0,
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
    matched_pairs: List[Tuple[Dict, Dict]] = []
    unmatched_lot_params: List[Dict] = []
    used_bid_ids: set[int] = set()

    for idx, lot_param in enumerate(lot_params_indexed):
        scored = []
        for bid_idx, bid_param in enumerate(bid_params_indexed):
            if bid_param["id"] in used_bid_ids:
                continue
            similarity = _cosine_similarity(lot_vectors[idx], bid_vectors[bid_idx])
            scored.append((similarity, bid_param["id"]))
        scored.sort(key=lambda item: item[0], reverse=True)
        top_candidate_ids = [item[1] for item in scored[:3]]
        top_candidates = [bid_by_id[candidate_id] for candidate_id in top_candidate_ids]
        if not top_candidates:
            unmatched_lot_params.append(lot_param)
            continue

        matched_id, confidence, _ = _classify_param_match(client, lot_param, top_candidates)
        if matched_id is None or confidence < LOT_PARAM_MATCH_MIN_CONFIDENCE or matched_id in used_bid_ids:
            unmatched_lot_params.append(lot_param)
            continue

        matched_bid_param = bid_by_id[matched_id]
        used_bid_ids.add(matched_id)
        matched_pairs.append((lot_param, matched_bid_param))

    unmatched_bid_params = [param for param in bid_params_indexed if param["id"] not in used_bid_ids]

    rows: List[Dict] = []
    rows.extend(
        {
            "left_text": _param_to_text(param),
            "right_text": "",
            "status": "unmatched_tz",
        }
        for param in unmatched_lot_params
    )
    rows.extend(
        {
            "left_text": _param_to_text(left_param),
            "right_text": _param_to_text(right_param),
            "status": "matched",
        }
        for left_param, right_param in matched_pairs
    )
    rows.extend(
        {
            "left_text": "",
            "right_text": _param_to_text(param),
            "status": "unmatched_kp",
        }
        for param in unmatched_bid_params
    )
    return rows


def _build_lot_comparison_rows(session: Session, purchase_id: int, bid_id: int) -> Dict:
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

    if not purchase_items:
        return {"rows": [], "note": "Лоты ТЗ не найдены"}
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
                "characteristic_rows": (
                    _build_characteristic_rows(client, purchase_item["parameters"], matched_item["parameters"])
                    if matched_item
                    else [
                        {
                            "left_text": _param_to_text(param),
                            "right_text": "",
                            "status": "unmatched_tz",
                        }
                        for param in purchase_item["parameters"]
                    ]
                ),
            }
        )

    return {
        "rows": rows,
        "note": f"Сопоставлено лотов: {matched_count} из {len(purchase_items)}",
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

    with Session(engine) as session:
        task_in_db = session.get(LLMTask, task.id)
        if not task_in_db:
            return

        result = _build_lot_comparison_rows(session, purchase_id, bid_id)
        task_in_db.output_text = json.dumps(result, ensure_ascii=False)
        task_in_db.status = "completed"
        session.add(task_in_db)
        session.commit()


def _process_task(task: LLMTask) -> None:
    if task.task_type == "lot_comparison":
        _process_lot_comparison_task(task)
        return

    payload = TaskQueue._load_payload(task.input_text)
    terms_text = payload.get("terms_text", "")

    logger.info("Starting supplier search task %s", task.id)
    result = _collect_combined_contacts(terms_text, task.task_type)

    with Session(engine) as session:
        task_in_db = session.get(LLMTask, task.id)
        if not task_in_db:
            return

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
            session.add(task_in_db)
            session.commit()
            logger.info("Finished supplier search task %s", task.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Supplier ETL failed for task %s", task.id)
            task_in_db.status = "failed"
            task_in_db.output_text = f"error: {exc}"
            session.add(task_in_db)
            session.commit()
        finally:
            shutdown_driver()


def run_worker() -> None:
    create_db_and_tables()
    while True:
        with Session(engine) as session:
            task = session.exec(
                select(LLMTask)
                .where(
                    LLMTask.status == "queued",
                    LLMTask.task_type.in_(["supplier_search", "supplier_search_perplexity", "lot_comparison"]),
                )
                .order_by(LLMTask.created_at)
            ).first()

            if not task:
                time.sleep(POLL_INTERVAL)
                continue

            task.status = "in_progress"
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        if task_id:
            _process_task(task)


def main() -> None:
    try:
        run_worker()
    except KeyboardInterrupt:
        logger.info("ETL worker stopped")


if __name__ == "__main__":
    main()
