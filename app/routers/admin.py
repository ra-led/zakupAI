from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import select, func, col

from ..auth import get_admin_user
from ..database import get_session
from ..models import Lead, Lot, Purchase, User
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
