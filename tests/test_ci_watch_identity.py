"""Exercise the actual watcher identity gate with fake Docker/gate commands.

A mutable image tag must not cause a deploy loop. The current contract is
stronger than revision alone: a matching web/worker pair, exact image receipt,
revision labels and both readiness probes are required. No real Docker, network,
credentials, or deployment commands run in this harness.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from scripts import sqlite_backup

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci-watch.sh"
TARGET = "f61ef1f2b48c0671eb83d75dd6a9dc437dd6c44a"
IMAGE = "sha256:" + "a" * 64
OTHER_IMAGE = "sha256:" + "b" * 64


def _bash():
    if os.name == "nt" and (git := shutil.which("git")):
        candidate = Path(git).parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash")


def _run_identity_check(root: Path, *, revision=TARGET, web_image=IMAGE,
                        worker_image=IMAGE, receipt_image=IMAGE, receipts_ok=True,
                        health="healthy", probes_ok=True, worker_present=True,
                        worker_revision=None, last=TARGET) -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("already_healthy() {")
    block = source[start:source.index("BUILD_LOG=", start)]
    log = root / "watch.log"
    worker_revision = revision if worker_revision is None else worker_revision
    harness = f"""
set -euo pipefail
LOG={shlex.quote(log.as_posix())}
LOG_DIR={shlex.quote(root.as_posix())}
TARGET={TARGET}
LAST={shlex.quote(last)}
SERVICE=social-auto-upload
WORKER=social-auto-upload-worker
CI_WATCH_FORCE=0
PYTHON=gate_check
log() {{ printf '%s\\n' "$*" >> "$LOG"; }}
gate_check() {{
  [[ {int(receipts_ok)} == 1 ]] || return 1
  [[ "$*" == *"--image-id {receipt_image}"* ]]
}}
docker() {{
  if [[ "$1" == inspect ]]; then
    local name="${{@: -1}}"
    if [[ "$name" == "$WORKER" && {int(worker_present)} != 1 ]]; then return 1; fi
    case "$3" in
      *Image*)
        if [[ "$name" == "$WORKER" ]]; then printf '%s\\n' '{worker_image}';
        else printf '%s\\n' '{web_image}'; fi ;;
      *revision*)
        if [[ "$name" == "$WORKER" ]]; then printf '%s\\n' '{worker_revision}';
        else printf '%s\\n' '{revision}'; fi ;;
      *Health*) printf '%s\\n' '{health}' ;;
      *) return 97 ;;
    esac
    return 0
  fi
  if [[ "$1" == exec ]]; then [[ {int(probes_ok)} == 1 ]]; return; fi
  # In particular, never inspect a mutable image tag or invoke a real daemon.
  return 98
}}
{block}
printf 'GATES_REQUIRED\\n' >> "$LOG"
"""
    result = subprocess.run([_bash(), "-c", harness], capture_output=True, text=True,
                            timeout=15, env={**os.environ})
    if result.returncode:
        raise AssertionError(f"watcher harness failed ({result.returncode}): {result.stderr}")
    return log.read_text(encoding="utf-8")


@unittest.skipUnless(_bash(), "Bash is required to exercise the deployment shell")
class CiWatchIdentityTests(unittest.TestCase):
    def check(self, *, healthy, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            output = _run_identity_check(Path(tmp), **kwargs)
        self.assertEqual("nothing to do" in output, healthy, output)
        self.assertEqual("GATES_REQUIRED" in output, not healthy, output)

    def test_gated_running_pair_is_left_alone_without_consulting_mutable_tags(self):
        self.check(healthy=True)

    def test_an_unlabelled_image_is_not_certified_by_image_id_alone(self):
        self.check(healthy=False, revision="")

    def test_a_different_revision_requires_gated_deployment(self):
        self.check(healthy=False, revision="b" * 40)

    def test_mismatched_role_images_are_not_accepted(self):
        self.check(healthy=False, worker_image=OTHER_IMAGE)

    def test_missing_worker_is_not_accepted(self):
        self.check(healthy=False, worker_present=False)

    def test_missing_or_different_image_receipt_is_not_accepted(self):
        self.check(healthy=False, receipts_ok=False)
        self.check(healthy=False, receipt_image=OTHER_IMAGE)

    def test_wrong_worker_revision_is_not_hidden_by_healthy_web(self):
        self.check(healthy=False, worker_revision="b" * 40)

    def test_unhealthy_or_failed_readiness_requires_verification(self):
        self.check(healthy=False, health="unhealthy")
        self.check(healthy=False, probes_ok=False)

    def test_a_missing_success_marker_is_not_treated_as_verified(self):
        self.check(healthy=False, last="")


class CiWatchDeferralTests(unittest.TestCase):
    def test_busy_work_is_drained_without_an_emergency_force_kill(self):
        watcher = SCRIPT.read_text(encoding="utf-8")
        deploy = (REPO / "scripts/deploy-local.sh").read_text(encoding="utf-8")
        self.assertIn('bash "$WORKTREE/scripts/deploy-local.sh"', watcher)
        self.assertIn("wait_drained || fail", deploy)
        self.assertNotIn("SAU_CI_WATCH_FORCE_DEPLOY", watcher)
        self.assertNotIn("deploying anyway (forced)", watcher)
        self.assertNotIn("--signal=KILL", deploy)
        self.assertLess(deploy.index("wait_drained || fail"), deploy.index("social-auto-upload -m myUtils.db_bootstrap"))

    def test_build_passes_the_exact_commit_and_date_labels(self):
        source = (REPO / "scripts/deploy-local.sh").read_text(encoding="utf-8")
        self.assertIn('--build-arg GIT_SHA="$REVISION"', source)
        self.assertIn('--build-arg SAU_BUILD_REVISION="$REVISION"', source)
        self.assertIn("--build-arg BUILD_DATE=", source)

    def test_dockerfile_stamps_the_revision_used_by_both_roles(self):
        source = (REPO / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG GIT_SHA", source)
        self.assertIn('org.opencontainers.image.revision="${SAU_BUILD_REVISION}"', source)
        self.assertIn('ENV SAU_BUILD_REVISION=${SAU_BUILD_REVISION}', source)

    def test_dirty_and_unpublished_local_work_is_preserved(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(source.index("scripts/release_checks.py clean"), source.index("git fetch"))
        self.assertIn('git merge-base --is-ancestor "$LOCAL_HEAD" "$TARGET"', source)
        self.assertIn("unpublished/divergent history; refusing to overwrite", source)
        self.assertNotIn("git reset", source)
        self.assertIn("watcher will not release the pause", source)

    def test_long_lived_prep_leases_are_not_ignored_by_a_time_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "activity.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript("CREATE TABLE publish_job_targets(status TEXT, lease_token TEXT);"
                                   "CREATE TABLE campaigns(status TEXT, metadata_json TEXT);")
                conn.execute("INSERT INTO campaigns VALUES ('preparing', ?)",
                             (json.dumps({"_prepLease": {"owner": "slow", "claimedAt": "1999-01-01"}}),))
                conn.commit()
            finally:
                conn.close()
            activity = sqlite_backup.activity(path)
            self.assertEqual(activity["active_preps"], 1)
            self.assertFalse(activity["drained"])
        self.assertNotIn("MAX_DEFER", SCRIPT.read_text(encoding="utf-8"))

    def test_watchtower_is_opted_out_so_only_one_deployer_updates_the_pair(self):
        source = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('com.centurylinklabs.watchtower.enable: "false"', source)


if __name__ == "__main__":
    unittest.main()
