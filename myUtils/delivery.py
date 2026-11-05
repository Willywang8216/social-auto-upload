"""Durable delivery checkpoints, independent of Flask and provider credentials.

Publishers call checkpoint immediately before a non-idempotent request and after
receiving a receipt. The worker installs the fenced persistence callback. A lost
response is not permission to repeat a publish.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from uuid import uuid4


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
               operation_started_at=utc_now(), operation_generation=uuid4().hex)


def accepted(payload: dict, *, provider: str, receipt_id: str,
             status: str = "processing", **metadata) -> dict:
    prior = receipt_for(payload, provider) or {}
    if str(prior.get("id")) != str(receipt_id):
        prior = {}
    receipt = {**prior, "provider": provider, "id": str(receipt_id), "status": status,
               "submitted_at": prior.get("submitted_at") or utc_now(), **metadata}
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
    progress = copy.deepcopy(state(payload).get("payload") or {})
    progress.update({k: copy.deepcopy(payload[k]) for k in
                     ("telegramCompletedByDelivery", "artifacts", "preparedPart", "preparedPost", "_preserveApprovedText")
                     if k in payload})
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


def has_unresolved_submission(payload: dict) -> bool:
    """Sticky submission history is resolved only by a scoped rejection proof."""
    current = payload.get("_delivery", {})
    if not isinstance(current, dict) or current.get("checkpoint_invalid"):
        return True
    if (current.get("provider_receipt") or current.get("completed_operations")
            or current.get("provider_operations") or current.get("telegramCompletedByDelivery")):
        return True
    started = current.get("submission_started")
    if started is None or started is False:
        return False
    if started is not True:
        return True
    proof = current.get("definitive_non_delivery")
    generation = current.get("operation_generation")
    return not (isinstance(proof, dict) and isinstance(generation, str) and generation
                and isinstance(proof.get("provider"), str) and proof["provider"] in {"native", "sociamonials"}
                and isinstance(current.get("operation"), str) and current["operation"]
                and proof.get("operation") == current["operation"]
                and proof.get("operation_generation") == generation and proof.get("confirmed_at"))


class RouteExhausted(RuntimeError):
    """A provider's finite submission budget is consumed; polling is separate."""

    retryable = False

    def __init__(self, provider: str):
        super().__init__(f"{provider} submission budget exhausted")
        self.details = {"provider": provider, "retrySafe": True}


def publish_routes(payload: dict) -> dict:
    current = payload.get("_delivery", {})
    if not isinstance(current, dict) or current.get("checkpoint_invalid"):
        raise DeliveryUnknown("Invalid persisted delivery checkpoint")
    if "submission_started" in current and type(current["submission_started"]) is not bool:
        raise DeliveryUnknown("Invalid persisted submission evidence")
    if "payload" in current and not isinstance(current["payload"], dict):
        raise DeliveryUnknown("Invalid persisted payload progress")
    receipt = current.get("provider_receipt", {})
    if not isinstance(receipt, dict):
        raise DeliveryUnknown("Invalid persisted provider receipt")
    if receipt and (not isinstance(receipt.get("provider"), str) or not receipt["provider"].strip()
                    or isinstance(receipt.get("id"), bool) or not isinstance(receipt.get("id"), (str, int))
                    or not str(receipt["id"]).strip()):
        raise DeliveryUnknown("Incomplete persisted provider receipt; reconcile before publishing")
    value = current.get("publish_routes")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DeliveryUnknown("Invalid persisted publishing route state")
    for provider in ("native", "sociamonials"):
        if provider in value and not isinstance(value[provider], dict):
            raise DeliveryUnknown("Invalid persisted provider attempt state")
    return copy.deepcopy(value)


def active_route(payload: dict) -> str:
    value = publish_routes(payload).get("active", "native")
    if not isinstance(value, str) or value not in {"native", "sociamonials"}:
        raise DeliveryUnknown("Unknown persisted publishing provider")
    return value


def begin_route_attempt(payload: dict, provider: str, *, max_attempts: int) -> int:
    """Persist the chosen provider and increment ONLY its submission budget."""
    if provider not in {"native", "sociamonials"} or type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("A supported route and positive finite attempt budget are required")
    routes = publish_routes(payload)
    previous = routes.get(provider) or {}
    if not isinstance(previous, dict):
        raise DeliveryUnknown("Invalid persisted provider attempt state")
    attempts = previous.get("attempts", 0)
    if type(attempts) is not int or attempts < 0:
        raise DeliveryUnknown("Invalid persisted provider attempt count")
    stored_budget = previous.get("max_attempts", max_attempts)
    if type(stored_budget) is not int or stored_budget < 1:
        raise DeliveryUnknown("Invalid persisted provider maximum attempts")
    max_attempts = min(max_attempts, stored_budget)
    if attempts >= max_attempts:
        raise RouteExhausted(provider)
    attempts += 1
    routes["active"] = provider
    routes[provider] = {**previous, "attempts": attempts, "max_attempts": max_attempts,
                        "outcome": "attempting", "started_at": utc_now()}
    checkpoint(payload, publish_routes=routes)
    return attempts


def route_outcome(payload: dict, provider: str, outcome: str, error: str = "") -> None:
    from myUtils.secret_redaction import redact_text

    routes = publish_routes(payload)
    prior = routes.get(provider) or {}
    if not isinstance(prior, dict):
        raise DeliveryUnknown("Invalid persisted provider outcome state")
    routes[provider] = {**prior, "outcome": str(outcome)[:64], "error": redact_text(error)[:1800],
                        "updated_at": utc_now()}
    checkpoint(payload, publish_routes=routes)


def select_backup(payload: dict) -> None:
    """Pin subsequent retries to backup; never erase native submission evidence."""
    routes = publish_routes(payload)
    routes["active"] = "sociamonials"
    checkpoint(payload, publish_routes=routes)


def record_non_delivery(payload: dict, *, provider: str, reason: str) -> None:
    """Retain evidence for a definitely rejected operation, not a lost response."""
    from myUtils.secret_redaction import redact_text

    checkpoint(payload, definitive_non_delivery={"provider": provider,
               "operation": state(payload).get("operation"), "confirmed_at": utc_now(),
               "operation_generation": state(payload).get("operation_generation"),
               "reason": redact_text(reason)[:1200]})
