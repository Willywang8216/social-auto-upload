"""Best-effort operator alerting for background maintenance.

When the token-maintenance loop gives up on an account (repeated refresh
failures → flagged for manual reconnect) we want the operator to hear about it
instead of discovering it days later when a publish fails. This module sends a
short notice to whatever channel is configured via environment variables.

Design rules:
  * Every send is best-effort. Any failure is swallowed and logged so alerting
    can never break the maintenance loop.
  * If nothing is configured the functions no-op (the caller's own logging
    remains the source of truth). This keeps the feature zero-config-safe.
  * All configured channels are attempted, so you can wire up more than one.

Supported channels (all optional):
  * Telegram   — SAU_ALERT_TELEGRAM_BOT_TOKEN + SAU_ALERT_TELEGRAM_CHAT_ID
                 (chat id may be a comma/;-separated list)
  * Webhook    — SAU_ALERT_WEBHOOK_URL (JSON POST {"subject","body"})
  * SMTP email — SAU_ALERT_SMTP_HOST (+ _PORT/_USER/_PASSWORD/_FROM/_TO/_TLS)
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

try:  # reuse the app's structured logger when available
    from utils.log import worker_logger as _logger
except Exception:  # pragma: no cover - fallback for isolated imports
    import logging

    _logger = logging.getLogger(__name__)

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    requests = None


def _env(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _split_recipients(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def _send_telegram(subject: str, body: str) -> bool:
    token = _env("SAU_ALERT_TELEGRAM_BOT_TOKEN")
    chat = _env("SAU_ALERT_TELEGRAM_CHAT_ID")
    if not token or not chat or requests is None:
        return False
    text = f"*{subject}*\n{body}"
    sent = False
    for chat_id in _split_recipients(chat):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        resp.raise_for_status()
        sent = True
    return sent


def _send_webhook(subject: str, body: str) -> bool:
    url = _env("SAU_ALERT_WEBHOOK_URL")
    if not url or requests is None:
        return False
    resp = requests.post(url, json={"subject": subject, "body": body}, timeout=30)
    resp.raise_for_status()
    return True


def _send_smtp(subject: str, body: str) -> bool:
    host = _env("SAU_ALERT_SMTP_HOST")
    if not host:
        return False
    port = int(_env("SAU_ALERT_SMTP_PORT") or "587")
    user = _env("SAU_ALERT_SMTP_USER")
    password = _env("SAU_ALERT_SMTP_PASSWORD")
    sender = _env("SAU_ALERT_SMTP_FROM") or user
    recipients = _split_recipients(_env("SAU_ALERT_SMTP_TO"))
    if not sender or not recipients:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    use_tls = (_env("SAU_ALERT_SMTP_TLS") or "1").lower() not in ("0", "false", "no")
    with smtplib.SMTP(host, port, timeout=30) as server:
        if use_tls:
            server.starttls(context=ssl.create_default_context())
        if user and password:
            server.login(user, password)
        server.send_message(msg)
    return True


def send_ops_alert(*, subject: str, body: str) -> bool:
    """Fire the alert on every configured channel. Returns True if at least one
    channel accepted it. Never raises."""
    sent = False
    for channel in (_send_telegram, _send_webhook, _send_smtp):
        try:
            sent = channel(subject, body) or sent
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            _logger.warning(f"ops_alerts: {channel.__name__} failed: {exc!r}")
    if not sent:
        _logger.info(f"ops_alerts: no channel configured; alert not delivered: {subject}")
    return sent
