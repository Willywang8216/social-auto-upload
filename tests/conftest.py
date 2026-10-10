"""Shared test isolation.

The worker's per-job logger defaults to ``logs/jobs/job-<id>.log``. The test
suite reuses small job ids (1, 2, ...), so without this every run appended
phantom "target failed" lines to the operator's real log view — test fixtures
such as ``RuntimeError: bluesky said no`` looked like live production errors.
``SAU_JOB_LOG_DIR`` redirects the sink to a throwaway directory.

Media prep has the same shared-state hazard: ``myUtils.media_pipeline``
defaults ``GENERATED_MEDIA_ROOT`` to ``<repo>/generated/campaigns``, which is a
bind mount the (root) production container writes to. On the host that tree is
often root-owned, so a test that builds a campaign workspace died with
``PermissionError: .../generated/campaigns/campaign-<id>`` and then took loguru's
sink down with it (``ValueError: I/O operation on closed file``). Point
``SAU_GENERATED_MEDIA_ROOT`` at a throwaway directory *before* any test imports
``media_pipeline`` so the suite never touches the operator's real artifacts.
This is forced (not ``setdefault``): a stray value in the developer's shell must
not be able to send tests back at the shared, possibly root-owned tree.
"""

from __future__ import annotations

import os
import tempfile
import sys
from pathlib import Path

# Resolve once, before any test imports the worker, and point the per-job sink
# at a temp dir that pytest/tmp cleanup owns.
_TEST_JOB_LOG_DIR = tempfile.mkdtemp(prefix="sau-test-jobs-")
os.environ.setdefault("SAU_JOB_LOG_DIR", _TEST_JOB_LOG_DIR)

# Same idea for the media-prep workspace root.
_TEST_GENERATED_MEDIA_ROOT = os.path.join(
    tempfile.mkdtemp(prefix="sau-test-generated-"), "campaigns"
)
os.environ["SAU_GENERATED_MEDIA_ROOT"] = _TEST_GENERATED_MEDIA_ROOT

# Async media prep is opt-in by environment, and the suite loads the repo .env -
# so enabling it for the deployment silently changed what the *tests* exercise:
# the sync-contract tests (e.g. "a valid submit returns jobs") started taking the
# async branch, which returns `jobs: []` while the campaign is `preparing`, and
# failed for a reason that had nothing to do with their subject.
#
# Pin it OFF for the whole suite. The tests that DO cover the async path set
# `SAU_ASYNC_PREP` themselves via `patch.dict(os.environ, ...)`, so they are
# unaffected, and no ambient value can leak in again.
os.environ["SAU_ASYNC_PREP"] = "0"

# Stop the suite reaching the operator's real Telegram. Tests deliberately
# exercise the failure path, and several patch nothing, so the alert sender read
# the live bot token from .env and delivered them:
#
#   [SAU] Publish failed: bluesky target #1 (job #1) ... RuntimeError: bluesky said no
#
# Job #1 and accounts like acct-1 / fb-bluesky never existed in the database (the
# real rows start at a much higher id) - they are fixtures. Receiving them on a
# phone is indistinguishable from a production incident, which is exactly the
# alarm fatigue this is meant to prevent.
#
# Blanking the credentials makes _send_telegram a no-op, so no ambient value can
# leak. Tests that assert alerting behaviour patch send_ops_alert / the channel
# itself, and are unaffected.
for _alert_key in (
    "SAU_ALERT_TELEGRAM_BOT_TOKEN",
    "SAU_ALERT_TELEGRAM_CHAT_ID",
    "SAU_TG_REVIEW_BOT_TOKEN",
    "SAU_TG_REVIEW_CHAT_ID",
):
    os.environ[_alert_key] = ""

# The publisher refuses subreddits nobody has verified, because publishing to an
# unchecked subreddit is how this deployment was banned from r/GayBros and
# r/GayBody. Tests use synthetic names (r/test), which that guard would reject,
# so relax *only* the unknown-name check here. The rules for known subreddits
# (bans, self-promotion, submission mode, title format) stay enforced, and the
# tests that cover the guard itself set SAU_SUBREDDIT_STRICT=1 explicitly.
os.environ["SAU_SUBREDDIT_STRICT"] = "0"

# Point the suite at a throwaway database, not the production one.
#
# Most modules default to the live path and only accept a `db_path` override:
#
#   myUtils/jobs.py:45          DB_PATH = Path(BASE_DIR) / "db" / "database.db"
#   myUtils/campaigns.py:15     same
#   myUtils/account_events.py:14, analytics_store.py:21,
#   content_generator.py:21     same
#
# So any test (or helper) that calls one of those functions without passing
# db_path writes to PRODUCTION. That is not hypothetical: while a full-suite run
# was in progress, production campaign 2603 was left with no posts and had to be
# reclaimed for a clean re-submit, and the queue and campaign status are shared
# mutable state the tests read too.
#
# SAU_DB_PATH is the documented override (see db/createTable.py and
# scripts/*.py), so pinning it here isolates every reader and writer at once.
# The file is created empty; tests that need a schema bootstrap it themselves.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="sau-test-db-")
os.environ["SAU_DB_PATH"] = os.path.join(_TEST_DB_DIR, "database.db")
# Also neutralise the module-level constants that were bound at import time from
# the production path, for any module already imported before this line runs.
for _module_name in (
    "myUtils.jobs",
    "myUtils.campaigns",
    "myUtils.account_events",
    "myUtils.analytics_store",
    "myUtils.content_generator",
):
    _module = sys.modules.get(_module_name)
    if _module is not None and hasattr(_module, "DB_PATH"):
        _module.DB_PATH = Path(os.environ["SAU_DB_PATH"])
os.environ["SAU_LLM_MIN_INTERVAL_SECONDS"] = "0"  # tests must not sleep on LLM pacing
