import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from .database import engine
from .llm_stub import build_search_queries
from .models import LLMTask, Purchase


@dataclass
class SupplierSearchState:
    task_id: int
    status: str
    queries: List[str]
    note: str
    tech_task_excerpt: str
    search_output: List[Dict[str, Any]]
    processed_contacts: List[Dict[str, Any]]
    queue_length: int = 0
    estimated_complete_time: Optional[datetime] = None


class TaskQueue:
    def __init__(self, poll_interval: float = 2.0) -> None:
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def enqueue_supplier_search_task(
        self, purchase_id: int, terms_text: str, hints: Optional[List[str]] = None
    ) -> LLMTask:
        """Create a task that will search suppliers based on technical task text."""
        payload = {"terms_text": terms_text or "", "hints": hints or []}
        with Session(engine) as session:
            existing = session.exec(
                select(LLMTask)
                .where(
                    LLMTask.purchase_id == purchase_id,
                    LLMTask.task_type == "supplier_search",
                    LLMTask.status.in_(["queued", "in_progress"]),
                )
                .order_by(LLMTask.created_at.desc())
            ).first()
            if existing:
                return existing

            task = LLMTask(
                purchase_id=purchase_id,
                task_type="supplier_search",
                input_text=json.dumps(payload, ensure_ascii=False),
                status="queued",
            )
            session.add(task)
            purchase = session.get(Purchase, purchase_id)
            if purchase and purchase.status == "draft":
                purchase.status = "searching_suppliers"
                session.add(purchase)
            session.commit()
            session.refresh(task)
            return task

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with Session(engine) as session:
                task = session.exec(
                    select(LLMTask)
                    .where(LLMTask.status == "queued")
                    .order_by(LLMTask.created_at)
                ).first()
                if not task:
                    time.sleep(self.poll_interval)
                    continue

                task.status = "in_progress"
                session.add(task)
                session.commit()
                session.refresh(task)
                task_id = task.id

            if task_id is None:
                continue

            try:
                self._process_task(task_id)
            except Exception as exc:  # pragma: no cover - diagnostic only
                with Session(engine) as session:
                    errored = session.get(LLMTask, task_id)
                    if errored:
                        errored.status = "failed"
                        errored.output_text = f"error: {exc}"
                        session.add(errored)
                        session.commit()

    def _process_task(self, task_id: int) -> None:
        with Session(engine) as session:
            task = session.get(LLMTask, task_id)
            if not task:
                return

            if task.task_type == "supplier_search":
                payload = self._load_payload(task.input_text)
                terms_text = payload.get("terms_text", "")
                hints = payload.get("hints") or []
                plan = build_search_queries(terms_text, hints)
                task.output_text = json.dumps(
                    {
                        "queries": plan.queries,
                        "note": plan.note,
                        "tech_task_excerpt": terms_text[:160],
                    },
                    ensure_ascii=False,
                )
                task.status = "completed"
                if task.purchase_id:
                    purchase = session.get(Purchase, task.purchase_id)
                    if purchase:
                        purchase.status = "suppliers_found"
                        session.add(purchase)
            else:
                task.status = "completed"

            session.add(task)
            session.commit()

    @staticmethod
    def _load_payload(raw_text: str) -> Dict[str, Any]:
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        return {"terms_text": raw_text, "hints": []}


def get_supplier_search_state(purchase_id: int) -> Optional[SupplierSearchState]:
    with Session(engine) as session:
        task = session.exec(
            select(LLMTask)
            .where(
                LLMTask.purchase_id == purchase_id,
                LLMTask.task_type == "supplier_search",
            )
            .order_by(LLMTask.created_at.desc())
        ).first()

        if not task:
            return None

        queries: List[str] = []
        note = ""
        tech_task_excerpt = ""
        search_output: List[Dict[str, Any]] = []
        processed_contacts: List[Dict[str, Any]] = []
        if task.output_text:
            payload = TaskQueue._load_payload(task.output_text)
            queries = payload.get("queries") or []
            note = payload.get("note") or payload.get("status") or "Поиск поставщиков выполняется"
            tech_task_excerpt = payload.get("tech_task_excerpt") or ""
            search_output = payload.get("search_output") or []
            processed_contacts = payload.get("processed_contacts") or []

        queue_length = get_supplier_search_queue_length(session)
        estimated_complete_time: Optional[datetime] = None
        if task.status in ("queued", "in_progress"):
            estimated_complete_time = datetime.utcnow() + timedelta(minutes=10 + queue_length * 10)

        return SupplierSearchState(
            task_id=task.id or 0,
            status=task.status,
            queries=queries,
            note=note,
            tech_task_excerpt=tech_task_excerpt,
            search_output=search_output,
            processed_contacts=processed_contacts,
            queue_length=queue_length,
            estimated_complete_time=estimated_complete_time,
        )


def get_supplier_search_queue_length(session: Optional[Session] = None) -> int:
    if session is None:
        with Session(engine) as managed_session:
            queued_tasks = managed_session.exec(
                select(LLMTask).where(
                    LLMTask.task_type == "supplier_search", LLMTask.status == "queued"
                )
            ).all()
            return len(queued_tasks)

    queued_tasks = session.exec(
        select(LLMTask).where(LLMTask.task_type == "supplier_search", LLMTask.status == "queued")
    ).all()
    return len(queued_tasks)


task_queue = TaskQueue()
