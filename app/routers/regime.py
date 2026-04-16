import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session as SMSession
from sqlmodel import select

from ..auth import get_current_user
from ..database import engine, get_session
from ..models import (
    Bid,
    BidLot,
    BidLotParameter,
    Purchase,
    RegimeCheck,
    RegimeCheckItem,
    User,
)
from ..services.check_runner import get_progress, run_check_from_items

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/regime", tags=["regime"])


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------

class RegimeCheckItemOut(BaseModel):
    id: int
    check_id: int
    source_bid_id: Optional[int] = None
    source_supplier: Optional[str] = None
    product_name: Optional[str] = None
    registry_number: Optional[str] = None
    okpd2_code: Optional[str] = None
    supplier_characteristics: Optional[str] = None
    registry_status: Optional[str] = None
    registry_actual: Optional[bool] = None
    registry_cert_end_date: Optional[str] = None
    registry_raw_url: Optional[str] = None
    localization_status: Optional[str] = None
    localization_actual_score: Optional[float] = None
    localization_required_score: Optional[float] = None
    localization_details: Optional[str] = None
    gisp_status: Optional[str] = None
    gisp_characteristics: Optional[str] = None
    gisp_comparison: Optional[str] = None
    gisp_url: Optional[str] = None
    overall_status: Optional[str] = None

    class Config:
        from_attributes = True


class RegimeCheckOut(BaseModel):
    id: int
    purchase_id: int
    status: str
    filename: Optional[str] = None
    ok_count: int
    warning_count: int
    error_count: int
    not_found_count: int
    created_at: datetime
    items: List[RegimeCheckItemOut] = []

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_purchase(session, purchase_id: int, user: User) -> Purchase:
    """Fetch a purchase and verify it belongs to the current user."""
    purchase = session.get(Purchase, purchase_id)
    if not purchase or purchase.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase not found",
        )
    return purchase


def _get_latest_check(session, purchase_id: int) -> RegimeCheck:
    """Return the most recent RegimeCheck for a purchase, or 404."""
    stmt = (
        select(RegimeCheck)
        .where(RegimeCheck.purchase_id == purchase_id)
        .order_by(RegimeCheck.created_at.desc())  # type: ignore[union-attr]
    )
    check = session.exec(stmt).first()
    if not check:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No regime check found for this purchase",
        )
    return check


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/purchases/{purchase_id}/check",
    response_model=RegimeCheckOut,
    status_code=status.HTTP_201_CREATED,
)
def start_regime_check(
    purchase_id: int,
    background_tasks: BackgroundTasks,
    session=Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Run M4 Нацрежим check across ALL bids (КП) for this purchase.

    Gathers items from every Bid that has parsed BidLot rows, merges them,
    and runs the full pipeline in the background. The caller polls
    ``GET /regime/purchases/{id}/check/progress`` to watch it progress.
    """
    _get_user_purchase(session, purchase_id, user)

    # Gather items from ALL bids for this purchase
    bids = session.exec(
        select(Bid).where(Bid.purchase_id == purchase_id)
    ).all()
    if not bids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Нет загруженных КП. Сначала загрузите коммерческие предложения.",
        )

    all_items: list[dict] = []
    bid_names: list[str] = []
    for bid in bids:
        items = _build_items_from_bid(session, bid.id)
        if items:
            label = bid.supplier_name or f"КП #{bid.id}"
            bid_names.append(f"{label} ({len(items)} поз.)")
            for item in items:
                item["_bid_id"] = bid.id
                item["_supplier_name"] = label
            all_items.extend(items)

    if not all_items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Ни в одном КП нет распознанных позиций. Сначала запустите "
                "распознавание лотов в модуле «Письма и КП»."
            ),
        )

    filename_label = "; ".join(bid_names)

    check = RegimeCheck(
        purchase_id=purchase_id,
        user_id=user.id,
        status="pending",
        ok_count=0,
        warning_count=0,
        error_count=0,
        not_found_count=0,
        filename=filename_label,
    )
    session.add(check)
    session.commit()
    session.refresh(check)

    background_tasks.add_task(_bg_run_check_from_items, check.id, all_items)
    logger.info(
        "[regime] enqueued check-all check_id=%s purchase=%s bids=%s items=%d",
        check.id, purchase_id, [b.id for b in bids], len(all_items),
    )

    return check


@router.get("/purchases/{purchase_id}/check")
def get_regime_check(
    purchase_id: int,
    session=Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Get the latest regime check for a purchase, including all items."""
    _get_user_purchase(session, purchase_id, user)
    check = _get_latest_check(session, purchase_id)

    # Eagerly load items
    items_stmt = (
        select(RegimeCheckItem)
        .where(RegimeCheckItem.check_id == check.id)
    )
    items = session.exec(items_stmt).all()

    # Serialize manually to avoid Pydantic validation issues
    items_out = []
    for item in items:
        items_out.append({
            "id": item.id,
            "check_id": item.check_id,
            "source_bid_id": getattr(item, "source_bid_id", None),
            "source_supplier": getattr(item, "source_supplier", None),
            "product_name": item.product_name,
            "registry_number": item.registry_number,
            "okpd2_code": item.okpd2_code,
            "supplier_characteristics": item.supplier_characteristics,
            "registry_status": item.registry_status,
            "registry_actual": item.registry_actual,
            "registry_cert_end_date": item.registry_cert_end_date,
            "registry_raw_url": item.registry_raw_url,
            "localization_status": item.localization_status,
            "localization_actual_score": item.localization_actual_score,
            "localization_required_score": item.localization_required_score,
            "localization_details": item.localization_details,
            "gisp_status": item.gisp_status,
            "gisp_characteristics": item.gisp_characteristics,
            "gisp_comparison": item.gisp_comparison,
            "gisp_url": item.gisp_url,
            "overall_status": item.overall_status,
        })

    return {
        "id": check.id,
        "purchase_id": check.purchase_id,
        "status": check.status,
        "filename": check.filename,
        "ok_count": check.ok_count or 0,
        "warning_count": check.warning_count or 0,
        "error_count": check.error_count or 0,
        "not_found_count": check.not_found_count or 0,
        "created_at": check.created_at.isoformat() if check.created_at else None,
        "items": items_out,
    }


@router.get(
    "/purchases/{purchase_id}/check/items",
    response_model=List[RegimeCheckItemOut],
)
def get_regime_check_items(
    purchase_id: int,
    session=Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Get all check items for the latest regime check of a purchase."""
    _get_user_purchase(session, purchase_id, user)
    check = _get_latest_check(session, purchase_id)

    items_stmt = (
        select(RegimeCheckItem)
        .where(RegimeCheckItem.check_id == check.id)
    )
    return session.exec(items_stmt).all()


# ---------------------------------------------------------------------------
# Real-time progress (in-memory, for polling UI)
# ---------------------------------------------------------------------------


@router.get("/purchases/{purchase_id}/check/progress")
def get_regime_check_progress(
    purchase_id: int,
    session=Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Return real-time pipeline progress for the latest regime check.

    Response shape::

        {
          "check_id": 42,
          "total": 5,
          "processed": 3,
          "status": "processing",
          "message": "Проверено 3 из 5",
          "stages": [
            {"name": "Парсинг файла", "status": "done", "detail": "5 позиций"},
            {"name": "Проверка товаров", "status": "in_progress", "detail": "3 из 5"},
            {"name": "Формирование отчёта", "status": "pending", "detail": ""}
          ]
        }
    """
    _get_user_purchase(session, purchase_id, user)
    check = _get_latest_check(session, purchase_id)
    progress = get_progress(check.id)
    return {"check_id": check.id, "filename": check.filename, **progress}


# ---------------------------------------------------------------------------
# Diagnostics (admin only)
# ---------------------------------------------------------------------------


@router.get("/purchases/{purchase_id}/check/diagnostics")
def get_regime_diagnostics(
    purchase_id: int,
    session=Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Return diagnostic info for M4 regime checks of a purchase (admin only)."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    _get_user_purchase(session, purchase_id, user)

    # All regime checks for this purchase (latest first)
    checks = session.exec(
        select(RegimeCheck)
        .where(RegimeCheck.purchase_id == purchase_id)
        .order_by(RegimeCheck.created_at.desc())  # type: ignore[union-attr]
    ).all()

    # All bids and their lot counts
    bids = session.exec(select(Bid).where(Bid.purchase_id == purchase_id)).all()
    bid_info = []
    for bid in bids:
        lot_count = len(session.exec(
            select(BidLot).where(BidLot.bid_id == bid.id)
        ).all())
        bid_info.append({
            "bid_id": bid.id,
            "supplier_name": bid.supplier_name,
            "lot_count": lot_count,
            "created_at": str(bid.created_at),
        })

    checks_info = []
    for c in checks[:10]:
        item_count = len(session.exec(
            select(RegimeCheckItem).where(RegimeCheckItem.check_id == c.id)
        ).all())
        progress = get_progress(c.id)
        checks_info.append({
            "check_id": c.id,
            "status": c.status,
            "filename": c.filename,
            "ok": c.ok_count,
            "warning": c.warning_count,
            "error": c.error_count,
            "not_found": c.not_found_count,
            "items_in_db": item_count,
            "created_at": str(c.created_at),
            "progress": progress,
        })

    return {
        "purchase_id": purchase_id,
        "bids": bid_info,
        "checks": checks_info,
    }


# ---------------------------------------------------------------------------
# Path 2: regime check from an existing M2 КП (BidLot rows)
# ---------------------------------------------------------------------------


def _build_items_from_bid(session, bid_id: int) -> list[dict]:
    """Translate BidLot + BidLotParameter rows into the dict shape that
    ``check_runner.run_check_from_items`` expects.

    Mirrors ``llm_tasks.kp_lots_to_check_items`` for ORM-backed input. Empty
    strings on registry_number / okpd2_code stay None so the downstream
    "if rn:" checks behave consistently.
    """
    bid_lots = session.exec(select(BidLot).where(BidLot.bid_id == bid_id)).all()
    items: list[dict] = []
    for lot in bid_lots:
        params = session.exec(
            select(BidLotParameter).where(BidLotParameter.bid_lot_id == lot.id)
        ).all()
        chars: list[dict] = []
        for p in params:
            pname = (p.name or "").strip()
            if not pname:
                continue
            value = (p.value or "").strip()
            units = (p.units or "").strip()
            if units:
                value = f"{value} {units}".strip()
            chars.append({"name": pname, "value": value})
        items.append({
            "name": lot.name,
            "registry_number": (lot.registry_number or "").strip() or None,
            "okpd2_code": (lot.okpd2_code or "").strip() or None,
            "characteristics": chars,
        })
    return items


async def _bg_run_check_from_items(check_id: int, items: list[dict]) -> None:
    """Background-task wrapper. Owns its own DB session, since the request
    session is closed by the time FastAPI dispatches background tasks."""
    with SMSession(engine) as db:
        try:
            await run_check_from_items(check_id, items, db)
        except Exception:
            logger.exception("[regime] background check_id=%s failed", check_id)


@router.post(
    "/purchases/{purchase_id}/check/from-bid/{bid_id}",
    response_model=RegimeCheckOut,
    status_code=status.HTTP_201_CREATED,
)
def start_regime_check_from_bid(
    purchase_id: int,
    bid_id: int,
    background_tasks: BackgroundTasks,
    session=Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Run an M4 Нацрежим check against an already-parsed M2 КП (no file upload).

    Preconditions:
      * The purchase must belong to the current user.
      * The bid must belong to that purchase.
      * The bid must already have BidLot rows (M2 КП parsing must have completed).
        If not, the caller should first run M2 lot extraction and retry.

    The check itself runs in the background. The response returns the freshly
    created RegimeCheck row with status='pending'; the caller polls
    ``GET /regime/purchases/{id}/check`` to watch it progress.
    """
    purchase = _get_user_purchase(session, purchase_id, user)

    bid = session.get(Bid, bid_id)
    if not bid or bid.purchase_id != purchase.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bid not found in this purchase",
        )

    items = _build_items_from_bid(session, bid_id)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "У этого КП ещё нет распознанных позиций. Сначала запустите "
                "распознавание лотов в модуле «Письма и КП»."
            ),
        )

    check = RegimeCheck(
        purchase_id=purchase_id,
        user_id=user.id,
        status="pending",
        ok_count=0,
        warning_count=0,
        error_count=0,
        not_found_count=0,
        filename=f"bid #{bid_id}",
    )
    session.add(check)
    session.commit()
    session.refresh(check)

    background_tasks.add_task(_bg_run_check_from_items, check.id, items)
    logger.info(
        "[regime] enqueued path-2 check check_id=%s purchase=%s bid=%s items=%d",
        check.id, purchase_id, bid_id, len(items),
    )

    return check
