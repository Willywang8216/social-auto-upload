"""Cross-cutting request instrumentation (Phase 1).

Adds three things to the Flask app without altering any existing route's
behavior:

1. **Request / correlation IDs** — every request gets a stable id (reused from
   an inbound ``X-Request-ID`` header when present, otherwise generated). It is
   exposed on ``flask.g.request_id``, echoed back in the response header, and
   attached to log records via a logging filter.
2. **Structured request logging** — a single line per request at completion
   with method, path, status, and request id.
3. **A standard JSON error schema** for *unhandled* framework errors (404 for
   unmatched URLs, 405, and 500). Views that already return an explicit
   ``(jsonify(...), status)`` are untouched — Flask error handlers only fire for
   raised ``HTTPException``/uncaught exceptions, not for normal responses.
"""

from __future__ import annotations

import logging
import time
import uuid

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

REQUEST_ID_ENV_KEY = "SAU_REQUEST_ID_HEADER"
_DEFAULT_HEADER = "X-Request-ID"
_LOGGER = logging.getLogger("sau.request")


class RequestIdLogFilter(logging.Filter):
    """Inject the current request id into every log record as ``request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not hasattr(record, "request_id"):
            record.request_id = _current_request_id() or "-"
        return True


def _current_request_id() -> str | None:
    try:
        return getattr(g, "request_id", None)
    except RuntimeError:
        # Outside of an application/request context.
        return None


def _header_name(app: Flask) -> str:
    return app.config.get("SAU_REQUEST_ID_HEADER", _DEFAULT_HEADER)


def install(app: Flask, *, header_name: str = _DEFAULT_HEADER) -> None:
    """Install request-id middleware, request logging, and error handlers."""

    app.config.setdefault("SAU_REQUEST_ID_HEADER", header_name)

    # Attach the request-id filter to the root logger once so any logger that
    # propagates to root gets the field.
    root = logging.getLogger()
    if not any(isinstance(f, RequestIdLogFilter) for f in root.filters):
        root.addFilter(RequestIdLogFilter())

    @app.before_request
    def _assign_request_id() -> None:
        incoming = request.headers.get(_header_name(app))
        g.request_id = incoming.strip() if incoming and incoming.strip() else uuid.uuid4().hex
        g.request_started_at = time.monotonic()

    @app.after_request
    def _emit_request_id(response):
        request_id = _current_request_id()
        if request_id:
            response.headers[_header_name(app)] = request_id
        started = getattr(g, "request_started_at", None)
        duration_ms = round((time.monotonic() - started) * 1000, 1) if started is not None else None
        _LOGGER.info(
            "request",
            extra={
                "http_method": request.method,
                "http_path": request.path,
                "http_status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    def _error_response(status: int, message: str):
        payload = {"code": status, "msg": message, "data": None}
        request_id = _current_request_id()
        if request_id:
            payload["requestId"] = request_id
        return jsonify(payload), status

    @app.errorhandler(404)
    def _handle_404(exc):  # noqa: ANN001
        return _error_response(404, "not found")

    @app.errorhandler(405)
    def _handle_405(exc):  # noqa: ANN001
        return _error_response(405, "method not allowed")

    @app.errorhandler(HTTPException)
    def _handle_http_exception(exc: HTTPException):
        # Preserve the intended status code; standardize the body.
        return _error_response(exc.code or 500, exc.description or exc.name)

    @app.errorhandler(Exception)
    def _handle_uncaught(exc: Exception):  # noqa: ANN001
        # Let Flask re-raise when configured to propagate (e.g. TESTING/debug),
        # so tests and the debugger see the original traceback.
        if app.config.get("PROPAGATE_EXCEPTIONS") or app.testing or app.debug:
            raise exc
        _LOGGER.exception("unhandled exception", extra={"http_path": request.path})
        return _error_response(500, "internal server error")
