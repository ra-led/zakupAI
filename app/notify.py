import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")


def send_lead_notification(name: str, email: str, company: str | None, phone: str | None) -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL]):
        logger.warning("SMTP not configured, skipping lead notification")
        return

    body = (
        f"Новая заявка на пилот zakupAI\n\n"
        f"Имя: {name}\n"
        f"Email: {email}\n"
        f"Компания: {company or '—'}\n"
        f"Телефон: {phone or '—'}\n"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Новая заявка: {name}"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Lead notification sent to %s", NOTIFY_EMAIL)
    except Exception:
        logger.exception("Failed to send lead notification")
