from typing import Optional


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
