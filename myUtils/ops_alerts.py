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

import html
import os
import smtplib
import ssl
import sys
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


def public_app_origin(explicit: str | None = None) -> str:
    """The public origin operator-facing links should point at, or ``""``.

    ``SAU_PUBLIC_APP_URL`` is honoured as an explicit override (the digest CLI's
    ``--app-url``), falling back to ``SAU_PUBLIC_BASE_URL`` — the variable the
    app already keeps its own origin in (``sau_app.config`` reads it, and the
    Google-login check requires it). Alert and digest links therefore share one
    source of truth instead of needing a second variable that can drift out of
    step with the real URL.

    Never invents a hostname: an unconfigured box yields ``""`` and callers omit
    the link, because a guessed domain reads as a working link to an operator
    and quietly sends them nowhere.
    """
    if explicit is not None:
        return str(explicit).strip().rstrip("/")
    for name in ("SAU_PUBLIC_APP_URL", "SAU_PUBLIC_BASE_URL"):
        value = _env(name)
        if value:
            return value.rstrip("/")
    return ""


# sendMessage rejects payloads over 4096 characters, so the limit is enforced on
# our side, with room left for the subject header and the part counter.
_TELEGRAM_TEXT_LIMIT = 3900


def _split_message(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """UTF-16-bounded chunks; keep complete Unicode graphemes where possible."""
    import regex
    from myUtils.text_limits import utf16_length

    if limit < 2:
        raise ValueError("Notification text budget must be at least two units")
    if utf16_length(text) <= limit:
        return [text]
    parts, current, size = [], [], 0
    for match in regex.finditer(r"\X", text):
        cluster = match.group()
        units = utf16_length(cluster)
        if units > limit:
            # An enormous combining sequence is still valid text. Hard-split
            # it at code-point boundaries rather than lose the entire notice.
            chunks, chunk, used = [], [], 0
            for char in cluster:
                width = utf16_length(char)
                if used + width > limit:
                    chunks.append("".join(chunk)); chunk, used = [], 0
                chunk.append(char); used += width
            chunks.append("".join(chunk))
        else:
            chunks = [cluster]
        for chunk in chunks:
            width = utf16_length(chunk)
            if current and size + width > limit:
                parts.append("".join(current)); current, size = [], 0
            current.append(chunk); size += width
    if current:
        parts.append("".join(current))
    return parts


def _telegram_payloads(subject: str, body: str) -> list[tuple[str, str]]:
    """(escaped HTML, plain) chunks that fit after Telegram entity parsing."""
    from myUtils.text_limits import utf16_length

    budget = min(_TELEGRAM_TEXT_LIMIT, 4096 - utf16_length(subject) - 40)
    chunks = _split_message(body, limit=budget)
    total = len(chunks)
    payloads: list[tuple[str, str]] = []
    for index, chunk in enumerate(chunks, start=1):
        header = subject if index == 1 else f"{subject} (cont.)"
        counter = f"\n\n[{index}/{total}]" if total > 1 else ""
        payloads.append((f"<b>{html.escape(header)}</b>\n{html.escape(chunk)}{counter}",
                         f"{header}\n{chunk}{counter}"))
    return payloads


def _is_under_test() -> bool:
    """Whether this process is a test run, and must never alert a human.

    The suite loads the repo .env, so tests that exercise the failure path can
    pick up the LIVE bot token and deliver fixtures:

        [SAU] Publish failed: bluesky target #1 (job #1) ... bluesky said no

    Job #1 and accounts like acct-1 / fb-bluesky do not exist in the database -
    they are literals in the test files. A fixture on the operator's phone is
    indistinguishable from a production incident, which is the exact alarm
    fatigue alerting exists to prevent.

    ``tests/conftest.py`` blanks the credentials, but a conftest is only loaded
    when pytest collects from that directory - a test invoked directly, or a
    script that imports a test module, misses it. This second guard lives in the
    sender, so it holds no matter how the code was reached. It keys on pytest's
    own marker (set whenever pytest is the running process) plus the explicit
    ``SAU_ALERTS_DISABLED`` switch.
    """
    if str(os.environ.get("SAU_ALERTS_DISABLED", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return True
    # SAU_ALERTS_ALLOW_UNDER_TEST lets a test exercise the real transport (with a
    # mocked HTTP layer) while every other test stays blocked.
    if str(os.environ.get("SAU_ALERTS_ALLOW_UNDER_TEST", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _send_telegram(subject: str, body: str) -> bool:
    if _is_under_test():
        _logger.info(
            "ops_alerts: running under test; alert not delivered: %s", subject
        )
        return False
    token = _env("SAU_ALERT_TELEGRAM_BOT_TOKEN")
    chat = _env("SAU_ALERT_TELEGRAM_CHAT_ID")
    if not token or not chat or requests is None:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Operator alerts routinely contain Markdown-special characters: the "[SAU]"
    # subject tag ("[" starts Markdown link syntax), account names with "_", and
    # exception reprs. Legacy Markdown 400s on an unbalanced "_", "*" or "[",
    # which would silently drop exactly the reconnect alerts this channel exists
    # to deliver. Send HTML with escaped content (only < > & are special, and we
    # escape them) and fall back to unformatted text if the API still rejects
    # the payload — delivery matters more than the bold subject.
    sent = False
    for chat_id in _split_recipients(chat):
        for html_text, plain_text in _telegram_payloads(subject, body):
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": html_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if resp.status_code == 400:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": plain_text,
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
    if _is_under_test():
        return False  # Applies to Telegram, webhook and SMTP alike.
    sent = False
    for channel in (_send_telegram, _send_webhook, _send_smtp):
        try:
            sent = channel(subject, body) or sent
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            from myUtils.secret_redaction import redact_text
            _logger.warning(f"ops_alerts: {channel.__name__} failed: {redact_text(repr(exc))}")
    if not sent:
        _logger.info(f"ops_alerts: no channel configured; alert not delivered: {subject}")
    return sent
