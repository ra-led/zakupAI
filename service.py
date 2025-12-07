from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from suppliers_contacts import (
    build_validation_tz,
    company_validation,
    doc_validation,
    extract_emails_from_html,
    summarize_tz_for_single_supplier,
)


class SummarizeRequest(BaseModel):
    tz_text: str = Field(..., description="Raw technical specification text")


class SummarizeResponse(BaseModel):
    item: str
    product_groups: List[Dict[str, Any]]
    search_queries: List[str]
    validation_tz: str


class SearchResult(BaseModel):
    link: str
    title: str
    text: str


class ValidateSearchRequest(BaseModel):
    validation_tz: str = Field(..., description="Compact TZ for validation tasks")
    result: SearchResult


class ValidateSearchResponse(BaseModel):
    is_relevant: bool
    reason: str


class ValidateCompanyRequest(BaseModel):
    validation_tz: str
    website: str
    main_page_content: Optional[str] = Field(None, description="HTML or text from main page")
    about_page_content: Optional[str] = Field(None, description="HTML or text from about page")
    catalog_page_content: Optional[str] = Field(None, description="HTML or text from catalog page")


class ValidateCompanyResponse(BaseModel):
    is_relevant: bool
    reason: str
    name: Optional[str]
    website: str


class ExtractEmailsRequest(BaseModel):
    html: str


class ExtractEmailsResponse(BaseModel):
    emails: List[str]


app = FastAPI(title="Suppliers contacts service")


@app.post("/summaries", response_model=SummarizeResponse)
def summarize_tz(payload: SummarizeRequest) -> SummarizeResponse:
    try:
        summary = summarize_tz_for_single_supplier(payload.tz_text)
        validation_tz = build_validation_tz(summary)
        return SummarizeResponse(
            item=summary.get("item", ""),
            product_groups=summary.get("product_groups", []),
            search_queries=summary.get("search_queries", []),
            validation_tz=validation_tz,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/validate/search-result", response_model=ValidateSearchResponse)
def validate_search_result(payload: ValidateSearchRequest) -> ValidateSearchResponse:
    try:
        is_relevant, reason = doc_validation(
            payload.validation_tz,
            doc=payload.result.model_dump(),
        )
        return ValidateSearchResponse(is_relevant=is_relevant, reason=reason)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/validate/company", response_model=ValidateCompanyResponse)
def validate_company(payload: ValidateCompanyRequest) -> ValidateCompanyResponse:
    try:
        result = company_validation(
            payload.validation_tz,
            website=payload.website,
            main_page_content=payload.main_page_content,
            about_page_content=payload.about_page_content,
            catalog_page_content=payload.catalog_page_content,
        )
        return ValidateCompanyResponse(
            is_relevant=result.get("is_relevant", False),
            reason=result.get("reason", ""),
            name=result.get("name"),
            website=payload.website,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/emails/from-html", response_model=ExtractEmailsResponse)
def extract_emails(payload: ExtractEmailsRequest) -> ExtractEmailsResponse:
    try:
        emails = extract_emails_from_html(payload.html)
        return ExtractEmailsResponse(emails=emails)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("service:app", host="0.0.0.0", port=8000, reload=False)
