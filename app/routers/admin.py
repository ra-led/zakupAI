from datetime import datetime, timedelta
from typing import List, Optional

import json
import logging
import os
import traceback

SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "qwadro@mail.ru").strip().lower()


def _is_superadmin(user: "User") -> bool:
    return bool(user and user.email and user.email.strip().lower() == SUPERADMIN_EMAIL)

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlmodel import select, func, col
from sqlalchemy import exists

logger = logging.getLogger(__name__)

from ..auth import get_admin_user
from ..database import get_session
from ..models import (
    Bid,
    BidLot,
    BidLotParameter,
    Lead,
    LLMTask,
    LLMTrace,
    LLMUsage,
    Lot,
    LotParameter,
    Purchase,
    PurchaseFile,
    RegimeCheck,
    RegimeCheckItem,
    SessionToken,
    Supplier,
    User,
)
from ..notify import send_activation_notification
from ..schemas import AdminDashboard, AdminPurchaseRead, AdminUserRead, LeadRead

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/dashboard", response_model=AdminDashboard)
def get_dashboard(
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> AdminDashboard:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = session.exec(select(func.count(User.id))).one()
    new_users_today = session.exec(
        select(func.count(User.id)).where(col(User.created_at) >= today_start)
    ).one()
    pending_users_count = session.exec(
        select(func.count(User.id)).where(User.is_active == False)  # noqa: E712
    ).one()
    total_purchases = session.exec(select(func.count(Purchase.id))).one()
    purchases_today = session.exec(
        select(func.count(Purchase.id)).where(col(Purchase.created_at) >= today_start)
    ).one()

    return AdminDashboard(
        total_users=total_users,
        new_users_today=new_users_today,
        total_purchases=total_purchases,
        purchases_today=purchases_today,
        pending_users_count=pending_users_count,
    )


@router.get("/queue")
def queue_depth(
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
):
    # Сигнализирует о перегрузке воркеров: сколько задач по типам висит в queued
    # и как долго самая старая ждёт. Источник для алертинга (cron → Telegram/email).
    now = datetime.utcnow()
    rows = session.exec(
        select(
            LLMTask.task_type,
            LLMTask.status,
            func.count(LLMTask.id).label("count"),
            func.min(LLMTask.created_at).label("oldest_created_at"),
        )
        .where(col(LLMTask.status).in_(["queued", "in_progress"]))
        .group_by(LLMTask.task_type, LLMTask.status)
    ).all()

    buckets: dict = {}
    alerts: list = []
    total_active = 0
    for r in rows:
        key = f"{r.task_type}.{r.status}"
        oldest_age = int((now - r.oldest_created_at).total_seconds()) if r.oldest_created_at else 0
        count = int(r.count)
        buckets[key] = {"count": count, "oldest_age_seconds": oldest_age}
        total_active += count
        if r.status == "queued" and count > 10:
            alerts.append(f"{r.task_type}: {count} queued")
        if r.status == "queued" and oldest_age > 600:
            alerts.append(f"{r.task_type}: oldest queued {oldest_age // 60}m old")

    return {
        "timestamp": now.isoformat(),
        "buckets": buckets,
        "alerts": alerts,
        "total_active": total_active,
    }


@router.get("/users", response_model=List[AdminUserRead])
def list_users(
    q: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> List[AdminUserRead]:
    stmt = select(User)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            col(User.email).ilike(pattern) | col(User.full_name).ilike(pattern)
        )
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    stmt = stmt.order_by(col(User.created_at).desc())
    users = session.exec(stmt).all()

    result = []
    for u in users:
        purchase_count = session.exec(
            select(func.count(Purchase.id)).where(Purchase.user_id == u.id)
        ).one()
        result.append(
            AdminUserRead(
                id=u.id,
                email=u.email,
                full_name=u.full_name,
                organization=u.organization,
                is_admin=u.is_admin,
                is_active=u.is_active,
                created_at=u.created_at,
                last_login_at=u.last_login_at,
                purchase_count=purchase_count,
            )
        )
    return result


class ToggleAdminRequest(BaseModel):
    is_admin: bool


@router.patch("/users/{user_id}/admin")
def toggle_admin(
    user_id: int,
    payload: ToggleAdminRequest,
    admin: User = Depends(get_admin_user),
    session=Depends(get_session),
):
    if admin.id == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot change own admin status")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if _is_superadmin(user) and not _is_superadmin(admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Суперадмин защищён от изменений другими администраторами",
        )
    user.is_admin = payload.is_admin
    session.add(user)
    session.commit()
    return {"ok": True, "is_admin": user.is_admin}


class ToggleActiveRequest(BaseModel):
    is_active: bool
    notify: bool = True


@router.patch("/users/{user_id}/active")
def toggle_active(
    user_id: int,
    payload: ToggleActiveRequest,
    admin: User = Depends(get_admin_user),
    session=Depends(get_session),
):
    if admin.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change own active status",
        )
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if _is_superadmin(user) and not _is_superadmin(admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Суперадмин защищён от изменений другими администраторами",
        )

    was_inactive = not user.is_active
    user.is_active = payload.is_active
    session.add(user)
    session.commit()

    if payload.is_active and was_inactive and payload.notify:
        import threading
        threading.Thread(
            target=send_activation_notification,
            args=(user.email, user.full_name),
            daemon=True,
        ).start()

    return {"ok": True, "is_active": user.is_active}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    session=Depends(get_session),
):
    """Анонимизация аккаунта (soft-delete под 152-ФЗ):
    email/ФИО/организация затираются, пароль делается невалидным,
    активные сессии сбрасываются. Закупки и LLM-usage сохраняются для биллинга."""
    if admin.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить собственный аккаунт",
        )
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")
    if _is_superadmin(user) and not _is_superadmin(admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Суперадмин защищён от удаления другими администраторами",
        )

    anonymized_marker = f"deleted+{user.id}@anonymized.local"
    if user.email == anonymized_marker:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Аккаунт уже удалён",
        )

    original_email = user.email
    user.email = anonymized_marker
    user.full_name = "Удалён"
    user.organization = None
    user.is_active = False
    user.is_admin = False
    user.password_hash = "!DELETED!"
    session.add(user)

    tokens = session.exec(select(SessionToken).where(SessionToken.user_id == user.id)).all()
    for t in tokens:
        session.delete(t)

    session.commit()
    logger.info(
        "admin_user_deleted admin_id=%s user_id=%s original_email=%s sessions_revoked=%s",
        admin.id, user.id, original_email, len(tokens),
    )
    return {"ok": True, "anonymized": True, "sessions_revoked": len(tokens)}


@router.get("/leads", response_model=List[LeadRead])
def list_leads(
    status: Optional[str] = Query(default=None),
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> List[LeadRead]:
    stmt = select(Lead)
    if status:
        stmt = stmt.where(Lead.status == status)
    stmt = stmt.order_by(col(Lead.created_at).desc())
    return session.exec(stmt).all()


@router.get("/usage")
def get_usage_summary(
    days: int = Query(default=30, ge=1, le=365),
    purchase_id: Optional[int] = Query(default=None),
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> dict:
    """Aggregated LLM/search API usage stats for admins.

    Returns totals + breakdowns by channel, operation, and top purchases.
    Token / cost values come straight from provider responses (we never
    hardcode prices). Cost can be NULL when the provider didn't return it
    (Yandex always; Perplexity sometimes) — UI должен это показать как «—».
    """
    since = datetime.utcnow() - timedelta(days=days)

    base_filter = [col(LLMUsage.created_at) >= since]
    if purchase_id is not None:
        base_filter.append(LLMUsage.purchase_id == purchase_id)

    def _row_to_dict(row, *fields):
        return {field: getattr(row, field) if hasattr(row, field) else row[i] for i, field in enumerate(fields)}

    # Totals
    totals_row = session.exec(
        select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
            func.coalesce(func.sum(LLMUsage.request_count), 0),
        ).where(*base_filter)
    ).one()

    totals = {
        "rows": int(totals_row[0] or 0),
        "prompt_tokens": int(totals_row[1] or 0),
        "completion_tokens": int(totals_row[2] or 0),
        "total_tokens": int(totals_row[3] or 0),
        "cost_usd": round(float(totals_row[4] or 0.0), 6),
        "requests": int(totals_row[5] or 0),
    }

    # Breakdown by channel
    by_channel_rows = session.exec(
        select(
            LLMUsage.channel,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
            func.coalesce(func.sum(LLMUsage.request_count), 0),
        )
        .where(*base_filter)
        .group_by(LLMUsage.channel)
        .order_by(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0).desc())
    ).all()
    by_channel = [
        {
            "channel": r[0],
            "calls": int(r[1] or 0),
            "total_tokens": int(r[2] or 0),
            "cost_usd": round(float(r[3] or 0.0), 6),
            "requests": int(r[4] or 0),
        }
        for r in by_channel_rows
    ]

    # Breakdown by operation
    by_operation_rows = session.exec(
        select(
            LLMUsage.operation,
            LLMUsage.channel,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
        )
        .where(*base_filter)
        .group_by(LLMUsage.operation, LLMUsage.channel)
        .order_by(func.count(LLMUsage.id).desc())
    ).all()
    by_operation = [
        {
            "operation": r[0],
            "channel": r[1],
            "calls": int(r[2] or 0),
            "total_tokens": int(r[3] or 0),
            "cost_usd": round(float(r[4] or 0.0), 6),
        }
        for r in by_operation_rows
    ]

    # Top purchases by cost
    top_purchases_rows = session.exec(
        select(
            LLMUsage.purchase_id,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
        )
        .where(*base_filter, LLMUsage.purchase_id.is_not(None))
        .group_by(LLMUsage.purchase_id)
        .order_by(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0).desc())
        .limit(20)
    ).all()
    top_purchases = []
    for r in top_purchases_rows:
        pid = r[0]
        purchase = session.get(Purchase, pid) if pid else None
        top_purchases.append(
            {
                "purchase_id": pid,
                "purchase_name": (purchase.custom_name or purchase.full_name) if purchase else None,
                "calls": int(r[1] or 0),
                "total_tokens": int(r[2] or 0),
                "cost_usd": round(float(r[3] or 0.0), 6),
            }
        )

    # Errors count
    errors_count = session.exec(
        select(func.count(LLMUsage.id)).where(*base_filter, LLMUsage.success == False)  # noqa: E712
    ).one()

    return {
        "since": since.isoformat() + "Z",
        "days": days,
        "purchase_id": purchase_id,
        "totals": totals,
        "errors": int(errors_count or 0),
        "by_channel": by_channel,
        "by_operation": by_operation,
        "top_purchases": top_purchases,
        "note": (
            "cost_usd берётся из ответа API провайдера; для каналов без cost (Yandex, "
            "часть Perplexity-моделей) поле может быть 0 — смотрите request_count и total_tokens."
        ),
    }


@router.get("/purchases", response_model=List[AdminPurchaseRead])
def list_all_purchases(
    user_id: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> List[AdminPurchaseRead]:
    stmt = select(Purchase)
    if user_id:
        stmt = stmt.where(Purchase.user_id == user_id)
    if status:
        stmt = stmt.where(Purchase.status == status)
    stmt = stmt.order_by(col(Purchase.created_at).desc())
    purchases = session.exec(stmt).all()

    result = []
    for p in purchases:
        user = session.get(User, p.user_id)
        lots_count = session.exec(
            select(func.count(Lot.id)).where(Lot.purchase_id == p.id)
        ).one()
        result.append(
            AdminPurchaseRead(
                id=p.id,
                user_email=user.email if user else "unknown",
                auto_number=p.auto_number,
                full_name=p.full_name,
                custom_name=p.custom_name,
                status=p.status,
                lots_count=lots_count,
                created_at=p.created_at,
            )
        )
    return result


# ---------------------------------------------------------------------------
# LLM Trace endpoints
# ---------------------------------------------------------------------------


@router.get("/trace/purchases")
def list_traced_purchases(
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> list:
    """List purchases that have LLM calls, for the trace purchase picker."""
    stmt = (
        select(
            LLMUsage.purchase_id,
            func.count(LLMUsage.id).label("call_count"),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("total_tokens"),
            func.max(LLMUsage.created_at).label("last_call"),
        )
        .where(LLMUsage.purchase_id.is_not(None))
        .group_by(LLMUsage.purchase_id)
        .order_by(func.max(LLMUsage.created_at).desc())
        .limit(limit)
    )

    if q:
        purchase_ids = session.exec(
            select(Purchase.id).where(
                col(Purchase.full_name).ilike(f"%{q}%")
                | col(Purchase.custom_name).ilike(f"%{q}%")
            )
        ).all()
        if not purchase_ids:
            return []
        stmt = stmt.where(LLMUsage.purchase_id.in_(purchase_ids))

    rows = session.exec(stmt).all()
    result = []
    for r in rows:
        pid = r[0]
        purchase = session.get(Purchase, pid) if pid else None
        has_traces = session.exec(
            select(exists().where(
                LLMTrace.usage_id == LLMUsage.id,
                LLMUsage.purchase_id == pid,
            ))
        ).one()
        result.append({
            "id": pid,
            "name": (purchase.custom_name or purchase.full_name) if purchase else f"#{pid}",
            "created_at": purchase.created_at.isoformat() + "Z" if purchase else None,
            "call_count": int(r[1]),
            "total_tokens": int(r[2]),
            "last_call": r[3].isoformat() + "Z" if r[3] else None,
            "has_traces": has_traces,
            "is_archived": purchase.is_archived if purchase else False,
        })
    return result


@router.get("/trace/purchases/{purchase_id}")
def get_purchase_trace(
    purchase_id: int,
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> dict:
    """Full trace timeline for a purchase (without message bodies — lazy load)."""
    purchase = session.get(Purchase, purchase_id)

    usage_rows = session.exec(
        select(LLMUsage)
        .where(LLMUsage.purchase_id == purchase_id)
        .order_by(col(LLMUsage.created_at).asc())
    ).all()

    trace_usage_ids = set(session.exec(
        select(LLMTrace.usage_id).where(
            LLMTrace.usage_id.in_([u.id for u in usage_rows])
        )
    ).all()) if usage_rows else set()

    calls = []
    total_tokens = 0
    total_cost = 0.0
    total_duration = 0
    for u in usage_rows:
        calls.append({
            "usage_id": u.id,
            "operation": u.operation,
            "channel": u.channel,
            "model": u.model,
            "task_id": u.task_id,
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "total_tokens": u.total_tokens,
            "cost_usd": round(u.cost_usd, 6) if u.cost_usd else None,
            "duration_ms": u.duration_ms,
            "success": u.success,
            "error_message": u.error_message,
            "has_trace": u.id in trace_usage_ids,
            "created_at": u.created_at.isoformat() + "Z",
        })
        total_tokens += u.total_tokens or 0
        total_cost += u.cost_usd or 0.0
        total_duration += u.duration_ms or 0

    task_rows = session.exec(
        select(LLMTask)
        .where(LLMTask.purchase_id == purchase_id)
        .order_by(col(LLMTask.created_at).asc())
    ).all()
    tasks = [
        {
            "id": t.id,
            "task_type": t.task_type,
            "status": t.status,
            "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
            "updated_at": t.updated_at.isoformat() + "Z" if t.updated_at else None,
        }
        for t in task_rows
    ]

    return {
        "purchase_id": purchase_id,
        "purchase_name": (purchase.custom_name or purchase.full_name) if purchase else None,
        "calls": calls,
        "tasks": tasks,
        "summary": {
            "total_calls": len(calls),
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "total_duration_ms": total_duration,
        },
    }


@router.get("/trace/calls/{usage_id}")
def get_call_trace(
    usage_id: int,
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> dict:
    """Lazy-load the full request/response for a single LLM call."""
    trace = session.exec(
        select(LLMTrace).where(LLMTrace.usage_id == usage_id)
    ).first()

    if not trace:
        raise HTTPException(status_code=404, detail="No trace found for this call")

    try:
        messages = json.loads(trace.request_messages)
    except (json.JSONDecodeError, TypeError):
        messages = trace.request_messages

    return {
        "usage_id": usage_id,
        "request_messages": messages,
        "response_content": trace.response_content,
        "duration_ms": trace.duration_ms,
    }


# ---------------------------------------------------------------------------
# LLM Sandbox endpoints
# ---------------------------------------------------------------------------


@router.post("/track-conversion")
def track_conversion_usage(
    payload: dict,
    _user: User = Depends(get_admin_user),
) -> dict:
    """Record doc-to-md (Mistral OCR) usage from frontend calls."""
    from ..usage_tracking import record_usage, save_trace

    usage = payload.get("usage") or {}
    if not usage:
        return {"ok": False, "reason": "no usage data"}

    usage_id = record_usage(
        channel="mistral_ocr",
        operation="pdf_conversion",
        model=usage.get("model"),
        duration_ms=usage.get("duration_ms"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        purchase_id=payload.get("purchase_id"),
        request_count=1,
    )

    # Save a trace record so LLM Trace tab shows it as expandable
    if usage_id:
        pages = usage.get("pages_count")
        summary = "Mistral OCR: model={}, pages={}, duration={}ms".format(
            usage.get("model", "?"), pages or "?", usage.get("duration_ms", "?"),
        )
        save_trace(
            usage_id=usage_id,
            request_messages=[{"role": "system", "content": "PDF → Markdown conversion via Mistral OCR API"}],
            response_content=summary,
            duration_ms=usage.get("duration_ms"),
        )

    return {"ok": True}


@router.post("/sandbox/convert")
async def sandbox_convert_file(
    file: UploadFile = File(...),
    _admin: User = Depends(get_admin_user),
) -> dict:
    """Convert a PDF/DOCX file to text for sandbox testing."""
    import httpx
    from ..usage_tracking import record_usage

    file_bytes = await file.read()
    form_data = {"file": (file.filename, file_bytes, file.content_type or "application/octet-stream")}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post("http://doc-to-md:8001/convert", files=form_data)
            resp.raise_for_status()
            data = resp.json()
            markdown = data.get("markdown", "")
            usage = data.get("usage") or {}

            # Record usage for PDF conversion (Mistral OCR)
            if usage:
                record_usage(
                    channel="mistral_ocr",
                    operation="pdf_conversion",
                    model=usage.get("model"),
                    duration_ms=usage.get("duration_ms"),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    request_count=1,
                )

            return {
                "markdown": markdown,
                "chars": len(markdown),
                "usage": usage if usage else None,
            }
    except Exception as exc:
        logger.warning("[sandbox] convert failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Conversion failed: {exc}")


@router.post("/sandbox/run")
async def run_sandbox_step(
    step: str = Form(...),
    input_text: Optional[str] = Form(default=None),
    input_json: Optional[str] = Form(default=None),
    _admin: User = Depends(get_admin_user),
) -> dict:
    """Run a single pipeline step in sandbox mode (no purchase context)."""
    import time as _time

    from ..services import llm_tasks

    result = None
    usage_info = {}
    trace_info = {}

    try:
        if step == "lots_extraction":
            if not input_text:
                raise ValueError("input_text is required for lots_extraction")
            t0 = _time.monotonic()
            result = llm_tasks.extract_lots(input_text)
            usage_info["duration_ms"] = int((_time.monotonic() - t0) * 1000)

        elif step == "search_queries":
            if not input_text:
                raise ValueError("input_text is required for search_queries")
            t0 = _time.monotonic()
            plan = llm_tasks.build_search_queries(input_text)
            result = {"queries": plan.queries, "note": plan.note}
            usage_info["duration_ms"] = int((_time.monotonic() - t0) * 1000)

        elif step == "kp_extraction":
            if not input_text:
                raise ValueError("input_text is required for kp_extraction")
            t0 = _time.monotonic()
            result = llm_tasks.parse_kp(input_text)
            usage_info["duration_ms"] = int((_time.monotonic() - t0) * 1000)

        elif step == "compare_characteristics":
            if not input_json:
                raise ValueError("input_json is required for compare_characteristics")
            data = json.loads(input_json)
            t0 = _time.monotonic()
            result = await llm_tasks.compare_characteristics(
                supplier_chars=data["supplier_chars"],
                gisp_chars=data["gisp_chars"],
                product_name=data.get("product_name", ""),
            )
            usage_info["duration_ms"] = int((_time.monotonic() - t0) * 1000)

        elif step == "perplexity_postprocess":
            if not input_text:
                raise ValueError("input_text is required for perplexity_postprocess")
            terms = ""
            if input_json:
                try:
                    terms = json.loads(input_json).get("terms_text", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            t0 = _time.monotonic()
            result = llm_tasks.extract_structured_contacts_from_perplexity(input_text, terms)
            usage_info["duration_ms"] = int((_time.monotonic() - t0) * 1000)

        else:
            raise ValueError(f"Unknown sandbox step: {step}")

        # Fetch the latest usage row to get token/cost info
        from ..database import engine as _engine
        from sqlmodel import Session as _Session
        with _Session(_engine) as s:
            latest = s.exec(
                select(LLMUsage)
                .order_by(col(LLMUsage.created_at).desc())
                .limit(1)
            ).first()
            if latest:
                usage_info.update({
                    "prompt_tokens": latest.prompt_tokens,
                    "completion_tokens": latest.completion_tokens,
                    "total_tokens": latest.total_tokens,
                    "cost_usd": round(latest.cost_usd, 6) if latest.cost_usd else None,
                    "model": latest.model,
                    "duration_ms": latest.duration_ms or usage_info.get("duration_ms"),
                })
                trace_row = s.exec(
                    select(LLMTrace).where(LLMTrace.usage_id == latest.id)
                ).first()
                if trace_row:
                    try:
                        trace_info["request_messages"] = json.loads(trace_row.request_messages)
                    except (json.JSONDecodeError, TypeError):
                        trace_info["request_messages"] = trace_row.request_messages
                    trace_info["response_content"] = trace_row.response_content

        return {
            "step": step,
            "success": True,
            "result": result,
            "usage": usage_info,
            "trace": trace_info if trace_info else None,
            "error": None,
        }

    except Exception as exc:
        logger.warning("[sandbox] step %s failed: %s", step, exc)
        return {
            "step": step,
            "success": False,
            "result": None,
            "usage": usage_info,
            "trace": None,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Per-user detail + original file downloads (PR 2)
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/detail")
def get_user_detail(
    user_id: int,
    days: int = Query(default=30, ge=1, le=365),
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> dict:
    """Full profile for one user: purchases, file counts, LLM usage by operation.

    Used by the admin UI "Детали пользователя" modal — this is the main
    surface for collecting pilot usage insights and reviewing customer data.
    """
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    since = datetime.utcnow() - timedelta(days=days)

    purchases = session.exec(
        select(Purchase)
        .where(Purchase.user_id == user_id)
        .order_by(col(Purchase.created_at).desc())
    ).all()

    purchases_payload = []
    total_files = 0
    for p in purchases:
        lots_count = session.exec(
            select(func.count(Lot.id)).where(Lot.purchase_id == p.id)
        ).one()
        suppliers_count = session.exec(
            select(func.count(Supplier.id)).where(Supplier.purchase_id == p.id)
        ).one()
        bids_count = session.exec(
            select(func.count(Bid.id)).where(Bid.purchase_id == p.id)
        ).one()
        files = session.exec(
            select(PurchaseFile)
            .where(PurchaseFile.purchase_id == p.id)
            .order_by(col(PurchaseFile.created_at).desc())
        ).all()
        total_files += len(files)
        purchases_payload.append({
            "id": p.id,
            "auto_number": p.auto_number,
            "full_name": p.full_name,
            "custom_name": p.custom_name,
            "status": p.status,
            "is_archived": p.is_archived,
            "created_at": p.created_at.isoformat() + "Z",
            "updated_at": p.updated_at.isoformat() + "Z" if p.updated_at else None,
            "lots_count": int(lots_count or 0),
            "suppliers_count": int(suppliers_count or 0),
            "bids_count": int(bids_count or 0),
            "files": [
                {
                    "id": f.id,
                    "filename": f.filename,
                    "file_type": f.file_type,
                    "size_bytes": f.size_bytes,
                    "mime_type": f.mime_type,
                    "has_original": bool(f.storage_path),
                    "created_at": f.created_at.isoformat() + "Z",
                }
                for f in files
            ],
        })

    usage_filter = [LLMUsage.user_id == user_id, col(LLMUsage.created_at) >= since]
    totals_row = session.exec(
        select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
            func.coalesce(func.sum(LLMUsage.request_count), 0),
        ).where(*usage_filter)
    ).one()

    by_operation_rows = session.exec(
        select(
            LLMUsage.operation,
            LLMUsage.channel,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
        )
        .where(*usage_filter)
        .group_by(LLMUsage.operation, LLMUsage.channel)
        .order_by(func.count(LLMUsage.id).desc())
    ).all()

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "organization": user.organization,
            "is_admin": user.is_admin,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() + "Z",
            "last_login_at": user.last_login_at.isoformat() + "Z" if user.last_login_at else None,
        },
        "totals": {
            "purchase_count": len(purchases),
            "file_count": total_files,
            "llm_calls": int(totals_row[0] or 0),
            "llm_tokens": int(totals_row[1] or 0),
            "llm_cost_usd": round(float(totals_row[2] or 0.0), 6),
            "llm_requests": int(totals_row[3] or 0),
        },
        "usage_window_days": days,
        "usage_by_operation": [
            {
                "operation": r[0],
                "channel": r[1],
                "calls": int(r[2] or 0),
                "total_tokens": int(r[3] or 0),
                "cost_usd": round(float(r[4] or 0.0), 6),
            }
            for r in by_operation_rows
        ],
        "purchases": purchases_payload,
    }


@router.get("/purchases/{purchase_id}/files/{file_id}/download")
def admin_download_purchase_file(
    purchase_id: int,
    file_id: int,
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
):
    """Admin-only download of the original uploaded file (ТЗ/КП).

    Returns 404 if the file was uploaded before PR 2 (no storage_path).
    """
    from fastapi.responses import FileResponse
    from ..services.file_storage import resolve

    pf = session.get(PurchaseFile, file_id)
    if not pf or pf.purchase_id != purchase_id:
        raise HTTPException(status_code=404, detail="File not found")
    if not pf.storage_path:
        raise HTTPException(
            status_code=404,
            detail="Original file not stored — uploaded before file persistence was enabled",
        )
    try:
        disk = resolve(pf.storage_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File is referenced in DB but missing on disk")

    return FileResponse(
        path=str(disk),
        media_type=pf.mime_type or "application/octet-stream",
        filename=pf.filename,
    )


@router.get("/purchases/{purchase_id}/snapshot")
def admin_purchase_snapshot(
    purchase_id: int,
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> dict:
    """JSON dump of everything we have about a purchase — for offline analysis."""
    purchase = session.get(Purchase, purchase_id)
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    user = session.get(User, purchase.user_id)

    lots = session.exec(select(Lot).where(Lot.purchase_id == purchase_id)).all()
    lots_payload = []
    for lot in lots:
        params = session.exec(
            select(LotParameter).where(LotParameter.lot_id == lot.id)
        ).all()
        lots_payload.append({
            "id": lot.id,
            "name": lot.name,
            "parameters": [
                {"name": pr.name, "value": pr.value, "units": pr.units}
                for pr in params
            ],
        })

    suppliers = session.exec(
        select(Supplier).where(Supplier.purchase_id == purchase_id)
    ).all()

    bids = session.exec(select(Bid).where(Bid.purchase_id == purchase_id)).all()
    bids_payload = []
    for bid in bids:
        bid_lots = session.exec(select(BidLot).where(BidLot.bid_id == bid.id)).all()
        bid_lots_payload = []
        for bl in bid_lots:
            params = session.exec(
                select(BidLotParameter).where(BidLotParameter.bid_lot_id == bl.id)
            ).all()
            bid_lots_payload.append({
                "id": bl.id,
                "name": bl.name,
                "price": bl.price,
                "registry_number": bl.registry_number,
                "okpd2_code": bl.okpd2_code,
                "parameters": [
                    {"name": pr.name, "value": pr.value, "units": pr.units}
                    for pr in params
                ],
            })
        bids_payload.append({
            "id": bid.id,
            "supplier_name": bid.supplier_name,
            "supplier_contact": bid.supplier_contact,
            "created_at": bid.created_at.isoformat() + "Z",
            "bid_lots": bid_lots_payload,
        })

    regime_checks = session.exec(
        select(RegimeCheck).where(RegimeCheck.purchase_id == purchase_id)
    ).all()
    regime_payload = []
    for rc in regime_checks:
        items = session.exec(
            select(RegimeCheckItem).where(RegimeCheckItem.check_id == rc.id)
        ).all()
        regime_payload.append({
            "id": rc.id,
            "status": rc.status,
            "filename": rc.filename,
            "created_at": rc.created_at.isoformat() + "Z",
            "ok_count": rc.ok_count,
            "warning_count": rc.warning_count,
            "error_count": rc.error_count,
            "not_found_count": rc.not_found_count,
            "items": [
                {
                    "id": it.id,
                    "product_name": it.product_name,
                    "registry_number": it.registry_number,
                    "okpd2_code": it.okpd2_code,
                    "overall_status": it.overall_status,
                    "registry_status": it.registry_status,
                    "localization_status": it.localization_status,
                    "gisp_status": it.gisp_status,
                }
                for it in items
            ],
        })

    files = session.exec(
        select(PurchaseFile)
        .where(PurchaseFile.purchase_id == purchase_id)
        .order_by(col(PurchaseFile.created_at).desc())
    ).all()

    return {
        "purchase": {
            "id": purchase.id,
            "user_id": purchase.user_id,
            "user_email": user.email if user else None,
            "auto_number": purchase.auto_number,
            "full_name": purchase.full_name,
            "custom_name": purchase.custom_name,
            "terms_text": purchase.terms_text,
            "status": purchase.status,
            "nmck_value": purchase.nmck_value,
            "nmck_currency": purchase.nmck_currency,
            "is_archived": purchase.is_archived,
            "created_at": purchase.created_at.isoformat() + "Z",
            "updated_at": purchase.updated_at.isoformat() + "Z" if purchase.updated_at else None,
        },
        "lots": lots_payload,
        "suppliers": [
            {
                "id": s.id,
                "company_name": s.company_name,
                "website_url": s.website_url,
                "relevance_score": s.relevance_score,
                "reason": s.reason,
            }
            for s in suppliers
        ],
        "bids": bids_payload,
        "regime_checks": regime_payload,
        "files": [
            {
                "id": f.id,
                "filename": f.filename,
                "file_type": f.file_type,
                "size_bytes": f.size_bytes,
                "mime_type": f.mime_type,
                "sha256": f.sha256,
                "has_original": bool(f.storage_path),
                "download_url": (
                    f"/admin/purchases/{purchase_id}/files/{f.id}/download"
                    if f.storage_path else None
                ),
                "created_at": f.created_at.isoformat() + "Z",
            }
            for f in files
        ],
    }
