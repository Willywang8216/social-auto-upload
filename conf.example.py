import os
from pathlib import Path


def _get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


BASE_DIR = Path(__file__).parent.resolve()
XHS_SERVER = os.getenv("XHS_SERVER", "http://127.0.0.1:11901")  # only used by xhs-related flows
LOCAL_CHROME_PATH = os.getenv("LOCAL_CHROME_PATH", "")  # optional, e.g. C:/Program Files/Google/Chrome/Application/chrome.exe
LOCAL_CHROME_HEADLESS = _get_bool_env("LOCAL_CHROME_HEADLESS", True)  # default headless behavior for uploader/examples
DEBUG_MODE = _get_bool_env("DEBUG_MODE", True)  # default debug behavior
