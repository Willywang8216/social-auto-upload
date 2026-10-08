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
