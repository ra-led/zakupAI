from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SessionToken(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Purchase(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    auto_number: int
    custom_name: Optional[str] = None
    full_name: str
    terms_text: Optional[str] = None
    status: str = Field(default="draft")
    nmck_value: Optional[float] = None
    nmck_currency: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Supplier(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchase.id")
    company_name: Optional[str] = None
    website_url: Optional[str] = None
    relevance_score: Optional[float] = None
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SupplierContact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    supplier_id: int = Field(foreign_key="supplier.id")
    email: str
    source_url: Optional[str] = None
    is_selected_for_request: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EmailAccount(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    email: str
    imap_host: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    login: Optional[str] = None
    password_enc: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EmailMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchase.id")
    supplier_contact_id: Optional[int] = Field(default=None, foreign_key="suppliercontact.id")
    direction: str = Field(description="outgoing or incoming")
    subject: str
    body: str
    price_value: Optional[float] = None
    currency: Optional[str] = None
    raw_payload: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LLMTask(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: Optional[int] = Field(default=None, foreign_key="purchase.id")
    task_type: str
    input_text: str
    output_text: Optional[str] = None
    status: str = Field(default="queued")
    created_at: datetime = Field(default_factory=datetime.utcnow)
