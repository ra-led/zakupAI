from datetime import datetime, timedelta
from typing import List, Optional

import json
import logging
import traceback

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlmodel import select, func, col
from sqlalchemy import exists

logger = logging.getLogger(__name__)

from ..auth import get_admin_user
from ..database import get_session
from ..models import Lead, LLMTask, LLMTrace, LLMUsage, Lot, Purchase, User
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
    total_purchases = session.exec(select(func.count(Purchase.id))).one()
    purchases_today = session.exec(
        select(func.count(Purchase.id)).where(col(Purchase.created_at) >= today_start)
    ).one()

    return AdminDashboard(
        total_users=total_users,
        new_users_today=new_users_today,
        total_purchases=total_purchases,
        purchases_today=purchases_today,
    )


@router.get("/users", response_model=List[AdminUserRead])
def list_users(
    q: Optional[str] = Query(default=None),
    _admin: User = Depends(get_admin_user),
    session=Depends(get_session),
) -> List[AdminUserRead]:
    stmt = select(User)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            col(User.email).ilike(pattern) | col(User.full_name).ilike(pattern)
        )
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
    user.is_admin = payload.is_admin
    session.add(user)
    session.commit()
    return {"ok": True, "is_admin": user.is_admin}


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


@router.post("/sandbox/convert")
async def sandbox_convert_file(
    file: UploadFile = File(...),
    _admin: User = Depends(get_admin_user),
) -> dict:
    """Convert a PDF/DOCX file to text for sandbox testing."""
    import httpx

    file_bytes = await file.read()
    form_data = {"file": (file.filename, file_bytes, file.content_type or "application/octet-stream")}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post("http://doc-to-md:3000/convert", files=form_data)
            resp.raise_for_status()
            data = resp.json()
            markdown = data.get("markdown", "")
            return {"markdown": markdown, "chars": len(markdown)}
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
