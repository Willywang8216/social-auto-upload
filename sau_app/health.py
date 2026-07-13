"""Liveness and readiness endpoints (Phase 1).

- ``GET /healthz`` — **liveness**: returns 200 as long as the process can serve
  a request. No dependency checks; used by orchestrators to decide whether to
  restart the container.
- ``GET /readyz`` — **readiness**: runs the registered readiness checks (e.g. a
  database ping) and returns 200 only when all pass, otherwise 503. Used to
  decide whether to route traffic to this instance.

Readiness checks are registered on the app via
``register_readiness_check(app, name, fn)``. Each ``fn`` may return:
- ``True`` / ``False``
- a ``(status: str, ok: bool)`` tuple
- or raise, which is reported as a failed check.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

READINESS_CHECKS_KEY = "SAU_READINESS_CHECKS"
_LOGGER = logging.getLogger("sau.health")

health_bp = Blueprint("sau_health", __name__)


@health_bp.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


@health_bp.route("/readyz", methods=["GET"])
def readyz():
    checks = current_app.config.get(READINESS_CHECKS_KEY, {})
    results: dict[str, str] = {}
    all_ok = True

    for name, check in checks.items():
        try:
            outcome = check()
        except Exception as exc:  # noqa: BLE001 — a failing probe must not 500
            _LOGGER.warning("readiness check %s raised: %s", name, exc)
            results[name] = "error"
            all_ok = False
            continue

        if isinstance(outcome, tuple):
            status, ok = outcome
            results[name] = str(status)
            all_ok = all_ok and bool(ok)
        else:
            ok = bool(outcome)
            results[name] = "ok" if ok else "error"
            all_ok = all_ok and ok

    body = {"status": "ready" if all_ok else "not_ready", "checks": results}
    return jsonify(body), (200 if all_ok else 503)
