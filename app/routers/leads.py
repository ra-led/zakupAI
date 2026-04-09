import threading

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select

from ..database import get_session
from ..models import Lead
from ..notify import send_lead_notification
from ..schemas import LeadCreate, LeadRead

router = APIRouter(prefix="/leads", tags=["leads"])


@router.post("", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadCreate, session=Depends(get_session)) -> Lead:
    lead = Lead(
        name=payload.name,
        email=payload.email,
        company=payload.company,
        phone=payload.phone,
    )
    session.add(lead)
    session.commit()
    session.refresh(lead)

    threading.Thread(
        target=send_lead_notification,
        args=(lead.name, lead.email, lead.company, lead.phone),
        daemon=True,
    ).start()

    return lead
