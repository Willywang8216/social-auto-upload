"""Bootstrap entry point that is not hidden by the container's /app/db mount.

Source checkouts reuse the canonical db.createTable module (including its test
patch points). Docker replaces this facade with that same implementation during
COPY, so the released service never imports bootstrap code from mutable data.
Use ``python -m myUtils.db_bootstrap`` inside containers; the legacy host CLI
``python db/createTable.py`` remains supported.
"""
from __future__ import annotations

import sys
from db import createTable as _canonical

if __name__ == "__main__":
    _canonical.main()
else:
    sys.modules[__name__] = _canonical
