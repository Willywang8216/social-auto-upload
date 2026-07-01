from __future__ import annotations

from pathlib import Path

try:
    import conf as _conf
except ModuleNotFoundError:  # pragma: no cover - environment-specific
    _conf = None

_BASE_DIR_DEFAULT = Path(__file__).resolve().parent.parent

BASE_DIR = Path(getattr(_conf, 'BASE_DIR', _BASE_DIR_DEFAULT))
XHS_SERVER = getattr(_conf, 'XHS_SERVER', 'http://127.0.0.1:11901')
LOCAL_CHROME_PATH = getattr(_conf, 'LOCAL_CHROME_PATH', '')
LOCAL_CHROME_HEADLESS = getattr(_conf, 'LOCAL_CHROME_HEADLESS', True)
DEBUG_MODE = getattr(_conf, 'DEBUG_MODE', True)
YT_PROXY = getattr(_conf, 'YT_PROXY', None)
REDDIT_PROXY = getattr(_conf, 'REDDIT_PROXY', None)
