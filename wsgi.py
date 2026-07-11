"""WSGI entrypoint for production servers (Gunicorn).

Run with, e.g.::

    gunicorn wsgi:app --workers 1 --threads 8 --bind 0.0.0.0:5409

The application is built by ``sau_app.create_app()``, which wraps the existing
``sau_backend`` monolith with the Phase 1 factory extensions (request IDs,
health endpoints, config validation, standard error schema).

Note: until the worker is split out (Phase 9), the app still starts its
in-process publishing/maintenance threads, so production should run a **single**
Gunicorn worker (``--workers 1``) and rely on ``--threads`` for concurrency, to
avoid multiple processes draining the job queue at once.
"""

from __future__ import annotations

from sau_app import create_app

app = create_app()


if __name__ == "__main__":
    # Convenience for local runs without Gunicorn; production uses ``wsgi:app``.
    app.run(host="0.0.0.0", port=5409, threaded=True)
