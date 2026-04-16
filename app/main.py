import logging
import os
import json
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import select, func, col
from sqlalchemy import delete as sa_delete

logger = logging.getLogger(__name__)

from io import BytesIO

import pandas as pd
from fastapi.responses import StreamingResponse

from . import auth
from .database import create_db_and_tables, get_session
from .routers import auth as auth_router
from .routers import admin as admin_router
from .routers import leads as leads_router
from .routers import regime as regime_router
from .services.llm_tasks import build_search_queries
from .llm_stub import generate_email_body
from .models import (
    Bid,
    BidLot,
    BidLotParameter,
    EmailAccount,
    EmailMessage,
    LLMTask,
    Lot,
    LotParameter,
    Purchase,
    PurchaseFile,
    RegimeCheck,
    Supplier,
    SupplierContact,
    User,
)
from .schemas import (
    BidCreate,
    ComparisonCharacteristicRowRead,
    BidLotParameterRead,
    BidLotRead,
    BidRead,
    EmailAccountCreate,
    EmailAccountRead,
    EmailDraftResponse,
    EmailMessageCreate,
    EmailMessageRead,
    LLMTaskCreate,
    LLMTaskRead,
    LotComparisonResponse,
    LotComparisonRowRead,
    LotCreate,
    LotsResponse,
    LotRead,
    LotParameterRead,
    PurchaseCreate,
    PurchaseDashboardRead,
    PurchaseFileCreate,
    PurchaseFileRead,
    PurchaseRead,
    PurchaseUpdate,
    SupplierContactCreate,
    SupplierContactRead,
    SupplierCreate,
    SupplierRead,
    SupplierImportRequest,
    SupplierImportResult,
    SupplierSearchRequest,
    SupplierSearchResponse,
)
from .supplier_import import load_contacts_from_files, merge_contacts
from .task_queue import (
    get_supplier_search_queue_length,
    get_supplier_search_state,
    task_queue,
)

app = FastAPI(title="zakupAI service", version="0.1.0")

raw_origins = os.getenv("CORS_ORIGINS", "*")
origins = [item.strip() for item in raw_origins.split(",") if item.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(leads_router.router)
app.include_router(regime_router.router)


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()
    if os.getenv("ENABLE_EMBEDDED_QUEUE", "false").lower() == "true":
        task_queue.start()


@app.get("/health")
def healthcheck() -> dict:
    return {"status": "ok"}


@app.post("/purchases", response_model=PurchaseRead, status_code=status.HTTP_201_CREATED)
def create_purchase(payload: PurchaseCreate, session=Depends(get_session), current_user: User = Depends(auth.get_current_user)) -> Purchase:
    last_number = session.exec(
        select(Purchase.auto_number)
        .where(Purchase.user_id == current_user.id)
        .order_by(Purchase.auto_number.desc())
    ).first()
    auto_number = 1 if not last_number else last_number + 1
    full_name = f"Закупка №{auto_number}" + (f" — {payload.custom_name}" if payload.custom_name else "")
    purchase = Purchase(
        user_id=current_user.id,
        auto_number=auto_number,
        custom_name=payload.custom_name,
        full_name=full_name,
        terms_text=payload.terms_text,
    )
    session.add(purchase)
    session.commit()
    session.refresh(purchase)
    if purchase.terms_text:
        try:
            task_queue.enqueue_lots_extraction_task(purchase.id, purchase.terms_text)
        except Exception as exc:
            print(f"[lots_extraction] enqueue failed: {exc}")
    return purchase


@app.get("/purchases", response_model=List[PurchaseRead])
def list_purchases(
    include_archived: bool = False,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> List[Purchase]:
    stmt = select(Purchase).where(Purchase.user_id == current_user.id)
    if not include_archived:
        stmt = stmt.where(Purchase.is_archived == False)  # noqa: E712
    return session.exec(stmt.order_by(col(Purchase.created_at).desc())).all()


@app.get("/purchases/dashboard", response_model=List[PurchaseDashboardRead])
def get_purchases_dashboard(
    archived: bool | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> List[PurchaseDashboardRead]:
    stmt = select(Purchase).where(Purchase.user_id == current_user.id)
    if archived is not None:
        stmt = stmt.where(Purchase.is_archived == archived)  # noqa: E712

    order_col = getattr(Purchase, sort_by, Purchase.created_at)
    stmt = stmt.order_by(order_col.desc() if sort_order == "desc" else order_col.asc())
    purchases = session.exec(stmt).all()

    result = []
    for p in purchases:
        lots_count = session.exec(select(func.count(Lot.id)).where(Lot.purchase_id == p.id)).one()
        suppliers_count = session.exec(select(func.count(Supplier.id)).where(Supplier.purchase_id == p.id)).one()
        bids_count = session.exec(select(func.count(Bid.id)).where(Bid.purchase_id == p.id)).one()

        regime_check = session.exec(
            select(RegimeCheck).where(RegimeCheck.purchase_id == p.id).order_by(col(RegimeCheck.created_at).desc())
        ).first()
        regime_status = regime_check.status if regime_check else None

        files = session.exec(select(PurchaseFile).where(PurchaseFile.purchase_id == p.id)).all()

        # Derive module statuses
        search_task = session.exec(
            select(LLMTask).where(
                LLMTask.purchase_id == p.id,
                LLMTask.task_type.in_(["supplier_search", "supplier_search_perplexity"]),
            ).order_by(col(LLMTask.created_at).desc())
        ).first()
        if suppliers_count > 0:
            search_st = "done"
        elif search_task and search_task.status in ("queued", "in_progress"):
            search_st = "in_progress"
        elif search_task and search_task.status == "completed":
            search_st = "done"
        else:
            search_st = "not_started"

        emails_count = session.exec(select(func.count(EmailMessage.id)).where(EmailMessage.purchase_id == p.id)).one()
        if bids_count > 0:
            corr_st = "done"
        elif emails_count > 0:
            corr_st = "in_progress"
        else:
            corr_st = "not_started"

        comparison_task = session.exec(
            select(LLMTask).where(LLMTask.purchase_id == p.id, LLMTask.task_type == "lot_comparison").order_by(col(LLMTask.created_at).desc())
        ).first()
        if comparison_task and comparison_task.status == "completed":
            comp_st = "done"
        elif comparison_task and comparison_task.status in ("queued", "in_progress"):
            comp_st = "in_progress"
        else:
            comp_st = "not_started"

        regime_st = "not_started"
        if regime_check:
            regime_st = "done" if regime_check.status in ("completed", "done") else "in_progress"

        result.append(PurchaseDashboardRead(
            id=p.id,
            auto_number=p.auto_number,
            full_name=p.full_name,
            custom_name=p.custom_name,
            status=p.status,
            is_archived=p.is_archived if p.is_archived else False,
            created_at=p.created_at,
            updated_at=p.updated_at,
            lots_count=lots_count,
            suppliers_count=suppliers_count,
            bids_count=bids_count,
            regime_status=regime_status,
            files=[PurchaseFileRead(id=f.id, filename=f.filename, file_type=f.file_type, created_at=f.created_at) for f in files],
            search_status=search_st,
            correspondence_status=corr_st,
            comparison_status=comp_st,
            regime_check_status=regime_st,
        ))
    return result


@app.get("/purchases/{purchase_id}", response_model=PurchaseRead)
def get_purchase(purchase_id: int, session=Depends(get_session), current_user: User = Depends(auth.get_current_user)) -> Purchase:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
    return purchase


@app.patch("/purchases/{purchase_id}", response_model=PurchaseRead)
def update_purchase(
    purchase_id: int,
    payload: PurchaseUpdate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> Purchase:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    original_terms = purchase.terms_text
    if payload.custom_name is not None:
        purchase.custom_name = payload.custom_name
        purchase.full_name = f"Закупка №{purchase.auto_number}" + (f" — {payload.custom_name}" if payload.custom_name else "")
    if payload.terms_text is not None:
        purchase.terms_text = payload.terms_text
    if payload.status is not None:
        purchase.status = payload.status
    if payload.nmck_value is not None:
        purchase.nmck_value = payload.nmck_value
    if payload.nmck_currency is not None:
        purchase.nmck_currency = payload.nmck_currency
    if payload.is_archived is not None:
        purchase.is_archived = payload.is_archived

    purchase.updated_at = datetime.utcnow()
    session.add(purchase)
    session.commit()
    session.refresh(purchase)

    if payload.terms_text is not None and payload.terms_text != original_terms:
        if purchase.terms_text:
            try:
                task_queue.enqueue_lots_extraction_task(purchase.id, purchase.terms_text)
            except Exception as exc:
                print(f"[lots_extraction] enqueue failed: {exc}")
    return purchase


@app.post("/purchases/{purchase_id}/files", response_model=PurchaseFileRead, status_code=status.HTTP_201_CREATED)
def track_purchase_file(
    purchase_id: int,
    payload: PurchaseFileCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> PurchaseFile:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
    pf = PurchaseFile(purchase_id=purchase_id, filename=payload.filename, file_type=payload.file_type)
    session.add(pf)
    session.commit()
    session.refresh(pf)
    return pf


@app.get("/purchases/{purchase_id}/files", response_model=List[PurchaseFileRead])
def list_purchase_files(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> List[PurchaseFileRead]:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
    files = session.exec(
        select(PurchaseFile)
        .where(PurchaseFile.purchase_id == purchase_id)
        .order_by(col(PurchaseFile.created_at).desc())
    ).all()
    return [
        PurchaseFileRead(id=f.id, filename=f.filename, file_type=f.file_type, created_at=f.created_at)
        for f in files
    ]


def _load_lots(session, purchase_id: int) -> list[LotRead]:
    lots = session.exec(select(Lot).where(Lot.purchase_id == purchase_id)).all()
    lot_reads: list[LotRead] = []
    for lot in lots:
        params = session.exec(select(LotParameter).where(LotParameter.lot_id == lot.id)).all()
        lot_reads.append(
            LotRead(
                id=lot.id or 0,
                name=lot.name,
                parameters=[
                    LotParameterRead(name=param.name, value=param.value, units=param.units)
                    for param in params
                ],
            )
        )
    return lot_reads


def _load_bid_lots(session, bid_id: int) -> list[BidLotRead]:
    lots = session.exec(select(BidLot).where(BidLot.bid_id == bid_id)).all()
    lot_reads: list[BidLotRead] = []
    for lot in lots:
        params = session.exec(select(BidLotParameter).where(BidLotParameter.bid_lot_id == lot.id)).all()
        lot_reads.append(
            BidLotRead(
                id=lot.id or 0,
                name=lot.name,
                price=lot.price,
                parameters=[
                    BidLotParameterRead(name=param.name, value=param.value, units=param.units)
                    for param in params
                ],
            )
        )
    return lot_reads


def _safe_json_dict(raw_text: str | None) -> dict:
    if not raw_text:
        return {}
    try:
        payload = json.loads(raw_text)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _serialize_lot_comparison(task: LLMTask, bid_id: int) -> LotComparisonResponse:
    payload = _safe_json_dict(task.output_text)
    rows_payload = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    rows: list[LotComparisonRowRead] = []
    for item in rows_payload:
        if not isinstance(item, dict):
            continue
        lot_params = item.get("lot_parameters") if isinstance(item.get("lot_parameters"), list) else []
        bid_lot_params = item.get("bid_lot_parameters") if isinstance(item.get("bid_lot_parameters"), list) else []
        rows.append(
            LotComparisonRowRead(
                lot_id=int(item.get("lot_id", 0)),
                lot_name=str(item.get("lot_name") or ""),
                lot_parameters=[
                    LotParameterRead(
                        name=str(param.get("name") or ""),
                        value=str(param.get("value") or ""),
                        units=str(param.get("units") or ""),
                    )
                    for param in lot_params
                    if isinstance(param, dict)
                ],
                bid_lot_id=int(item["bid_lot_id"]) if item.get("bid_lot_id") is not None else None,
                bid_lot_name=str(item.get("bid_lot_name")) if item.get("bid_lot_name") is not None else None,
                bid_lot_price=str(item.get("bid_lot_price")) if item.get("bid_lot_price") is not None else None,
                bid_lot_parameters=[
                    BidLotParameterRead(
                        name=str(param.get("name") or ""),
                        value=str(param.get("value") or ""),
                        units=str(param.get("units") or ""),
                    )
                    for param in bid_lot_params
                    if isinstance(param, dict)
                ],
                confidence=float(item["confidence"]) if item.get("confidence") is not None else None,
                reason=str(item.get("reason")) if item.get("reason") is not None else None,
                characteristic_rows=[
                    ComparisonCharacteristicRowRead(
                        left_text=str(row.get("left_text") or ""),
                        right_text=str(row.get("right_text") or ""),
                        status=(
                            row.get("status")
                            if row.get("status") in ("unmatched_tz", "matched", "unmatched_kp", "mismatch", "partial")
                            else "matched"
                        ),
                    )
                    for row in (item.get("characteristic_rows") or [])
                    if isinstance(row, dict)
                ],
            )
        )

    # Map DB status to frontend-compatible status
    fe_status = "done" if task.status == "completed" else task.status

    return LotComparisonResponse(
        task_id=task.id or 0,
        status=fe_status,
        bid_id=bid_id,
        created_at=task.created_at,
        note=str(payload.get("note")) if payload.get("note") is not None else None,
        stages=payload.get("stages"),
        rows=rows,
    )


@app.get("/purchases/{purchase_id}/lots", response_model=LotsResponse)
def get_purchase_lots(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> LotsResponse:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    lots = _load_lots(session, purchase_id)
    task = session.exec(
        select(LLMTask)
        .where(
            LLMTask.purchase_id == purchase_id,
            LLMTask.task_type == "lots_extraction",
        )
        .order_by(LLMTask.created_at.desc())
    ).first()

    # Auto-enqueue if there's terms_text but no task at all, or the previous one failed and we still have no lots.
    needs_enqueue = (
        purchase.terms_text
        and not lots
        and (task is None or task.status == "failed")
    )
    if needs_enqueue:
        try:
            task = task_queue.enqueue_lots_extraction_task(purchase_id, purchase.terms_text)
        except Exception as exc:
            print(f"[lots_extraction] enqueue failed: {exc}")

    if task:
        status_value = task.status
    elif lots:
        status_value = "completed"
    elif not purchase.terms_text:
        status_value = "idle"
    else:
        status_value = "queued"

    error_text: Optional[str] = None
    if task and task.status == "failed" and task.output_text:
        try:
            payload = json.loads(task.output_text)
            if isinstance(payload, dict):
                error_text = payload.get("error") or None
        except Exception:
            error_text = task.output_text[:500]

    return LotsResponse(status=status_value, lots=lots, error_text=error_text)


@app.get("/purchases/{purchase_id}/lots/diagnostics")
def get_lots_diagnostics(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> dict:
    """Return full state of lots extraction for debugging from the UI.

    Admin-only — diagnostics expose internal state (worker liveness, raw
    LLMTask payloads, OpenAI config). Defence in depth: the UI hides the
    button for non-admins but the endpoint must reject them too.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    purchase = session.get(Purchase, purchase_id)
    # Admins can inspect any purchase, not only their own.
    if not purchase:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    lots_tasks = session.exec(
        select(LLMTask)
        .where(
            LLMTask.purchase_id == purchase_id,
            LLMTask.task_type == "lots_extraction",
        )
        .order_by(col(LLMTask.created_at).desc())
    ).all()

    supplier_tasks = session.exec(
        select(LLMTask)
        .where(
            LLMTask.purchase_id == purchase_id,
            LLMTask.task_type.in_(["supplier_search", "supplier_search_perplexity"]),
        )
        .order_by(col(LLMTask.created_at).desc())
    ).all()

    other_tasks = session.exec(
        select(LLMTask)
        .where(
            LLMTask.purchase_id == purchase_id,
            ~LLMTask.task_type.in_(
                ["lots_extraction", "supplier_search", "supplier_search_perplexity"]
            ),
        )
        .order_by(col(LLMTask.created_at).desc())
    ).all()

    lots_count = session.exec(
        select(func.count(Lot.id)).where(Lot.purchase_id == purchase_id)
    ).one()

    suppliers_count = session.exec(
        select(func.count(Supplier.id)).where(Supplier.purchase_id == purchase_id)
    ).one()

    embedded_queue_enabled = os.getenv("ENABLE_EMBEDDED_QUEUE", "false").lower() == "true"
    worker_alive = task_queue._thread.is_alive() if hasattr(task_queue, "_thread") else False

    def _serialize(t: LLMTask) -> dict:
        now = datetime.utcnow()
        age_seconds = None
        if t.created_at:
            age_seconds = int((now - t.created_at).total_seconds())
        updated_at = getattr(t, "updated_at", None)
        seconds_since_update = None
        if updated_at:
            seconds_since_update = int((now - updated_at).total_seconds())

        # Parse the FULL output_text JSON before truncating, so we can
        # surface the live `note` field even when the raw payload is huge
        # (search outputs easily exceed 30k chars and get cut mid-string
        # in output_preview, breaking any downstream json.loads).
        output_text_full = t.output_text or ""
        note_value: Optional[str] = None
        error_value: Optional[str] = None
        if output_text_full:
            try:
                parsed_full = json.loads(output_text_full)
                if isinstance(parsed_full, dict):
                    note_value = parsed_full.get("note")
                    error_value = parsed_full.get("error")
            except Exception:
                pass

        return {
            "id": t.id,
            "status": t.status,
            "task_type": t.task_type,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "age_seconds": age_seconds,
            "seconds_since_update": seconds_since_update,
            "note": note_value,
            "error": error_value,
            "input_preview": (t.input_text or "")[:500],
            "output_preview": output_text_full[:2000],
            "input_length": len(t.input_text or ""),
            "output_length": len(output_text_full),
        }

    serialized_lots = [_serialize(t) for t in lots_tasks[:10]]
    serialized_suppliers = [_serialize(t) for t in supplier_tasks[:10]]
    serialized_others = [_serialize(t) for t in other_tasks[:5]]

    # ── Build human-readable summary at the top ─────────────────────────
    def _fmt_age(seconds: Optional[int]) -> str:
        if seconds is None:
            return "(unknown)"
        m, s = divmod(int(seconds), 60)
        if m > 0:
            return f"{m}м {s}с"
        return f"{s}с"

    def _parse_note(task_dict: dict) -> Optional[str]:
        """Read the 'note' field that the serializer pre-parsed from full output_text."""
        return task_dict.get("note")

    def _parse_crawl_progress(note: Optional[str]) -> Optional[dict]:
        """Pull '12/47' style site count out of a note string written by the ETL worker.

        Returns {processed, total, percent} or None if no crawl progress segment found.
        """
        if not note:
            return None
        import re
        m = re.search(r"Краулинг сайтов:\s*(\d+)\s*/\s*(\d+)", note)
        if not m:
            # Final state: "Обход сайтов выполнен: N шт."
            m_done = re.search(r"Обход сайтов выполнен:\s*(\d+)", note)
            if m_done:
                n = int(m_done.group(1))
                return {"processed": n, "total": n, "percent": 100}
            # Pre-crawl state: "Найдено сайтов для обхода: N"
            m_found = re.search(r"Найдено сайтов для обхода:\s*(\d+)", note)
            if m_found:
                return {"processed": 0, "total": int(m_found.group(1)), "percent": 0}
            return None
        processed = int(m.group(1))
        total = int(m.group(2))
        percent = int(processed * 100 / total) if total > 0 else 0
        return {"processed": processed, "total": total, "percent": percent}

    # Lots verdict
    latest_lots = serialized_lots[0] if serialized_lots else None
    failed_lots_count = sum(1 for t in serialized_lots if t["status"] == "failed")
    if lots_count > 0:
        lots_verdict = "ok"
        lots_status_text = f"✅ {lots_count} лотов распознано"
        lots_action = None
    elif latest_lots and latest_lots["status"] in ("queued", "in_progress"):
        lots_verdict = "running"
        lots_status_text = f"🔄 Распознаётся (задача #{latest_lots['id']}, возраст {_fmt_age(latest_lots['age_seconds'])})"
        lots_action = "Подождите завершения, либо нажмите «Сбросить распознавание»"
    elif latest_lots and latest_lots["status"] == "failed":
        lots_verdict = "failed"
        lots_status_text = f"❌ Последняя попытка #{latest_lots['id']} упала"
        lots_action = "Нажмите «Распознать ещё раз» в основном UI"
    elif not purchase.terms_text:
        lots_verdict = "idle"
        lots_status_text = "⚪ Пусто (ТЗ не загружено)"
        lots_action = "Загрузите ТЗ в карточке закупки"
    else:
        lots_verdict = "idle"
        lots_status_text = "⚪ Распознавание ещё не запускалось"
        lots_action = None

    # Supplier search verdict
    latest_supplier = serialized_suppliers[0] if serialized_suppliers else None
    crawl_progress = None
    if latest_supplier and latest_supplier["status"] in ("queued", "in_progress"):
        sec_since = latest_supplier.get("seconds_since_update")
        note = _parse_note(latest_supplier) or "(нет данных о стадии)"
        crawl_progress = _parse_crawl_progress(note)
        is_stuck = sec_since is not None and sec_since > 180
        if is_stuck:
            supplier_verdict = "stuck"
            crawl_part = ""
            if crawl_progress and crawl_progress["total"] > 0:
                crawl_part = f", застряла на сайте {crawl_progress['processed']}/{crawl_progress['total']}"
            supplier_status_text = (
                f"⚠ ВОЗМОЖНО ЗАВИСЛА (задача #{latest_supplier['id']}, "
                f"нет обновлений {_fmt_age(sec_since)}{crawl_part})"
            )
            supplier_action = "Нажмите «Сбросить поиск поставщиков» и запустите заново"
        else:
            supplier_verdict = "running"
            if crawl_progress and crawl_progress["total"] > 0:
                supplier_status_text = (
                    f"🔄 Краулинг сайтов: {crawl_progress['processed']}/{crawl_progress['total']} "
                    f"({crawl_progress['percent']}%) — задача #{latest_supplier['id']}, "
                    f"идёт {_fmt_age(latest_supplier['age_seconds'])}"
                )
                supplier_action = (
                    f"Текущая стадия: {note}. "
                    f"Каждый сайт обходится 5-30 секунд (Selenium + LLM-валидация)."
                )
            else:
                update_str = (
                    f", последнее обновление {_fmt_age(sec_since)} назад"
                    if sec_since is not None
                    else ""
                )
                supplier_status_text = (
                    f"🔄 Идёт поиск (задача #{latest_supplier['id']}, "
                    f"возраст {_fmt_age(latest_supplier['age_seconds'])}{update_str})"
                )
                supplier_action = (
                    f"Текущая стадия: {note}. "
                    f"Краулинг сайтов через Selenium может занимать 5-15 минут — это нормально."
                )
    elif suppliers_count > 0:
        supplier_verdict = "ok"
        supplier_status_text = f"✅ Найдено поставщиков: {suppliers_count}"
        supplier_action = None
    elif latest_supplier and latest_supplier["status"] == "completed":
        supplier_verdict = "warning"
        supplier_status_text = "⚠ Задача завершена, но поставщиков не найдено"
        supplier_action = "Запустите поиск ещё раз — возможно временная проблема с провайдерами"
    elif latest_supplier and latest_supplier["status"] == "failed":
        supplier_verdict = "failed"
        supplier_status_text = f"❌ Последняя попытка #{latest_supplier['id']} упала"
        supplier_action = "Запустите поиск заново через основной UI"
    else:
        supplier_verdict = "idle"
        supplier_status_text = "⚪ Поиск ещё не запускался"
        supplier_action = "Нажмите «Запустить поиск» в карточке закупки"

    # Infrastructure verdict
    infra_problems = []
    if not embedded_queue_enabled:
        infra_problems.append("ENABLE_EMBEDDED_QUEUE=false (lots_extraction worker не запустится)")
    if not worker_alive:
        infra_problems.append("Backend worker thread мёртв")
    if not bool(os.getenv("OPENAI_API_KEY")):
        infra_problems.append("OPENAI_API_KEY не задан")

    if infra_problems:
        infra_verdict = "broken"
        infra_status_text = "❌ ПРОБЛЕМЫ: " + "; ".join(infra_problems)
    else:
        infra_verdict = "ok"
        infra_status_text = "✅ Всё в порядке"

    summary = {
        "lots": {
            "verdict": lots_verdict,
            "status": lots_status_text,
            "lots_in_db": lots_count,
            "completed_count": sum(1 for t in serialized_lots if t["status"] == "completed"),
            "failed_count": failed_lots_count,
            "action_hint": lots_action,
        },
        "supplier_search": {
            "verdict": supplier_verdict,
            "status": supplier_status_text,
            "suppliers_in_db": suppliers_count,
            "active_task_id": latest_supplier["id"] if latest_supplier and latest_supplier["status"] in ("queued", "in_progress") else None,
            "current_stage": _parse_note(latest_supplier) if latest_supplier else None,
            "crawl_progress": crawl_progress,
            "action_hint": supplier_action,
        },
        "infrastructure": {
            "verdict": infra_verdict,
            "status": infra_status_text,
            "embedded_queue_enabled": embedded_queue_enabled,
            "backend_worker_alive": worker_alive,
            "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        },
    }

    return {
        "summary": summary,
        "purchase_id": purchase_id,
        "purchase_status": purchase.status,
        "has_terms_text": bool(purchase.terms_text),
        "terms_text_length": len(purchase.terms_text or ""),
        "terms_text_preview": (purchase.terms_text or "")[:300],
        "lots_in_db": lots_count,
        "suppliers_in_db": suppliers_count,
        "embedded_queue_enabled": embedded_queue_enabled,
        "worker_thread_alive": worker_alive,
        "openai_api_key_set": bool(os.getenv("OPENAI_API_KEY")),
        "openai_base_url": os.getenv("OPENAI_BASE_URL") or None,
        "openai_model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "lots_tasks": serialized_lots,
        "supplier_tasks": serialized_suppliers,
        "other_tasks": serialized_others,
        # Backward-compat alias for the old field name
        "tasks": serialized_lots,
    }


@app.post("/purchases/{purchase_id}/tasks/reset")
def reset_purchase_tasks(
    purchase_id: int,
    task_type: str,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> dict:
    """Force-fail any queued/in_progress tasks of the given type for this purchase.

    Use case: a supplier_search task is stuck because the ETL container died
    mid-run and the row never got reaped, OR the task is genuinely hung and
    the user wants to start over without waiting for the 5-minute reaper.
    """
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    allowed_types = {
        "lots_extraction",
        "supplier_search",
        "supplier_search_perplexity",
        "lot_comparison",
    }
    if task_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"task_type must be one of {sorted(allowed_types)}")

    rows = session.exec(
        select(LLMTask).where(
            LLMTask.purchase_id == purchase_id,
            LLMTask.task_type == task_type,
            LLMTask.status.in_(["queued", "in_progress"]),
        )
    ).all()
    reset_count = 0
    for t in rows:
        t.status = "failed"
        t.output_text = json.dumps(
            {"error": "Принудительный сброс пользователем"}, ensure_ascii=False
        )
        t.updated_at = datetime.utcnow()
        session.add(t)
        reset_count += 1
    if reset_count:
        session.commit()
    return {"reset": reset_count, "task_type": task_type, "purchase_id": purchase_id}


@app.post("/purchases/{purchase_id}/lots", response_model=LotRead, status_code=status.HTTP_201_CREATED)
def create_purchase_lot(
    purchase_id: int,
    payload: LotCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> LotRead:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    lot = Lot(purchase_id=purchase_id, name=payload.name)
    session.add(lot)
    session.commit()
    session.refresh(lot)

    for param in payload.parameters:
        session.add(
            LotParameter(
                lot_id=lot.id,
                name=param.name,
                value=param.value,
                units=param.units or "",
            )
        )
    session.commit()

    params = session.exec(select(LotParameter).where(LotParameter.lot_id == lot.id)).all()
    return LotRead(
        id=lot.id or 0,
        name=lot.name,
        parameters=[
            LotParameterRead(name=param.name, value=param.value, units=param.units) for param in params
        ],
    )


@app.post("/purchases/{purchase_id}/bids", response_model=BidRead, status_code=status.HTTP_201_CREATED)
def create_bid(
    purchase_id: int,
    payload: BidCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> BidRead:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    bid_text = payload.bid_text.strip()
    if not bid_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bid text is required")

    supplier_name = payload.supplier_name
    supplier_contact = payload.supplier_contact
    supplier_id = payload.supplier_id
    supplier = session.get(Supplier, supplier_id) if supplier_id else None

    if supplier_id and (not supplier or supplier.purchase_id != purchase_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    if supplier and not supplier_name:
        supplier_name = supplier.company_name or supplier.website_url

    if supplier and not supplier_contact:
        contact = session.exec(
            select(SupplierContact).where(SupplierContact.supplier_id == supplier.id).order_by(SupplierContact.id)
        ).first()
        if contact:
            supplier_contact = contact.email

    bid = Bid(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        supplier_contact=supplier_contact,
        bid_text=bid_text,
    )
    session.add(bid)
    session.commit()
    session.refresh(bid)

    if bid.id is not None:
        try:
            task_queue.run_bid_lots_extraction_now(bid.id, bid_text, purchase_id=purchase_id)
        except Exception as exc:
            print(f"[bid_lots_extraction] immediate run failed: {exc}")

    lots = _load_bid_lots(session, bid.id or 0)
    return BidRead(
        id=bid.id or 0,
        purchase_id=bid.purchase_id,
        supplier_id=bid.supplier_id,
        supplier_name=bid.supplier_name,
        supplier_contact=bid.supplier_contact,
        bid_text=bid.bid_text,
        created_at=bid.created_at,
        lots=lots,
    )


@app.get("/purchases/{purchase_id}/bids", response_model=List[BidRead])
def list_bids(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> List[BidRead]:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    bids = session.exec(select(Bid).where(Bid.purchase_id == purchase_id).order_by(Bid.created_at.desc())).all()
    return [
        BidRead(
            id=bid.id or 0,
            purchase_id=bid.purchase_id,
            supplier_id=bid.supplier_id,
            supplier_name=bid.supplier_name,
            supplier_contact=bid.supplier_contact,
            bid_text=bid.bid_text,
            created_at=bid.created_at,
            lots=_load_bid_lots(session, bid.id or 0),
        )
        for bid in bids
    ]


@app.delete("/purchases/{purchase_id}/bids/{bid_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bid(
    purchase_id: int,
    bid_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
):
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    bid = session.get(Bid, bid_id)
    if not bid or bid.purchase_id != purchase_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bid not found")

    lots = session.exec(select(BidLot).where(BidLot.bid_id == bid_id)).all()
    for lot in lots:
        params = session.exec(select(BidLotParameter).where(BidLotParameter.bid_lot_id == lot.id)).all()
        for p in params:
            session.delete(p)
        session.delete(lot)
    # LLMTask.bid_id is a nullable FK — detach instead of deleting so trace
    # history survives the bid removal.
    tasks = session.exec(select(LLMTask).where(LLMTask.bid_id == bid_id)).all()
    for t in tasks:
        t.bid_id = None
        session.add(t)
    session.delete(bid)
    session.commit()


@app.delete("/purchases/{purchase_id}/tz", status_code=status.HTTP_204_NO_CONTENT)
def delete_tz(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
):
    """Remove the ТЗ from a purchase.

    Also drops every Lot + LotParameter we extracted from it and the `tz`
    PurchaseFile chip — so the user can re-upload a corrected ТЗ and run lot
    extraction from scratch instead of having to create a new purchase.
    """
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    try:
        lot_rows = session.exec(select(Lot).where(Lot.purchase_id == purchase_id)).all()
        lot_ids = [l.id for l in lot_rows if l.id is not None]
        if lot_ids:
            # Two bulk deletes — skip the per-row ORM flush ordering that can
            # surprise us when a parent is deleted while a child mapping is
            # still live in the session.
            session.exec(sa_delete(LotParameter).where(col(LotParameter.lot_id).in_(lot_ids)))
            session.exec(sa_delete(Lot).where(col(Lot.id).in_(lot_ids)))

        session.exec(
            sa_delete(PurchaseFile).where(
                PurchaseFile.purchase_id == purchase_id,
                PurchaseFile.file_type == "tz",
            )
        )

        purchase.terms_text = None
        purchase.updated_at = datetime.utcnow()
        session.add(purchase)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("delete_tz failed for purchase %s", purchase_id)
        raise HTTPException(status_code=500, detail=f"delete_tz: {type(exc).__name__}: {exc}")


@app.post("/purchases/{purchase_id}/bids/{bid_id}/comparison", response_model=LotComparisonResponse)
def start_bid_lot_comparison(
    purchase_id: int,
    bid_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> LotComparisonResponse:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    bid = session.get(Bid, bid_id)
    if not bid or bid.purchase_id != purchase_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bid not found")

    existing = session.exec(
        select(LLMTask)
        .where(
            LLMTask.purchase_id == purchase_id,
            LLMTask.bid_id == bid_id,
            LLMTask.task_type == "lot_comparison",
            LLMTask.status.in_(["queued", "in_progress"]),
        )
        .order_by(LLMTask.created_at.desc())
    ).first()
    if existing:
        return _serialize_lot_comparison(existing, bid_id)

    task = LLMTask(
        purchase_id=purchase_id,
        bid_id=bid_id,
        task_type="lot_comparison",
        input_text=json.dumps(
            {
                "purchase_id": purchase_id,
                "bid_id": bid_id,
            },
            ensure_ascii=False,
        ),
        status="queued",
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return _serialize_lot_comparison(task, bid_id)


@app.get("/purchases/{purchase_id}/bids/{bid_id}/comparison", response_model=LotComparisonResponse | None)
def get_bid_lot_comparison(
    purchase_id: int,
    bid_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> LotComparisonResponse | None:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    bid = session.get(Bid, bid_id)
    if not bid or bid.purchase_id != purchase_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bid not found")

    task = session.exec(
        select(LLMTask)
        .where(
            LLMTask.purchase_id == purchase_id,
            LLMTask.bid_id == bid_id,
            LLMTask.task_type == "lot_comparison",
        )
        .order_by(LLMTask.created_at.desc())
    ).first()
    if not task:
        return None
    return _serialize_lot_comparison(task, bid_id)


@app.get("/purchases/{purchase_id}/comparison/diagnostics")
def get_comparison_diagnostics(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
):
    """Return diagnostic info for M3 comparison tasks (admin only)."""
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    # All bids
    bids = session.exec(select(Bid).where(Bid.purchase_id == purchase_id)).all()
    bid_info = []
    for bid in bids:
        lot_count = len(session.exec(select(BidLot).where(BidLot.bid_id == bid.id)).all())
        bid_info.append({
            "bid_id": bid.id,
            "supplier_name": bid.supplier_name,
            "lot_count": lot_count,
            "created_at": str(bid.created_at),
        })

    # TZ lots
    lots = session.exec(select(Lot).where(Lot.purchase_id == purchase_id)).all()
    tz_info = {"lot_count": len(lots), "lots": [{"id": l.id, "name": l.name} for l in lots[:20]]}

    # Comparison tasks (latest 10)
    tasks = session.exec(
        select(LLMTask)
        .where(LLMTask.purchase_id == purchase_id, LLMTask.task_type == "lot_comparison")
        .order_by(LLMTask.created_at.desc())
    ).all()

    tasks_info = []
    for t in tasks[:10]:
        payload = _safe_json_dict(t.output_text)
        row_count = len(payload.get("rows", [])) if isinstance(payload.get("rows"), list) else 0
        tasks_info.append({
            "task_id": t.id,
            "bid_id": t.bid_id,
            "status": t.status,
            "row_count": row_count,
            "note": payload.get("note"),
            "stages": payload.get("stages"),
            "created_at": str(t.created_at),
            "updated_at": str(t.updated_at) if t.updated_at else None,
        })

    return {
        "purchase_id": purchase_id,
        "tz": tz_info,
        "bids": bid_info,
        "comparison_tasks": tasks_info,
    }


@app.post("/purchases/{purchase_id}/suppliers", response_model=SupplierRead, status_code=status.HTTP_201_CREATED)
def create_supplier(
    purchase_id: int,
    payload: SupplierCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> Supplier:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    supplier = Supplier(
        purchase_id=purchase_id,
        company_name=payload.company_name,
        website_url=payload.website_url,
        relevance_score=payload.relevance_score,
        reason=payload.reason,
    )
    session.add(supplier)
    session.commit()
    session.refresh(supplier)
    return supplier


@app.get("/purchases/{purchase_id}/suppliers", response_model=List[SupplierRead])
def list_suppliers(purchase_id: int, session=Depends(get_session), current_user: User = Depends(auth.get_current_user)) -> List[Supplier]:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
    return session.exec(select(Supplier).where(Supplier.purchase_id == purchase_id)).all()


@app.get(
    "/purchases/{purchase_id}/suppliers/export",
    response_class=StreamingResponse,
)
def export_suppliers_excel(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
):
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    suppliers = session.exec(select(Supplier).where(Supplier.purchase_id == purchase_id)).all()

    rows = []
    for supplier in suppliers:
        contacts = session.exec(select(SupplierContact).where(SupplierContact.supplier_id == supplier.id)).all()
        supplier_name = supplier.company_name or supplier.website_url or "Без названия"
        reason = supplier.reason or ""
        if contacts:
            for contact in contacts:
                rows.append(
                    {
                        "Поставщик": supplier_name,
                        "Сайт": supplier.website_url or "",
                        "Email": contact.email,
                        "Источник": contact.source_url or "Добавлено вручную",
                        "Комментарий": contact.reason or reason,
                        "Для рассылки": "Да" if contact.is_selected_for_request else "Нет",
                    }
                )
        else:
            rows.append(
                {
                    "Поставщик": supplier_name,
                    "Сайт": supplier.website_url or "",
                    "Email": "",
                    "Источник": "",
                    "Комментарий": reason,
                    "Для рассылки": "Нет",
                }
            )

    columns = ["Поставщик", "Сайт", "Email", "Источник", "Комментарий", "Для рассылки"]
    df = pd.DataFrame(rows, columns=columns)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Контакты")
    output.seek(0)

    filename = f"purchase_{purchase_id}_suppliers.xlsx"
    headers = {
        "Content-Disposition": f"attachment; filename=\"{filename}\"",
    }
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.post(
    "/purchases/{purchase_id}/suppliers/{supplier_id}/contacts",
    response_model=SupplierContactRead,
    status_code=status.HTTP_201_CREATED,
)
def add_supplier_contact(
    purchase_id: int,
    supplier_id: int,
    payload: SupplierContactCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> SupplierContact:
    purchase = session.get(Purchase, purchase_id)
    supplier = session.get(Supplier, supplier_id)
    if not purchase or purchase.user_id != current_user.id or not supplier or supplier.purchase_id != purchase_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    contact = SupplierContact(
        supplier_id=supplier_id,
        email=payload.email,
        source_url=payload.source_url,
        source=payload.source,
        confidence=payload.confidence,
        dedup_key=payload.dedup_key,
        reason=payload.reason,
        is_selected_for_request=payload.is_selected_for_request,
    )
    session.add(contact)
    session.commit()
    session.refresh(contact)
    return contact


@app.get("/suppliers/{supplier_id}/contacts", response_model=List[SupplierContactRead])
def list_contacts(
    supplier_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> List[SupplierContact]:
    supplier = session.get(Supplier, supplier_id)
    purchase = session.get(Purchase, supplier.purchase_id) if supplier else None
    if not supplier or not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    return session.exec(select(SupplierContact).where(SupplierContact.supplier_id == supplier_id)).all()


@app.post("/email/accounts", response_model=EmailAccountRead, status_code=status.HTTP_201_CREATED)
def save_email_account(
    payload: EmailAccountCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> EmailAccount:
    # naive pseudo-encryption to avoid storing plain password
    password_enc = payload.password[::-1] if payload.password else None
    account = EmailAccount(
        user_id=current_user.id,
        email=payload.email,
        imap_host=payload.imap_host,
        smtp_host=payload.smtp_host,
        smtp_port=payload.smtp_port,
        login=payload.login or payload.email,
        password_enc=password_enc,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


@app.get("/email/accounts", response_model=List[EmailAccountRead])
def list_email_accounts(session=Depends(get_session), current_user: User = Depends(auth.get_current_user)) -> List[EmailAccount]:
    return session.exec(select(EmailAccount).where(EmailAccount.user_id == current_user.id)).all()


@app.post("/purchases/{purchase_id}/emails", response_model=EmailMessageRead, status_code=status.HTTP_201_CREATED)
def create_email(
    purchase_id: int,
    payload: EmailMessageCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> EmailMessage:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    email_message = EmailMessage(
        purchase_id=purchase_id,
        supplier_contact_id=payload.supplier_contact_id,
        subject=payload.subject,
        body=payload.body,
        price_value=payload.price_value,
        currency=payload.currency,
        direction=payload.direction,
    )
    session.add(email_message)
    session.commit()
    session.refresh(email_message)
    return email_message


@app.get("/purchases/{purchase_id}/emails", response_model=List[EmailMessageRead])
def list_emails(purchase_id: int, session=Depends(get_session), current_user: User = Depends(auth.get_current_user)) -> List[EmailMessage]:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")
    return session.exec(select(EmailMessage).where(EmailMessage.purchase_id == purchase_id)).all()


@app.post("/purchases/{purchase_id}/llm-tasks", response_model=LLMTaskRead, status_code=status.HTTP_201_CREATED)
def create_llm_task(
    purchase_id: int,
    payload: LLMTaskCreate,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> LLMTask:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    task = LLMTask(
        purchase_id=purchase_id,
        task_type=payload.task_type,
        input_text=payload.input_text,
        status="queued",
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@app.post("/purchases/{purchase_id}/suppliers/search", response_model=SupplierSearchResponse)
def search_suppliers(
    purchase_id: int,
    payload: SupplierSearchRequest,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> SupplierSearchResponse:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    state = get_supplier_search_state(purchase_id)
    # Only return the existing state if it's still active. If the latest
    # task is in a terminal state (failed / completed without results),
    # we MUST create a new task — otherwise clicking "Запустить поиск"
    # silently re-renders the old failed state and looks like nothing
    # happened. (See incident 2026-04-12.)
    is_active_state = state is not None and state.status in ("queued", "in_progress")
    if not is_active_state:
        if payload.provider == "perplexity":
            task = task_queue.enqueue_supplier_search_perplexity_task(
                purchase_id,
                payload.terms_text or purchase.terms_text or "",
                payload.hints,
            )
        else:
            task = task_queue.enqueue_supplier_search_task(
                purchase_id,
                payload.terms_text or purchase.terms_text or "",
                payload.hints,
            )
        queue_length = get_supplier_search_queue_length()
        estimated_complete_time = datetime.utcnow() + timedelta(minutes=10 + queue_length * 10, hours=3)
        return SupplierSearchResponse(
            task_id=task.id or 0,
            status=task.status,
            queries=[],
            note="Поиск поставщиков поставлен в очередь",
            tech_task_excerpt="",
            search_output=[],
            processed_contacts=[],
            queue_length=queue_length,
            estimated_complete_time=estimated_complete_time,
        )

    if state.status == "completed" and not state.queries:
        try:
            plan = build_search_queries(
                payload.terms_text or purchase.terms_text or "",
                payload.hints,
                usage_ctx={"purchase_id": purchase_id, "user_id": current_user.id},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[search_queries_generation] restore_failed: {exc}")
            plan = None
        return SupplierSearchResponse(
            task_id=state.task_id,
            status=state.status,
            queries=plan.queries if plan else [],
            note=plan.note if plan else "Запросы не восстановлены: ошибка генерации через LLM",
            tech_task_excerpt=state.tech_task_excerpt,
            search_output=state.search_output,
            processed_contacts=state.processed_contacts,
            queue_length=state.queue_length,
            estimated_complete_time=state.estimated_complete_time,
        )

    return SupplierSearchResponse(
        task_id=state.task_id,
        status=state.status,
        queries=state.queries,
        note=state.note or "Поиск поставщиков выполняется",
        tech_task_excerpt=state.tech_task_excerpt,
        search_output=state.search_output,
        processed_contacts=state.processed_contacts,
        queue_length=state.queue_length,
        estimated_complete_time=state.estimated_complete_time,
    )


@app.get("/purchases/{purchase_id}/suppliers/search", response_model=SupplierSearchResponse | None)
def get_supplier_search_status(
    purchase_id: int,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> SupplierSearchResponse | None:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    state = get_supplier_search_state(purchase_id)
    if not state:
        return None

    return SupplierSearchResponse(
        task_id=state.task_id,
        status=state.status,
        queries=state.queries,
        note=state.note or "Поиск поставщиков выполняется",
        tech_task_excerpt=state.tech_task_excerpt,
        search_output=state.search_output,
        processed_contacts=state.processed_contacts,
        queue_length=state.queue_length,
        estimated_complete_time=state.estimated_complete_time,
    )


@app.post(
    "/purchases/{purchase_id}/suppliers/import-script-output",
    response_model=SupplierImportResult,
    status_code=status.HTTP_201_CREATED,
)
def import_suppliers_from_script(
    purchase_id: int,
    payload: SupplierImportRequest,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> SupplierImportResult:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    processed_contacts = payload.processed_contacts or []
    search_output = payload.search_output or []
    if not processed_contacts or not search_output:
        merged_contacts = load_contacts_from_files(payload.processed_contacts_path, payload.search_output_path)
    else:
        merged_contacts = merge_contacts(processed_contacts, search_output)

    if not merged_contacts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No suppliers_contacts.py data available to import.",
        )

    suppliers_created = 0
    suppliers_matched = 0
    contacts_created = 0

    for item in merged_contacts:
        if not item.get("is_relevant", True):
            continue

        website = item.get("website")
        if not website:
            continue

        supplier = session.exec(
            select(Supplier).where(
                Supplier.purchase_id == purchase_id,
                Supplier.website_url == website,
            )
        ).first()

        company_name = item.get("name")
        if not company_name:
            parsed = urlparse(website)
            company_name = parsed.hostname or website

        relevance_score = 1.0 if item.get("is_relevant", True) else 0.0
        reason = item.get("reason")

        if supplier:
            suppliers_matched += 1
            if not supplier.company_name and company_name:
                supplier.company_name = company_name
            if supplier.relevance_score is None:
                supplier.relevance_score = relevance_score
            if reason:
                supplier.reason = reason
        else:
            supplier = Supplier(
                purchase_id=purchase_id,
                company_name=company_name,
                website_url=website,
                relevance_score=relevance_score,
                reason=reason,
            )
            session.add(supplier)
            session.commit()
            session.refresh(supplier)
            suppliers_created += 1

        for email in item.get("emails", []):
            existing_contact = session.exec(
                select(SupplierContact).where(
                    SupplierContact.supplier_id == supplier.id,
                    SupplierContact.email == email,
                )
            ).first()
            if existing_contact:
                continue

            contact = SupplierContact(
                supplier_id=supplier.id,
                email=email,
                source_url=website,
                source=item.get("source"),
                confidence=item.get("confidence"),
                dedup_key=item.get("dedup_key"),
                reason=reason,
            )
            session.add(contact)
            contacts_created += 1

        session.add(supplier)
        session.commit()

    return SupplierImportResult(
        suppliers_created=suppliers_created,
        suppliers_matched=suppliers_matched,
        contacts_created=contacts_created,
    )


@app.post("/purchases/{purchase_id}/email-draft", response_model=EmailDraftResponse)
def build_email_draft(
    purchase_id: int,
    supplier_id: int | None = None,
    session=Depends(get_session),
    current_user: User = Depends(auth.get_current_user),
) -> EmailDraftResponse:
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase not found")

    supplier = session.get(Supplier, supplier_id) if supplier_id else None
    subject = f"Запрос КП: {purchase.full_name}"
    body = generate_email_body(purchase.full_name, purchase.terms_text or "", supplier.company_name if supplier else None)
    return EmailDraftResponse(subject=subject, body=body)
