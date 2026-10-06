"""Shared test isolation.

The worker's per-job logger defaults to ``logs/jobs/job-<id>.log``. The test
suite reuses small job ids (1, 2, ...), so without this every run appended
phantom "target failed" lines to the operator's real log view — test fixtures
such as ``RuntimeError: bluesky said no`` looked like live production errors.
``SAU_JOB_LOG_DIR`` redirects the sink to a throwaway directory.
"""

from __future__ import annotations

import os
import tempfile

# Resolve once, before any test imports the worker, and point the per-job sink
# at a temp dir that pytest/tmp cleanup owns.
_TEST_JOB_LOG_DIR = tempfile.mkdtemp(prefix="sau-test-jobs-")
os.environ.setdefault("SAU_JOB_LOG_DIR", _TEST_JOB_LOG_DIR)
