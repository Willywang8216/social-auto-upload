"""WSGI entrypoint for production servers (Gunicorn).

Run with, e.g.::

    gunicorn wsgi:app --workers 1 --threads 8 --bind 0.0.0.0:5409

The application is built by ``sau_app.create_app()``, which wraps the existing
``sau_backend`` monolith with the Phase 1 factory extensions (request IDs,
health endpoints, config validation, standard error schema).

Publishing and maintenance belong exclusively to the standalone worker service.
This entrypoint always selects the web role before importing the application,
even if an old local environment requested embedded background work.
"""

from __future__ import annotations

import os

os.environ["SAU_PROCESS_ROLE"] = "web"

from sau_app import create_app

app = create_app()


if __name__ == "__main__":
    # Convenience for local runs without Gunicorn; production uses ``wsgi:app``.
    app.run(host="0.0.0.0", port=5409, threaded=True)
