from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select

from ..auth import get_current_user
from ..database import get_session
from ..models import Purchase, RegimeCheck, RegimeCheckItem, User

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
