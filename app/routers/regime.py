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
from ..services.check_runner import run_check_from_items

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/regime", tags=["regime"])


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------

class RegimeCheckItemOut(BaseModel):
    id: int
    check_id: int
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
    session=Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Start a regime check for a purchase.

    Creates a RegimeCheck record with status 'pending' and returns it.
    Background processing will be added later.
    """
    _get_user_purchase(session, purchase_id, user)

    check = RegimeCheck(
        purchase_id=purchase_id,
        user_id=user.id,
        status="pending",
        ok_count=0,
        warning_count=0,
        error_count=0,
        not_found_count=0,
    )
    session.add(check)
    session.commit()
    session.refresh(check)

    return check


@router.get(
    "/purchases/{purchase_id}/check",
    response_model=RegimeCheckOut,
)
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
    check.items = items  # type: ignore[attr-defined]

    return check


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
