from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field


class LotParameter(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1)
    value: str = Field(...)
    units: str = Field(...)


class LotItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1)
    units: str = Field(..., min_length=1)
    count: str = Field(...)
    parameters: list[LotParameter] = Field(...)


class LotsExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lots: list[LotItem] = Field(...)


class BidLotItem(LotItem):
    price: str = Field(...)


class BidLotsExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lots: list[BidLotItem] = Field(...)


class KpItem(BaseModel):
    """Unified КП item — superset of BidLotItem and the M4 raw_item dict.

    All fields are required-but-may-be-empty. The schema is locked with
    extra="forbid" so the LLM can't sneak in arbitrary keys, and required
    via Field(...) so json_schema enforces every key. Empty string is the
    "no value" sentinel — use it instead of null to keep the schema simple.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1)
    units: str = Field(...)
    count: str = Field(...)
    price: str = Field(...)
    registry_number: str = Field(...)
    okpd2_code: str = Field(...)
    parameters: list[LotParameter] = Field(...)


class KpExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lots: list[KpItem] = Field(...)


def _render_prompt(template_filename: str, terms_text: str) -> str:
    base_dir = Path(__file__).resolve().parent
    prompts_dir = base_dir / "prompts"

    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_filename)
    return template.render(terms_text=terms_text or "")


def build_lots_prompt_and_schema(terms_text: str) -> Tuple[str, Dict[str, Any]]:
    prompt = _render_prompt("lots_extraction_prompt.j2", terms_text)
    schema: Dict[str, Any] = {
        "name": "lots_extraction_result",
        "strict": True,
        "schema": LotsExtractionResult.model_json_schema(),
    }
    return prompt, schema


def build_bid_lots_prompt_and_schema(terms_text: str) -> Tuple[str, Dict[str, Any]]:
    prompt = _render_prompt("bid_lots_extraction_prompt.j2", terms_text)
    schema: Dict[str, Any] = {
        "name": "bid_lots_extraction_result",
        "strict": True,
        "schema": BidLotsExtractionResult.model_json_schema(),
    }
    return prompt, schema


def build_kp_prompt_and_schema(terms_text: str) -> Tuple[str, Dict[str, Any]]:
    """Unified KP parser prompt + schema.

    Used by both M2/M3 (extract_bid_lots → BidLot) and M4 (extract_items_from_text
    → RegimeCheckItem). The schema is a superset: every consumer takes only the
    fields it needs.
    """
    prompt = _render_prompt("kp_extraction_prompt.j2", terms_text)
    schema: Dict[str, Any] = {
        "name": "kp_extraction_result",
        "strict": True,
        "schema": KpExtractionResult.model_json_schema(),
    }
    return prompt, schema
