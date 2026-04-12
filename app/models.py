from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    full_name: Optional[str] = None
    organization: Optional[str] = None
    is_admin: bool = Field(default=False)
    is_active: bool = Field(default=True)
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
    is_archived: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PurchaseFile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchase.id")
    filename: str
    file_type: str  # "tz", "kp", "regime_kp"
    created_at: datetime = Field(default_factory=datetime.utcnow)


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
    source: Optional[str] = None
    confidence: Optional[float] = None
    dedup_key: Optional[str] = None
    reason: Optional[str] = None
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
    bid_id: Optional[int] = Field(default=None, foreign_key="bid.id")
    task_type: str
    input_text: str
    output_text: Optional[str] = None
    status: str = Field(default="queued")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = Field(default_factory=datetime.utcnow)


class LLMUsage(SQLModel, table=True):
    """One row per outbound LLM/search API call.

    We never hardcode prices: prompt_tokens / completion_tokens / total_tokens
    are taken from the API response, and cost_usd is recorded only when the
    provider itself returns a cost (OpenRouter does via usage.cost). For
    Yandex/Perplexity-without-cost we still track tokens or just the request.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: Optional[int] = Field(default=None, foreign_key="purchase.id", index=True)
    task_id: Optional[int] = Field(default=None, foreign_key="llmtask.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    channel: str = Field(index=True)  # openrouter | perplexity | yandex
    model: Optional[str] = None
    operation: str = Field(index=True)  # lots_extraction | search_queries | crawl_validation | ...
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None  # only set if provider returned it
    request_count: int = Field(default=1)  # for per-request billing (Yandex)
    success: bool = Field(default=True)
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Bid(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchase.id")
    supplier_id: Optional[int] = Field(default=None, foreign_key="supplier.id")
    supplier_name: Optional[str] = None
    supplier_contact: Optional[str] = None
    bid_text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BidLot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bid_id: int = Field(foreign_key="bid.id")
    name: str
    price: Optional[str] = None
    # Извлечённые из КП реквизиты товара. Используются в M4 Нацрежим path 2:
    # `POST /regime/purchases/{id}/check/from-bid/{bid_id}` берёт BidLot и
    # формирует RegimeCheckItem без повторной загрузки файла. Оба поля
    # необязательные — у многих КП таких данных нет.
    registry_number: Optional[str] = Field(default=None, index=True)
    okpd2_code: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BidLotParameter(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bid_lot_id: int = Field(foreign_key="bidlot.id")
    name: str
    value: str
    units: str


class Lot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchase.id")
    name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LotParameter(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    lot_id: int = Field(foreign_key="lot.id")
    name: str
    value: str
    units: str


# --- National regime checker models ---


class RegimeCheck(SQLModel, table=True):
    __tablename__ = "regimecheck"

    id: Optional[int] = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchase.id")
    user_id: int = Field(foreign_key="user.id")
    file_path: Optional[str] = None
    filename: Optional[str] = None
    status: str = Field(default="pending")
    ok_count: Optional[int] = None
    warning_count: Optional[int] = None
    error_count: Optional[int] = None
    not_found_count: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RegimeCheckItem(SQLModel, table=True):
    __tablename__ = "regimecheckitem"

    id: Optional[int] = Field(default=None, primary_key=True)
    check_id: int = Field(foreign_key="regimecheck.id")
    product_name: Optional[str] = None
    registry_number: Optional[str] = None
    okpd2_code: Optional[str] = None
    supplier_characteristics: Optional[str] = None  # JSON string

    # Registry 719 PP check
    registry_status: Optional[str] = None
    registry_actual: Optional[bool] = None
    registry_cert_end_date: Optional[str] = None
    registry_raw_url: Optional[str] = None

    # Localization score check (PP 1875)
    localization_status: Optional[str] = None
    localization_actual_score: Optional[float] = None
    localization_required_score: Optional[float] = None

    # GISP characteristics check
    gisp_status: Optional[str] = None
    gisp_characteristics: Optional[str] = None  # JSON string
    gisp_comparison: Optional[str] = None  # JSON string
    gisp_url: Optional[str] = None

    # Overall status
    overall_status: str = Field(default="pending")


class Lead(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: str
    company: Optional[str] = None
    phone: Optional[str] = None
    status: str = Field(default="new")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RegistryProduct(SQLModel, table=True):
    """Product from PP 719 registry (opendata CSV)."""
    __tablename__ = "registryproduct"

    id: Optional[int] = Field(default=None, primary_key=True)
    registry_number: Optional[str] = Field(default=None, index=True)
    org_name: Optional[str] = None
    inn: Optional[str] = Field(default=None, index=True)
    ogrn: Optional[str] = None
    product_name: Optional[str] = None
    okpd2: Optional[str] = Field(default=None, index=True)
    tnved: Optional[str] = None
    doc_date: Optional[str] = None
    doc_valid_till: Optional[str] = None
    end_date: Optional[str] = None
    score: Optional[float] = None
    percentage: Optional[float] = None
    score_desc: Optional[str] = None
    reg_number_pp: Optional[str] = None
    doc_name: Optional[str] = None
    doc_num: Optional[str] = None
    mpt_dep: Optional[str] = None
    res_doc_num: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
