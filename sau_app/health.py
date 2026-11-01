"""Cheap liveness and fail-closed readiness probes.

``/healthz`` does not touch dependencies. ``/readyz`` runs every registered
check; a database check is mandatory, and a worker check is mandatory when
SAU_REQUIRE_WORKER_HEALTH=1. The registered worker check owns deployment policy
(e.g. an explicit ``not_required`` result for a web-only development process).
Checks return bool or (short status label, bool); exceptions/malformed results
fail readiness without exposing exception text or credentials.
"""
from __future__ import annotations

import logging
import os
import re

from flask import Blueprint, current_app, jsonify
from myUtils.secret_redaction import redact_text

READINESS_CHECKS_KEY = "SAU_READINESS_CHECKS"
_LOGGER = logging.getLogger("sau.health")
health_bp = Blueprint("sau_health", __name__)


def _label(value, fallback: str) -> str:
    # Allow only status/revision identifiers, not errors, URLs or config text.
    text = redact_text(value).strip()
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text) else fallback


def _revision() -> str:
    return _label(os.environ.get("SAU_BUILD_REVISION"), "unknown")


@health_bp.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok", "revision": _revision()}), 200


@health_bp.route("/readyz", methods=["GET"])
def readyz():
    checks = current_app.config.get(READINESS_CHECKS_KEY, {})
    if not isinstance(checks, dict):
        checks = {}
    required = {"database"}
    if os.environ.get("SAU_REQUIRE_WORKER_HEALTH", "0") == "1":
        required.add("worker")
    results: dict[str, str] = {name: "not_registered" for name in sorted(required - checks.keys())}
    all_ok = not results
    for name, check in list(checks.items()):
        label = _label(name, "invalid_check")
        try:
            outcome = check()
            if isinstance(outcome, tuple) and len(outcome) == 2 and isinstance(outcome[1], bool):
                status, ok = outcome
                results[label] = _label(status, "ok" if ok else "error")
            elif isinstance(outcome, bool):
                ok = outcome
                results[label] = "ok" if ok else "error"
            else:
                ok = False
                results[label] = "invalid_result"
        except Exception:  # noqa: BLE001 — a failing probe must not 500 or leak credentials
            _LOGGER.warning("readiness check %s failed", label)
            results[label] = "error"
            ok = False
        all_ok = all_ok and ok
    body = {"status": "ready" if all_ok else "not_ready", "checks": results, "revision": _revision()}
    return jsonify(body), (200 if all_ok else 503)
