import json
import os
from typing import Any, Dict, List

from openai import OpenAI


LOTS_SCHEMA: Dict[str, Any] = {
    "name": "lots_extraction",
    "schema": {
        "type": "object",
        "properties": {
            "lots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "parameters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "value": {"type": "string"},
                                    "units": {"type": "string"},
                                },
                                "required": ["name", "value", "units"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["name", "parameters"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["lots"],
        "additionalProperties": False,
    },
    "strict": True,
}


def _build_lots_prompt(terms_text: str) -> List[Dict[str, str]]:
    system_message = (
        "Вы извлекаете лоты из технического задания. "
        "Верните только JSON по схеме. "
        "Всегда возвращайте массив lots. "
        "Если нет лотов, верните {\"lots\":[]}. "
        "Если у параметра нет количественного значения, value=\"compliance\" и units=\"\". "
        "Если единицы не указаны, units=\"\"."
    )
    user_message = f"Техническое задание (markdown):\n{terms_text}"
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


def extract_lots(terms_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    base_url = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url)
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    response = client.chat.completions.create(
        model=model,
        messages=_build_lots_prompt(terms_text),
        response_format={"type": "json_schema", "json_schema": LOTS_SCHEMA},
        temperature=0.2,
        max_tokens=2000,
    )

    output_text = response.choices[0].message.content if response.choices else None
    if not output_text:
        raise RuntimeError("Empty response from OpenAI")

    return json.loads(output_text)
