# conf.py — local user overrides for project configuration.
#
# Copy this file to ``conf.py`` and edit any line you want to change.
# Every setting has a sane default in ``conf_defaults.py``; the wildcard
# import below pulls those defaults in so a partial ``conf.py`` (one that
# only sets, say, ``LOCAL_CHROME_PATH``) keeps working — every other
# attribute resolves through the defaults module rather than raising
# ``ImportError`` at import time.
#
# You can also drive every setting from environment variables (see
# ``conf_defaults.py`` for the names) and leave this file untouched.

from conf_defaults import *  # noqa: F401,F403 — provides BASE_DIR etc.

# --- Local overrides go below this line. Examples (commented out): ---
#
# from pathlib import Path
# BASE_DIR = Path(__file__).parent.resolve()
# XHS_SERVER = "http://127.0.0.1:11901"
# LOCAL_CHROME_PATH = "C:/Program Files/Google/Chrome/Application/chrome.exe"
# LOCAL_CHROME_HEADLESS = True
# DEBUG_MODE = True
