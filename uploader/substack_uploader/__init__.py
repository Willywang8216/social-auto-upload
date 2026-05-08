"""Substack browser-based uploader.

Substack has no public posting API. Each writer publishes from
``<subdomain>.substack.com/publish/post``. The uploader needs both a valid
session cookie and the writer's subdomain.
"""

from pathlib import Path

from utils.conf_defaults import BASE_DIR

BASE_DIR = Path(BASE_DIR)

Path(BASE_DIR / "cookies" / "substack").mkdir(parents=True, exist_ok=True)
