import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from .database import engine
from .services.llm_tasks import build_search_queries, extract_bid_lots, extract_lots
from .models import BidLot, BidLotParameter, LLMTask, Lot, LotParameter, Purchase

EMBEDDED_MAX_WORKERS = int(os.getenv("EMBEDDED_MAX_WORKERS", "4"))
EMBEDDED_TASK_TYPES = ["lots_extraction", "bid_lots_extraction"]


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
        """Create a combined supplier search task (Yandex + Perplexity)."""
        return self._enqueue_supplier_task("supplier_search", purchase_id, terms_text, hints)

    def enqueue_supplier_search_perplexity_task(
        self, purchase_id: int, terms_text: str, hints: Optional[List[str]] = None
    ) -> LLMTask:
        """Create a supplier search task using only Perplexity."""
        return self._enqueue_supplier_task("supplier_search_perplexity", purchase_id, terms_text, hints)

    def _enqueue_supplier_task(
        self, task_type: str, purchase_id: int, terms_text: str, hints: Optional[List[str]] = None
    ) -> LLMTask:
        payload = {"terms_text": terms_text or "", "hints": hints or []}
        with Session(engine) as session:
            existing = session.exec(
                select(LLMTask)
                .where(
                    LLMTask.purchase_id == purchase_id,
                    LLMTask.task_type == task_type,
                    LLMTask.status.in_(["queued", "in_progress"]),
                )
                .order_by(LLMTask.created_at.desc())
            ).first()
            if existing:
                return existing

            task = LLMTask(
                purchase_id=purchase_id,
                task_type=task_type,
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

    # If a task has been "in_progress" longer than this, we assume the
    # worker died mid-call (or LLM hung past timeout) and reclaim it.
    # OpenAI client timeout is 180s, so 5 minutes is generous.
    STUCK_IN_PROGRESS_SECONDS = 300

    def enqueue_lots_extraction_task(self, purchase_id: int, terms_text: str) -> LLMTask:
        payload = {"terms_text": terms_text or ""}
        terms_len = len(terms_text or "")
        print(
            f"[lots_extraction] enqueue called purchase={purchase_id} terms_len={terms_len} "
            f"worker_alive={self._thread.is_alive() if hasattr(self, '_thread') else 'N/A'}"
        )
        with Session(engine) as session:
            # Reclaim stuck in_progress tasks for THIS purchase before
            # checking for existing — otherwise enqueue returns the stuck
            # task forever and the user can never retry.
            now = datetime.utcnow()
            stuck = session.exec(
                select(LLMTask)
                .where(
                    LLMTask.purchase_id == purchase_id,
                    LLMTask.task_type == "lots_extraction",
                    LLMTask.status == "in_progress",
                )
            ).all()
            for s in stuck:
                age = (now - s.created_at).total_seconds() if s.created_at else 0
                if age > self.STUCK_IN_PROGRESS_SECONDS:
                    print(
                        f"[lots_extraction] reclaiming stuck task id={s.id} age={age:.0f}s "
                        f"-> failed"
                    )
                    s.status = "failed"
                    s.output_text = json.dumps(
                        {"error": f"Task abandoned after {age:.0f}s in_progress"},
                        ensure_ascii=False,
                    )
                    session.add(s)
            if stuck:
                session.commit()

            existing = session.exec(
                select(LLMTask)
                .where(
                    LLMTask.purchase_id == purchase_id,
                    LLMTask.task_type == "lots_extraction",
                    LLMTask.status.in_(["queued", "in_progress"]),
                )
                .order_by(LLMTask.created_at.desc())
            ).first()
            if existing:
                print(
                    f"[lots_extraction] enqueue skipped: existing task id={existing.id} status={existing.status}"
                )
                return existing

            task = LLMTask(
                purchase_id=purchase_id,
                task_type="lots_extraction",
                input_text=json.dumps(payload, ensure_ascii=False),
                status="queued",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            print(f"[lots_extraction] enqueue created task id={task.id} status=queued")
            return task

    def run_lots_extraction_now(self, purchase_id: int, terms_text: str) -> LLMTask:
        payload = {"terms_text": terms_text or ""}
        with Session(engine) as session:
            task = LLMTask(
                purchase_id=purchase_id,
                task_type="lots_extraction",
                input_text=json.dumps(payload, ensure_ascii=False),
                status="in_progress",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        if task_id is None:
            raise RuntimeError("Failed to create lots extraction task")

        try:
            self._process_task(task_id)
        except Exception as exc:
            print(f"[lots_extraction] failed for purchase {purchase_id}: {exc}")
            with Session(engine) as session:
                errored = session.get(LLMTask, task_id)
                if errored:
                    errored.status = "failed"
                    errored.output_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    session.add(errored)
                    session.commit()
        with Session(engine) as session:
            refreshed = session.get(LLMTask, task_id)
            if not refreshed:
                raise RuntimeError("Lots extraction task disappeared")
            return refreshed

    def run_bid_lots_extraction_now(self, bid_id: int, terms_text: str, purchase_id: Optional[int] = None) -> LLMTask:
        payload = {"bid_id": bid_id, "terms_text": terms_text or "", "purchase_id": purchase_id}
        with Session(engine) as session:
            task = LLMTask(
                purchase_id=purchase_id,
                bid_id=bid_id,
                task_type="bid_lots_extraction",
                input_text=json.dumps(payload, ensure_ascii=False),
                status="in_progress",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        if task_id is None:
            raise RuntimeError("Failed to create bid lots extraction task")

        try:
            self._process_task(task_id)
        except Exception as exc:
            print(f"[bid_lots_extraction] failed for bid {bid_id}: {exc}")
            with Session(engine) as session:
                errored = session.get(LLMTask, task_id)
                if errored:
                    errored.status = "failed"
                    errored.output_text = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    session.add(errored)
                    session.commit()
        with Session(engine) as session:
            refreshed = session.get(LLMTask, task_id)
            if not refreshed:
                raise RuntimeError("Bid lots extraction task disappeared")
            return refreshed

    def _recover_stale_tasks(self) -> None:
        """Reset tasks stuck in 'in_progress' (e.g. after container restart)."""
        with Session(engine) as session:
            stale = session.exec(
                select(LLMTask).where(
                    LLMTask.status == "in_progress",
                    LLMTask.task_type.in_(EMBEDDED_TASK_TYPES),
                )
            ).all()
            for t in stale:
                print(f"[task_queue] recovering stale task id={t.id} type={t.task_type}")
                t.status = "queued"
                session.add(t)
            if stale:
                session.commit()

    def _claim_next_task(self, exclude_purchase_ids: set[int]) -> Optional[LLMTask]:
        """Claim one queued task with FOR UPDATE SKIP LOCKED + fair scheduling."""
        with Session(engine) as session:
            task = self._try_claim(session, exclude_purchase_ids)
            if task:
                return task
            return self._try_claim(session, set())

    @staticmethod
    def _try_claim(session: Session, exclude_purchase_ids: set[int]) -> Optional[LLMTask]:
        query = (
            select(LLMTask)
            .where(
                LLMTask.status == "queued",
                LLMTask.task_type.in_(EMBEDDED_TASK_TYPES),
            )
        )
        if exclude_purchase_ids:
            query = query.where(LLMTask.purchase_id.notin_(exclude_purchase_ids))
        query = query.order_by(LLMTask.created_at).limit(1).with_for_update(skip_locked=True)

        task = session.exec(query).first()
        if not task:
            return None

        task.status = "in_progress"
        task.updated_at = datetime.utcnow()
        session.add(task)
        session.commit()
        session.refresh(task)
        return task

    def _process_task_safe(self, task_id: int) -> None:
        """Thread-safe wrapper: catches all exceptions and marks task as failed."""
        try:
            self._process_task(task_id)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"[task_queue] task {task_id} crashed: {exc}\n{tb}")
            try:
                with Session(engine) as session:
                    errored = session.get(LLMTask, task_id)
                    if errored and errored.status != "completed":
                        errored.status = "failed"
                        errored.output_text = json.dumps(
                            {"error": str(exc), "traceback": tb[-1500:]},
                            ensure_ascii=False,
                        )
                        session.add(errored)
                        session.commit()
            except Exception:
                print(f"[task_queue] failed to mark task {task_id} as failed")

    def _run(self) -> None:
        print(f"[task_queue] worker thread started (max_workers={EMBEDDED_MAX_WORKERS})")
        self._recover_stale_tasks()
        executor = ThreadPoolExecutor(max_workers=EMBEDDED_MAX_WORKERS)
        futures: dict[Future, int] = {}  # Future -> purchase_id

        while not self._stop_event.is_set():
            # Clean up completed futures
            done = [f for f in futures if f.done()]
            for f in done:
                futures.pop(f)
                exc = f.exception()
                if exc:
                    print(f"[task_queue] thread raised: {exc}")

            if len(futures) >= EMBEDDED_MAX_WORKERS:
                time.sleep(self.poll_interval)
                continue

            active_purchases = {pid for pid in futures.values() if pid is not None}
            task = self._claim_next_task(active_purchases)

            if not task:
                time.sleep(self.poll_interval)
                continue

            task_id = task.id
            print(f"[task_queue] dispatching task id={task_id} type={task.task_type} purchase={task.purchase_id}")
            future = executor.submit(self._process_task_safe, task_id)
            futures[future] = task.purchase_id or 0

    def _process_task(self, task_id: int) -> None:
        with Session(engine) as session:
            task = session.get(LLMTask, task_id)
            if not task:
                return

            if task.task_type in ("supplier_search", "supplier_search_perplexity"):
                payload = self._load_payload(task.input_text)
                terms_text = payload.get("terms_text", "")
                hints = payload.get("hints") or []
                print(f"[supplier_search] start task={task.id} purchase={task.purchase_id}")
                plan = build_search_queries(
                    terms_text,
                    hints,
                    usage_ctx={"purchase_id": task.purchase_id, "task_id": task.id},
                )
                print(f"[supplier_search] completed task={task.id} queries={len(plan.queries)}")
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
            elif task.task_type == "lots_extraction":
                payload = self._load_payload(task.input_text)
                terms_text = payload.get("terms_text", "")
                terms_len = len(terms_text or "")
                print(
                    f"[lots_extraction] start task={task.id} purchase={task.purchase_id} "
                    f"terms_len={terms_len}"
                )
                print(f"[lots_extraction] terms_text_preview={terms_text[:500]!r}")
                if not terms_text:
                    print(f"[lots_extraction] empty terms task={task.id}")
                    task.output_text = json.dumps({"error": "Пустой текст ТЗ"}, ensure_ascii=False)
                    task.status = "failed"
                    session.add(task)
                    session.commit()
                    return

                try:
                    lots_payload = extract_lots(
                        terms_text,
                        usage_ctx={"purchase_id": task.purchase_id, "task_id": task.id},
                    )
                except Exception as exc:
                    import traceback
                    tb = traceback.format_exc()
                    print(f"[lots_extraction] extract_lots raised: {exc}\n{tb}")
                    task.output_text = json.dumps(
                        {"error": f"LLM call failed: {exc}", "traceback": tb[-1500:]},
                        ensure_ascii=False,
                    )
                    task.status = "failed"
                    session.add(task)
                    session.commit()
                    return

                print(f"[lots_extraction] extract_lots returned keys={list(lots_payload.keys())}")
                extracted = lots_payload.get("lots") or []
                print(f"[lots_extraction] extracted_lots_count={len(extracted)}")
                if not extracted:
                    task.output_text = json.dumps(
                        {
                            "error": "Модель не нашла лоты в ТЗ. Проверьте текст или попробуйте ещё раз.",
                            "raw_payload_preview": json.dumps(lots_payload, ensure_ascii=False)[:1000],
                        },
                        ensure_ascii=False,
                    )
                    task.status = "failed"
                    print(f"[lots_extraction] empty result task={task.id} purchase={task.purchase_id}")
                else:
                    task.output_text = json.dumps(lots_payload, ensure_ascii=False)
                    task.status = "completed"
                    if task.purchase_id:
                        self._sync_lots(session, task.purchase_id, lots_payload)
                    print(f"[lots_extraction] completed task={task.id} purchase={task.purchase_id} lots={len(extracted)}")
            elif task.task_type == "bid_lots_extraction":
                payload = self._load_payload(task.input_text)
                terms_text = payload.get("terms_text", "")
                bid_id = payload.get("bid_id")
                print(f"[bid_lots_extraction] start task={task.id} bid={bid_id}")
                print(f"[bid_lots_extraction] bid_text={terms_text}")
                if not terms_text or not bid_id:
                    task.output_text = json.dumps({"lots": []}, ensure_ascii=False)
                    task.status = "completed"
                    session.add(task)
                    session.commit()
                    return

                lots_payload = extract_bid_lots(
                    terms_text,
                    usage_ctx={"purchase_id": task.purchase_id, "task_id": task.id},
                )
                task.output_text = json.dumps(lots_payload, ensure_ascii=False)
                task.status = "completed"
                self._sync_bid_lots(session, int(bid_id), lots_payload)
                print(f"[bid_lots_extraction] completed task={task.id} bid={bid_id}")
            else:
                task.status = "completed"

            session.add(task)
            session.commit()

    @staticmethod
    def _sync_lots(session: Session, purchase_id: int, payload: Dict[str, Any]) -> None:
        lots_payload = payload.get("lots") or []
        if not lots_payload:
            # Safety net: never wipe existing lots when the new payload is empty.
            return
        existing_lots = session.exec(select(Lot).where(Lot.purchase_id == purchase_id)).all()
        for lot in existing_lots:
            parameters = session.exec(select(LotParameter).where(LotParameter.lot_id == lot.id)).all()
            for param in parameters:
                session.delete(param)
            session.delete(lot)
        session.commit()

        for lot_item in lots_payload:
            lot = Lot(purchase_id=purchase_id, name=lot_item.get("name", "Лот"))
            session.add(lot)
            session.commit()
            session.refresh(lot)
            for param in lot_item.get("parameters") or []:
                parameter = LotParameter(
                    lot_id=lot.id,
                    name=param.get("name", ""),
                    value=param.get("value", ""),
                    units=param.get("units", ""),
                )
                session.add(parameter)
            session.commit()

    @staticmethod
    def _sync_bid_lots(session: Session, bid_id: int, payload: Dict[str, Any]) -> None:
        lots_payload = payload.get("lots") or []
        existing_lots = session.exec(select(BidLot).where(BidLot.bid_id == bid_id)).all()
        for lot in existing_lots:
            parameters = session.exec(select(BidLotParameter).where(BidLotParameter.bid_lot_id == lot.id)).all()
            for param in parameters:
                session.delete(param)
            session.delete(lot)
        session.commit()

        for lot_item in lots_payload:
            # The unified KP parser always returns "" for missing values; turn
            # them into None so the DB column reads as NULL and downstream
            # M4 checks (`if rn:`) work correctly.
            registry_number = (lot_item.get("registry_number") or "").strip() or None
            okpd2_code = (lot_item.get("okpd2_code") or "").strip() or None
            lot = BidLot(
                bid_id=bid_id,
                name=lot_item.get("name", "Лот"),
                price=lot_item.get("price", ""),
                registry_number=registry_number,
                okpd2_code=okpd2_code,
            )
            session.add(lot)
            session.commit()
            session.refresh(lot)
            for param in lot_item.get("parameters") or []:
                parameter = BidLotParameter(
                    bid_lot_id=lot.id,
                    name=param.get("name", ""),
                    value=param.get("value", ""),
                    units=param.get("units", ""),
                )
                session.add(parameter)
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
                LLMTask.task_type.in_(["supplier_search", "supplier_search_perplexity"]),
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
            estimated_complete_time = datetime.utcnow() + timedelta(
                minutes=10 + queue_length * 10, hours=3
            )

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
            queued_perplexity = managed_session.exec(
                select(LLMTask).where(
                    LLMTask.task_type == "supplier_search_perplexity", LLMTask.status == "queued"
                )
            ).all()
            return len(queued_tasks) + len(queued_perplexity)

    queued_tasks = session.exec(
        select(LLMTask).where(LLMTask.task_type == "supplier_search", LLMTask.status == "queued")
    ).all()
    queued_perplexity = session.exec(
        select(LLMTask).where(LLMTask.task_type == "supplier_search_perplexity", LLMTask.status == "queued")
    ).all()
    return len(queued_tasks) + len(queued_perplexity)


task_queue = TaskQueue()
