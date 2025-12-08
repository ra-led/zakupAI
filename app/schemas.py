from datetime import datetime
from typing import List, Optional, Literal

from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class UserRead(BaseModel):
    id: int
    email: EmailStr
    created_at: datetime


class TokenResponse(BaseModel):
    token: str


class PurchaseCreate(BaseModel):
    custom_name: Optional[str] = None
    terms_text: Optional[str] = None


class PurchaseUpdate(BaseModel):
    custom_name: Optional[str] = None
    terms_text: Optional[str] = None
    status: Optional[str] = None
    nmck_value: Optional[float] = None
    nmck_currency: Optional[str] = None


class PurchaseRead(BaseModel):
    id: int
    auto_number: int
    full_name: str
    custom_name: Optional[str]
    terms_text: Optional[str]
    status: str
    nmck_value: Optional[float]
    nmck_currency: Optional[str]
    created_at: datetime
    updated_at: datetime


class SupplierCreate(BaseModel):
    company_name: Optional[str] = None
    website_url: Optional[str] = None
    relevance_score: Optional[float] = None


class SupplierRead(BaseModel):
    id: int
    company_name: Optional[str]
    website_url: Optional[str]
    relevance_score: Optional[float]
    created_at: datetime


class SupplierContactCreate(BaseModel):
    email: EmailStr
    source_url: Optional[str] = None
    is_selected_for_request: bool = False


class SupplierContactRead(BaseModel):
    id: int
    email: EmailStr
    source_url: Optional[str]
    is_selected_for_request: bool
    created_at: datetime


class EmailAccountCreate(BaseModel):
    email: EmailStr
    imap_host: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    login: Optional[str] = None
    password: Optional[str] = None


class EmailAccountRead(BaseModel):
    id: int
    email: EmailStr
    imap_host: Optional[str]
    smtp_host: Optional[str]
    smtp_port: Optional[int]
    login: Optional[str]
    created_at: datetime


class EmailMessageCreate(BaseModel):
    supplier_contact_id: Optional[int] = None
    subject: str
    body: str
    price_value: Optional[float] = None
    currency: Optional[str] = None
    direction: Literal["outgoing", "incoming"]


class EmailMessageRead(BaseModel):
    id: int
    supplier_contact_id: Optional[int]
    subject: str
    body: str
    price_value: Optional[float]
    currency: Optional[str]
    direction: str
    created_at: datetime


class LLMTaskCreate(BaseModel):
    task_type: str
    input_text: str


class LLMTaskRead(BaseModel):
    id: int
    task_type: str
    input_text: str
    output_text: Optional[str]
    status: str
    created_at: datetime


class EmailDraftResponse(BaseModel):
    subject: str
    body: str


class SupplierSearchRequest(BaseModel):
    terms_text: Optional[str] = None
    hints: Optional[List[str]] = None


class SupplierSearchResponse(BaseModel):
    task_id: int
    status: str
    queries: List[str]
    note: str
    tech_task_excerpt: Optional[str] = None
    search_output: List["SearchOutputEntry"] = Field(default_factory=list)
    processed_contacts: List["ProcessedContact"] = Field(default_factory=list)


class ProcessedContact(BaseModel):
    website: str
    is_relevant: bool = True
    reason: Optional[str] = None
    name: Optional[str] = None
    emails: List[str] = Field(default_factory=list)


class SearchOutputEntry(BaseModel):
    website: str
    emails: List[str] = Field(default_factory=list)


class SupplierImportRequest(BaseModel):
    processed_contacts: Optional[List[ProcessedContact]] = None
    search_output: Optional[List[SearchOutputEntry]] = None
    processed_contacts_path: Optional[str] = Field(
        default="processed_contacts.json",
        description="Path to suppliers_contacts.py processed_contacts output",
    )
    search_output_path: Optional[str] = Field(
        default="search_output.json",
        description="Path to suppliers_contacts.py search_output output",
    )


class SupplierImportResult(BaseModel):
    suppliers_created: int
    suppliers_matched: int
    contacts_created: int
