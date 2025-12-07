from dataclasses import dataclass
from typing import List, Optional


@dataclass
class GeneratedSearchPlan:
    queries: List[str]
    note: str


def build_search_queries(terms_text: str, hints: Optional[List[str]] = None) -> GeneratedSearchPlan:
    """Generate lightweight search queries without hitting external LLMs.

    The function creates 2-3 realistic Yandex queries using the provided
    technical assignment text. Hints allow callers to inject extra focus words.
    """

    base = terms_text.strip().split("\n")[0][:80] if terms_text else "закупка оборудования"
    default_keywords = ["поставщик", "опт", "официальный дилер"]
    if hints:
        default_keywords.extend([h for h in hints if h])

    queries = [f"{base} {kw}" for kw in default_keywords][:3]

    return GeneratedSearchPlan(
        queries=queries,
        note=(
            "Лёгкая генерация без обращений к внешнему LLM. "
            "Используйте suppliers_contacts.py для полноценного поиска, если доступен API ключ."
        ),
    )


def generate_email_body(purchase_name: str, terms_text: str, company_name: Optional[str]) -> str:
    header = f"Запрос коммерческого предложения по закупке: {purchase_name}"
    intro = (
        "Добрый день! Мы готовим закупку и хотели бы получить ваше коммерческое предложение. "
        "Просим ответить в ответном письме и указать цены, сроки поставки и условия оплаты."
    )
    spec = terms_text or "Техническое задание не заполнено."
    addressee = company_name or "поставщик"
    closing = (
        "Если требуется дополнительная информация, дайте знать. "
        "Готовы обсудить детали и договориться об условиях."
    )
    return "\n\n".join([header, f"Уважаемый {addressee},", intro, "Техническое задание:", spec, closing])
