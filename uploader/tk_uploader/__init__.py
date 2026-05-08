from pathlib import Path

from utils.conf_defaults import BASE_DIR

BASE_DIR = Path(BASE_DIR)

Path(BASE_DIR / "cookies" / "tk_uploader").mkdir(exist_ok=True)