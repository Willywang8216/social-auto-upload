"""Durable delivery checkpoints, independent of Flask and provider credentials.

Publishers call checkpoint immediately before a non-idempotent request and after
receiving a receipt. The worker installs the fenced persistence callback. A lost
response is not permission to repeat a publish.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone


class LeaseLost(RuntimeError):
    """Stop all side effects when the attempt no longer owns its target."""


class DeliveryPending(RuntimeError):
    """Provider accepted the operation; poll the stored receipt, do not repost."""


class DeliveryUnknown(RuntimeError):
    """Delivery may have happened but cannot be reconciled automatically."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state(payload: dict) -> dict:
    value = payload.get("_delivery")
    return value if isinstance(value, dict) else {}


def checkpoint(context: dict, **changes) -> dict:
    current = {**state(context), **copy.deepcopy(changes)}
    context["_delivery"] = current
    persist = context.get("_save_checkpoint")
    if callable(persist):
        persist(current)
    return current


def starting(payload: dict, *, operation: str) -> None:
    checkpoint(payload, submission_started=True, operation=operation,
               operation_started_at=utc_now())


def accepted(payload: dict, *, provider: str, receipt_id: str,
             status: str = "processing", **metadata) -> dict:
    prior = receipt_for(payload, provider) or {}
    submitted_at = prior.get("submitted_at") if str(prior.get("id")) == str(receipt_id) else None
    receipt = {"provider": provider, "id": str(receipt_id), "status": status,
               "submitted_at": submitted_at or utc_now(), **metadata}
    return checkpoint(payload, submission_started=True, provider_receipt=receipt)


def receipt_for(payload: dict, provider: str) -> dict | None:
    receipt = state(payload).get("provider_receipt")
    if isinstance(receipt, dict) and receipt.get("provider") == provider and receipt.get("id"):
        return receipt
    return None


def confirmed(payload: dict, *, provider: str, receipt_id: str = "") -> None:
    prior = receipt_for(payload, provider) or {}
    accepted(payload, provider=provider, receipt_id=receipt_id or prior.get("id", "confirmed"),
             status="delivered", delivered_at=utc_now())


def save_payload_progress(payload: dict) -> None:
    progress = {k: copy.deepcopy(payload[k]) for k in ("telegramCompletedByDelivery",)
                if k in payload}
    checkpoint(payload, payload=progress)


def pending_or_unknown(payload: dict, message: str, *, max_age_hours: float = 24,
                       retry_after: str | float | None = None) -> None:
    receipt = state(payload).get("provider_receipt")
    if not isinstance(receipt, dict) or not receipt.get("id"):
        raise DeliveryUnknown(message)
    try:
        started = datetime.fromisoformat(receipt["submitted_at"].replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started).total_seconds()
    except (KeyError, ValueError, TypeError):
        raise DeliveryUnknown(message + "; missing receipt timestamp") from None
    if age > max_age_hours * 3600:
        raise DeliveryUnknown(message + "; reconciliation deadline exceeded")
    error = DeliveryPending(message)
    if retry_after is not None:
        error.details = {"retryAfter": retry_after}
    raise error
