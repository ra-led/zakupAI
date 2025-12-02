"""Client and parsing helpers for Yandex Search API."""

from __future__ import annotations

import base64
from typing import Iterable, List
from urllib.parse import urljoin

import requests
from lxml import html
from pydantic import BaseModel, Field

from .models import ContactSearchInput, SearchResult

YANDEX_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
DEFAULT_USER_AGENT = "zakupai-contact-finder/0.1"


class YandexSearchRequest(BaseModel):
    """Request payload for Yandex Search API."""

    query: dict = Field(..., description="Search query body as expected by the API")
    sortSpec: dict | None = Field(None, description="Sort specification")
    groupSpec: dict | None = Field(None, description="Grouping options")
    maxPassages: int | None = Field(None, description="Maximum number of passages")
    region: int | None = Field(None, description="Region identifier")
    l10N: str | None = Field(None, description="Localization code")
    folderId: str | None = Field(None, description="Yandex Cloud folder ID")
    responseFormat: str | None = Field(None, description="Response format")
    userAgent: str | None = Field(None, description="User agent header")


class YandexSearchClient:
    """Client for retrieving web results from Yandex Search API."""

    def __init__(
        self,
        iam_token: str,
        folder_id: str,
        *,
        session: requests.Session | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.iam_token = iam_token
        self.folder_id = folder_id
        self.session = session or requests.Session()
        self.user_agent = user_agent

    def build_request(self, query: ContactSearchInput) -> YandexSearchRequest:
        """Create a YandexSearchRequest payload from the query input."""

        body = {
            "query": {
                "searchType": "general",
                "queryText": query.query_text,
                "familyMode": "none",
                "page": query.page,
                "fixTypoMode": "default",
            },
            "sortSpec": {
                "sortMode": "byRelevance",
                "sortOrder": "desc",
            },
            "groupSpec": {
                "groupMode": "flat",
                "groupsOnPage": 10,
                "docsInGroup": 1,
            },
            "maxPassages": 0,
            "region": query.region,
            "l10N": "en",
            "folderId": self.folder_id,
            "responseFormat": "html",
            "userAgent": self.user_agent,
        }
        return YandexSearchRequest(**body)

    def search(self, query: ContactSearchInput) -> List[SearchResult]:
        """Execute search query and parse HTML results into SearchResult list."""

        request_body = self.build_request(query)
        response = self.session.post(
            YANDEX_SEARCH_URL,
            headers={"Authorization": f"Bearer {self.iam_token}"},
            data=request_body.model_dump_json(),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        raw_data = payload.get("rawData")
        if not raw_data:
            raise ValueError("Yandex search response is missing 'rawData'")

        decoded_html = base64.b64decode(raw_data)
        document = html.fromstring(decoded_html)
        return list(self._parse_results(document))

    def _parse_results(self, document: html.HtmlElement) -> Iterable[SearchResult]:
        """Parse Yandex search HTML page to extract result links."""

        link_nodes = document.xpath('//*[@id="search-result"]//a[@href]')
        if not link_nodes:
            link_nodes = document.xpath('//li//a[@href]')

        seen_urls: set[str] = set()
        for node in link_nodes:
            href = node.get("href") or ""
            if not href or href.startswith("#"):
                continue
            absolute_url = urljoin("https://yandex.ru", href)
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            title = (node.text_content() or "").strip() or absolute_url
            yield SearchResult(title=title, url=absolute_url)
