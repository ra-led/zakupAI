import json
import logging
import os
import time
from typing import Dict, List

from sqlmodel import Session, select

from app.database import create_db_and_tables, engine
from app.models import LLMTask, Purchase, Supplier, SupplierContact
from app.task_queue import TaskQueue
from suppliers_contacts import collect_contacts_from_text, shutdown_driver

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL = float(os.getenv("ETL_POLL_INTERVAL", "5"))


def _upsert_suppliers(session: Session, task: LLMTask, processed_contacts: List[Dict], search_output: List[Dict]) -> List[Dict]:
    created: List[Dict] = []
    email_map = {entry.get("website"): entry.get("emails", []) for entry in search_output}

    for contact in processed_contacts:
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
                relevance_score=1.0,
                reason=contact.get("reason"),
            )
            session.add(supplier)
            session.commit()
            session.refresh(supplier)
        elif not supplier.reason:
            supplier.reason = contact.get("reason")

        for email in email_map.get(website, []):
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
                        reason=contact.get("reason"),
                        is_selected_for_request=False,
                    )
                )

        created.append({"supplier_id": supplier.id, "website": website, "emails": email_map.get(website, [])})

    session.commit()
    return created


def _process_task(task: LLMTask) -> None:
    payload = TaskQueue._load_payload(task.input_text)
    terms_text = payload.get("terms_text", "")

    logger.info("Starting supplier search task %s", task.id)
    result = collect_contacts_from_text(terms_text)

    with Session(engine) as session:
        task_in_db = session.get(LLMTask, task.id)
        if not task_in_db:
            return

        created_suppliers: List[Dict] = []
        try:
            created_suppliers = _upsert_suppliers(
                session, task_in_db, result.get("processed_contacts", []), result.get("search_output", [])
            )
            if task_in_db.purchase_id:
                purchase = session.get(Purchase, task_in_db.purchase_id)
                if purchase and created_suppliers:
                    purchase.status = "suppliers_found"
                    session.add(purchase)

            payload = result | {"created_suppliers": created_suppliers, "note": "Поиск поставщиков завершён"}
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
                .where(LLMTask.status == "queued", LLMTask.task_type == "supplier_search")
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
