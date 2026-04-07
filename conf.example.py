from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
XHS_SERVER = "http://127.0.0.1:11901"  # only used by xhs-related flows
LOCAL_CHROME_PATH = ""  # optional, e.g. C:/Program Files/Google/Chrome/Application/chrome.exe
LOCAL_CHROME_HEADLESS = True  # default headless behavior for uploader/examples
DEBUG_MODE = True  # default debug behavior
APP_BASE_URL = "http://127.0.0.1:5173"  # frontend base URL used by OAuth popup flows
BACKEND_PUBLIC_URL = "http://127.0.0.1:5409"  # backend callback base URL for OAuth providers

REDDIT_CLIENT_ID = ""
REDDIT_CLIENT_SECRET = ""
REDDIT_REDIRECT_URI = "http://127.0.0.1:5409/oauth/reddit/callback"

X_CLIENT_ID = ""
X_CLIENT_SECRET = ""
X_REDIRECT_URI = "http://127.0.0.1:5409/oauth/x/callback"
