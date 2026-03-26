import json
import logging
import os
import time
from typing import Dict, List

from sqlmodel import Session, select

from app.database import create_db_and_tables, engine
from app.models import LLMTask, Purchase, Supplier, SupplierContact
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
    crawled = collect_contacts_from_websites(
        technical_task_text=terms_text,
        websites=websites_to_crawl,
        tz_summary=yandex_result.get("tz_summary"),
    )
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


def _process_task(task: LLMTask) -> None:
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
                    LLMTask.task_type.in_(["supplier_search", "supplier_search_perplexity"]),
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
