"""OpenRouter LLM client — используем openai SDK с base_url OpenRouter.
Модель задаётся через OPENROUTER_MODEL в .env, можно менять без перезапуска.
"""
import os
import json
from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            default_headers={
                "HTTP-Referer": "https://zakupai.app",
                "X-Title": "ZakupAI",
            },
        )
    return _client


async def chat(messages: list[dict], *, model: str | None = None, json_mode: bool = False) -> str:
    """Low-level chat call. Returns raw string content."""
    kwargs: dict = {
        "model": model or os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-001"),
        "messages": messages,
        "temperature": 0,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = await get_client().chat.completions.create(**kwargs)
    return response.choices[0].message.content


async def extract_items_from_text(raw_text: str) -> list[dict]:
    """
    Извлекает товарные позиции из произвольного текста файла поставщика.
    Возвращает список dict: name, registry_number, okpd2_code, quantity, characteristics.
    """
    prompt = f"""Ты — парсер файлов заявок поставщиков для закупок по 44-ФЗ/223-ФЗ.

Из текста ниже извлеки все товарные позиции. Для каждой позиции верни JSON-объект:
- name: наименование товара (строка)
- registry_number: реестровый номер по 719 ПП (строка типа «РПП-12345678» или просто цифры; null если нет)
- okpd2_code: код ОКПД 2 (формат XX.XX.XX.XXX; null если нет)
- quantity: количество (число или строка; null если нет)
- characteristics: массив {{name: "...", value: "..."}}

Верни JSON-объект с полем "items": массив позиций. Только JSON.

Текст:
{raw_text[:12000]}"""

    content = await chat([{"role": "user", "content": prompt}], json_mode=True)
    parsed = json.loads(content)
    if isinstance(parsed, list):
        return parsed
    for key in ("items", "products", "товары", "позиции"):
        if key in parsed:
            return parsed[key]
    # fallback: first list value
    for v in parsed.values():
        if isinstance(v, list):
            return v
    return []


async def compare_characteristics(
    supplier_chars: list[dict],
    gisp_chars: list[dict],
    product_name: str,
) -> list[dict]:
    """
    Сравнивает характеристики поставщика с ГИСП.
    Статусы: ok | mismatch | wording | missing_in_gisp
    """
    prompt = f"""Сравни характеристики товара «{product_name}» из заявки поставщика с данными ГИСП.

Поставщик:
{json.dumps(supplier_chars, ensure_ascii=False, indent=2)}

ГИСП:
{json.dumps(gisp_chars, ensure_ascii=False, indent=2)}

Для каждой характеристики поставщика верни:
- name: название (из заявки)
- supplier_value: значение поставщика
- gisp_value: значение из ГИСП (null если нет)
- status: "ok" | "mismatch" | "wording" | "missing_in_gisp"
  * ok — совпадают или эквивалентны
  * mismatch — отличаются, несовместимы
  * wording — эквивалентны, но записаны по-разному (единицы, синонимы)
  * missing_in_gisp — в ГИСП нет этой характеристики
- comment: пояснение (только если status != "ok")

Верни JSON: {{"comparison": [...]}}. Только JSON."""

    content = await chat([{"role": "user", "content": prompt}], json_mode=True)
    parsed = json.loads(content)
    return parsed.get("comparison", [])
