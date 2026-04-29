"""Medium browser-based uploader.

Medium's posting API was deprecated in 2024; new integration tokens are no
longer issued. This package therefore drives medium.com directly with
Playwright, the same way the Chinese-platform uploaders work.
"""

from pathlib import Path

from conf import BASE_DIR

Path(BASE_DIR / "cookies" / "medium").mkdir(parents=True, exist_ok=True)
