"""ci-watch must recognise a container that already runs the target commit.

The bug this pins (2026-10-22): two deployers produced two different image IDs
for the same commit — Watchtower pulled the GHCR build, ci-watch builds locally —
and ci-watch compared **image IDs**. It therefore decided "the running container
differs" every two minutes and re-deployed forever (119 occurrences in the log),
force-restarting the container after each wait and killing the in-flight campaign
preps (7 campaigns stranded in needs_review).

The identity check must therefore:
  * accept a running container whose `org.opencontainers.image.revision` label
    equals origin/main, whatever its image ID is,
  * still fall back to the image-ID comparison for an image with no label,
  * and still re-deploy when the revision genuinely differs (a manual revert).

The real decision block from `scripts/ci-watch.sh` is what runs; only `git` and
`docker` are stubbed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci-watch.sh"
TARGET = "f61ef1f2b48c0671eb83d75dd6a9dc437dd6c44a"


def _identity_harness(root: Path, *, running_revision: str, running_id: str,
                      target_id: str, image_exists: bool) -> str:
    """The real decision block, with git/docker replaced by shell functions."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("# Resolve origin/main")
    end = source.index("# Build (only when the image for this commit does not already exist)")
    block = source[start:end]
    log = root / "ci-watch.log"
    state = root / "ci-watch.state"
    state.write_text(TARGET + "\n", encoding="utf-8")
    return textwrap.dedent(f"""
        set -uo pipefail
        LOG="{log}"
        STATE="{state}"
        SERVICE="social-auto-upload"
        IMAGE_BASE="ghcr.io/willywang8216/social-auto-upload"
        log() {{ printf '%s\\n' "$*" >> "$LOG"; }}
        notify_failure() {{ :; }}
        git() {{
          case "$1" in
            fetch) return 0 ;;
            rev-parse) printf '%s\\n' "{TARGET}"; return 0 ;;
            *) command git "$@" ;;
          esac
        }}
        docker() {{
          if [ "$1" = "inspect" ]; then
            case "$3" in
              *revision*) printf '%s\\n' "{running_revision}" ;;
              *Image*)    printf '%s\\n' "{running_id}" ;;
            esac
            return 0
          fi
          if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
            if [ "{1 if image_exists else 0}" = "0" ]; then return 1; fi
            case "$4" in
              *Id*) printf '%s\\n' "{target_id}" ;;
            esac
            return 0
          fi
          exit 0
        }}
        {block}
        printf 'FELL_THROUGH\\n' >> "$LOG"
    """)


def _run_identity_check(root: Path, **kwargs) -> str:
    harness = _identity_harness(root, **kwargs)
    subprocess.run(["bash", "-c", harness], env={**os.environ}, text=True,
                   capture_output=True)
    log = root / "ci-watch.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


class CiWatchIdentityTests(unittest.TestCase):
    def test_ghcr_pulled_container_for_the_same_commit_is_left_alone(self):
        """The exact 2026-10-22 loop: same commit, different image ID."""
        with tempfile.TemporaryDirectory() as tmp:
            text = _run_identity_check(
                Path(tmp),
                running_revision=TARGET,          # GHCR build: correct label
                running_id="sha256:68a4b943",
                target_id="sha256:0b8abc64",      # local build: different ID
                image_exists=True,
            )
            self.assertIn("already deployed and running (revision", text)
            self.assertNotIn("re-deploying", text)
            self.assertNotIn("FELL_THROUGH", text)

    def test_image_id_fallback_still_works_without_a_revision_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = _run_identity_check(
                Path(tmp),
                running_revision="",
                running_id="sha256:same",
                target_id="sha256:same",
                image_exists=True,
            )
            self.assertIn("already deployed and running (image id)", text)
            self.assertNotIn("FELL_THROUGH", text)

    def test_a_different_revision_is_redeployed(self):
        """A manual revert must still be corrected."""
        with tempfile.TemporaryDirectory() as tmp:
            text = _run_identity_check(
                Path(tmp),
                running_revision="b746c0c79a7ad93125c0a14f6b720b123830d106",
                running_id="sha256:old",
                target_id="sha256:new",
                image_exists=True,
            )
            self.assertIn("re-deploying", text)
            self.assertIn("revision=b746c0c", text)
            self.assertIn("FELL_THROUGH", text)

    def test_missing_local_image_is_not_treated_as_deployed(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = _run_identity_check(
                Path(tmp),
                running_revision="",
                running_id="sha256:whatever",
                target_id="",
                image_exists=False,
            )
            self.assertIn("re-deploying", text)


class CiWatchDeferralTests(unittest.TestCase):
    def test_script_defers_instead_of_deploying_over_in_flight_work(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("deferring deploy of ${SHORT}", source)
        self.assertIn("SAU_CI_WATCH_FORCE_DEPLOY", source)
        # the forced path must still exist for emergencies
        self.assertIn("deploying anyway (forced)", source)

    def test_build_passes_the_commit_and_date_labels(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('--build-arg GIT_SHA="$TARGET"', source)
        self.assertIn("--build-arg BUILD_DATE=", source)

    def test_dockerfile_stamps_the_labels(self):
        dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG GIT_SHA", dockerfile)
        self.assertIn('org.opencontainers.image.revision="${GIT_SHA}"', dockerfile)

    def test_watchtower_is_opted_out_so_only_one_deployer_updates_the_container(self):
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('com.centurylinklabs.watchtower.enable: "false"', compose)


if __name__ == "__main__":
    unittest.main()
