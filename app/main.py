import os
from datetime import datetime
from typing import List
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import select

from . import auth
from .database import create_db_and_tables, get_session
from .llm_stub import build_search_queries, generate_email_body
from .models import (
    EmailAccount,
    EmailMessage,
    LLMTask,
    Purchase,
    Supplier,
    SupplierContact,
    User,
)
from .schemas import (
    EmailAccountCreate,
    EmailAccountRead,
    EmailDraftResponse,
    EmailMessageCreate,
    EmailMessageRead,
    LLMTaskCreate,
    LLMTaskRead,
    PurchaseCreate,
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
    TokenResponse,
    UserCreate,
    UserRead,
)
from .supplier_import import load_contacts_from_files, merge_contacts
from .task_queue import get_supplier_search_state, task_queue

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


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()
    if os.getenv("ENABLE_EMBEDDED_QUEUE", "false").lower() == "true":
        task_queue.start()


@app.get("/health")
def healthcheck() -> dict:
    return {"status": "ok"}


@app.post("/auth/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register_user(payload: UserCreate, session=Depends(get_session)) -> User:
    if len(payload.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters long",
        )

    if len(payload.password) > 72:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at most 72 characters long",
        )

    existing = session.exec(select(User).where(User.email == payload.email)).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User already exists")

    hashed = auth.hash_password(payload.password)
    user = User(email=payload.email, password_hash=hashed)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@app.post("/auth/login", response_model=TokenResponse)
def login_user(payload: UserCreate, session=Depends(get_session)) -> TokenResponse:
    user = session.exec(select(User).where(User.email == payload.email)).first()
    if not user or not auth.verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = auth.issue_token(user, session)
    return TokenResponse(token=token.token)


@app.get("/users/me", response_model=UserRead)
def get_me(current_user: User = Depends(auth.get_current_user)) -> User:
    return current_user


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
    task_queue.enqueue_supplier_search_task(purchase.id, purchase.terms_text or "")
    return purchase


@app.get("/purchases", response_model=List[PurchaseRead])
def list_purchases(session=Depends(get_session), current_user: User = Depends(auth.get_current_user)) -> List[Purchase]:
    return session.exec(select(Purchase).where(Purchase.user_id == current_user.id)).all()


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

    purchase.updated_at = datetime.utcnow()
    session.add(purchase)
    session.commit()
    session.refresh(purchase)

    if payload.terms_text is not None and payload.terms_text != original_terms:
        task_queue.enqueue_supplier_search_task(purchase.id, purchase.terms_text or "")
    return purchase


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
    if not state:
        task = task_queue.enqueue_supplier_search_task(
            purchase_id,
            payload.terms_text or purchase.terms_text or "",
            payload.hints,
        )
        return SupplierSearchResponse(
            task_id=task.id or 0,
            status=task.status,
            queries=[],
            note="Поиск поставщиков поставлен в очередь",
            tech_task_excerpt="",
            search_output=[],
            processed_contacts=[],
        )

    if state.status == "completed" and not state.queries:
        plan = build_search_queries(payload.terms_text or purchase.terms_text or "", payload.hints)
        return SupplierSearchResponse(
            task_id=state.task_id,
            status=state.status,
            queries=plan.queries,
            note=plan.note,
            tech_task_excerpt=state.tech_task_excerpt,
            search_output=state.search_output,
            processed_contacts=state.processed_contacts,
        )

    return SupplierSearchResponse(
        task_id=state.task_id,
        status=state.status,
        queries=state.queries,
        note=state.note or "Поиск поставщиков выполняется",
        tech_task_excerpt=state.tech_task_excerpt,
        search_output=state.search_output,
        processed_contacts=state.processed_contacts,
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
