"""Pydantic models for contact search inputs and outputs."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl


class ContactSearchInput(BaseModel):
    """Input for searching supplier contacts based on a product description."""

    product_name: str = Field(..., description="Product name to search for")
    description: str = Field(..., description="Product description or keywords")
    region: int = Field(
        213, description="Yandex region identifier for localized search (default Moscow)"
    )
    page: int = Field(0, description="Page number to request from Yandex search")

    @property
    def query_text(self) -> str:
        """Combine product name and description into a single search query."""

        return f"{self.product_name} {self.description}".strip()


class SearchResult(BaseModel):
    """Represents a parsed search result from Yandex."""

    title: str = Field(..., description="Result title")
    url: HttpUrl = Field(..., description="Destination URL")


class SupplierContact(BaseModel):
    """Contact info discovered on a supplier page."""

    email: str = Field(..., description="Supplier contact email")
    phone: str = Field(..., description="Supplier contact phone number")
    source_url: HttpUrl = Field(..., description="URL where the contact was discovered")
    source_title: Optional[str] = Field(
        None, description="Title of the page where the contact was discovered"
    )
    notes: Optional[str] = Field(None, description="Additional extraction notes")


class ContactSearchResult(BaseModel):
    """Aggregated contact search result with minimal metadata."""

    query: ContactSearchInput
    contacts: List[SupplierContact]
    inspected_urls: List[str] = Field(
        default_factory=list, description="URLs that were inspected for contacts"
    )
    skipped_urls: List[str] = Field(
        default_factory=list, description="URLs skipped as aggregators or invalid"
    )
