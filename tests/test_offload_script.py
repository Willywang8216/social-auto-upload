import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class OffloadScriptTests(unittest.TestCase):
    def test_copy_verification_uses_rclone_check_before_privileged_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable_dir = root / "bin"
            executable_dir.mkdir()
            calls = root / "calls"
            config = root / "rclone.conf"
            config.write_text("")
            fake_rclone = executable_dir / "rclone"
            fake_rclone.write_text(r'''#!/usr/bin/env bash
[ "$1" = "--config" ] || exit 90
shift 2
printf '%s\n' "$*" >> "$TEST_CALLS"
case " $* " in
  *' lsf '*) printf 'nested/file.mp4\n' ;;
  *' check '*) exit 0 ;;
  *) exit 91 ;;
esac
''')
            fake_sudo = executable_dir / "sudo"
            fake_sudo.write_text(r'''#!/usr/bin/env bash
[ "$1" = "-n" ] || exit 92
shift
exec "$@"
''')
            fake_rm = executable_dir / "rm"
            fake_rm.write_text(r'''#!/usr/bin/env bash
printf 'rm %s\n' "$*" >> "$TEST_CALLS"
exit 0
''')
            for path in (fake_rclone, fake_sudo, fake_rm):
                path.chmod(0o755)
            excludes = root / "excludes"
            excludes.write_text("/keep.mp4\n")
            env = {
                **os.environ,
                "RCLONE_BIN": str(fake_rclone),
                "PATH": f"{executable_dir}:{os.environ['PATH']}",
                "CONF": str(config),
                "SRC": str(root),
                "TEST_EXCLUDES": str(excludes),
                "TEST_SOURCE": str(root / "videoFile"),
                "TEST_DEST": "drive:sau/videoFile",
                "TEST_CALLS": str(calls),
            }
            body = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text().split("purge_verified_sources() {", 1)[1].split("\n}", 1)[0]
            script = r'''
EXCLUDES="$TEST_EXCLUDES"
CONF="$CONF"
RCLONE_BIN="$RCLONE_BIN"
RC=("${RCLONE_BIN}" --config "$CONF")
ts(){ printf 'test'; }
purge_verified_sources() {
''' + body + r'''
}
purge_verified_sources "$TEST_SOURCE" "$TEST_DEST"
'''
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = calls.read_text().splitlines()
            check_lines = [line for line in recorded if line.startswith("check ")]
            self.assertTrue(check_lines)
            for line in check_lines:
                parts = line.split()
                # rclone rejects a FILE as the check destination ("is a file not
                # a directory"), which aborted the whole purge before cleanup
                # could run. It must be given the two transfer roots.
                self.assertEqual(parts[2], "drive:sau/videoFile")
                self.assertIn("--one-way", parts)
                self.assertIn("/nested/file.mp4", parts)
            self.assertEqual(recorded[-1], f"rm -- {root}/videoFile/nested/file.mp4")

    def test_size_mismatch_skips_privileged_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable_dir = root / "bin"
            executable_dir.mkdir()
            calls = root / "calls"
            fake_rclone = executable_dir / "rclone"
            fake_rclone.write_text(r'''#!/usr/bin/env bash
[ "$1" = "--config" ] || exit 90
shift 2
printf '%s\n' "$*" >> "$TEST_CALLS"
case " $* " in
  *' lsf '*) printf 'nested/file.mp4\n' ;;
  *' check '*) exit 1 ;;
  *) exit 91 ;;
esac
''')
            fake_sudo = executable_dir / "sudo"
            fake_sudo.write_text("#!/usr/bin/env bash\nprintf 'sudo-called\\n' >> \"$TEST_CALLS\"\nexit 0\n")
            fake_rm = executable_dir / "rm"
            fake_rm.write_text("#!/usr/bin/env bash\nprintf 'rm-called\\n' >> \"$TEST_CALLS\"\nexit 0\n")
            for path in (fake_rclone, fake_sudo, fake_rm):
                path.chmod(0o755)
            excludes = root / "excludes"
            excludes.write_text("")
            env = {
                **os.environ,
                "RCLONE_BIN": str(fake_rclone),
                "PATH": f"{executable_dir}:{os.environ['PATH']}",
                "CONF": str(root / "rclone.conf"),
                "SRC": str(root),
                "TEST_EXCLUDES": str(excludes),
                "TEST_SOURCE": str(root / "videoFile"),
                "TEST_DEST": "drive:sau/videoFile",
                "TEST_CALLS": str(calls),
            }
            body = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text().split("purge_verified_sources() {", 1)[1].split("\n}", 1)[0]
            script = r'''
EXCLUDES="$TEST_EXCLUDES"
CONF="$CONF"
RCLONE_BIN="$RCLONE_BIN"
RC=("${RCLONE_BIN}" --config "$CONF")
ts(){ printf 'test'; }
purge_verified_sources() {
''' + body + r'''
}
purge_verified_sources "$TEST_SOURCE" "$TEST_DEST"
'''
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            recorded = calls.read_text().splitlines() if calls.exists() else []
            self.assertFalse(any(line.startswith("sudo-called") or line.startswith("rm-called") for line in recorded))

    def test_one_unverifiable_file_does_not_block_cleanup_of_the_others(self):
        """A single mismatch must not strand every other verified source."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable_dir = root / "bin"
            executable_dir.mkdir()
            calls = root / "calls"
            fake_rclone = executable_dir / "rclone"
            fake_rclone.write_text(r'''#!/usr/bin/env bash
[ "$1" = "--config" ] || exit 90
shift 2
printf '%s\n' "$*" >> "$TEST_CALLS"
case " $* " in
  *' lsf '*) printf 'good.mp4\nbad.mp4\n' ;;
  *' check '*) case " $* " in *good.mp4*) exit 0 ;; *) exit 1 ;; esac ;;
  *) exit 91 ;;
esac
''')
            fake_sudo = executable_dir / "sudo"
            fake_sudo.write_text(r'''#!/usr/bin/env bash
[ "$1" = "-n" ] || exit 92
shift
exec "$@"
''')
            fake_rm = executable_dir / "rm"
            fake_rm.write_text(r'''#!/usr/bin/env bash
printf 'rm %s\n' "$*" >> "$TEST_CALLS"
exit 0
''')
            for path in (fake_rclone, fake_sudo, fake_rm):
                path.chmod(0o755)
            excludes = root / "excludes"
            excludes.write_text("")
            env = {
                **os.environ,
                "RCLONE_BIN": str(fake_rclone),
                "PATH": f"{executable_dir}:{os.environ['PATH']}",
                "CONF": str(root / "rclone.conf"),
                "SRC": str(root),
                "TEST_EXCLUDES": str(excludes),
                "TEST_SOURCE": str(root / "videoFile"),
                "TEST_DEST": "drive:sau/videoFile",
                "TEST_CALLS": str(calls),
            }
            body = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text().split("purge_verified_sources() {", 1)[1].split("\n}", 1)[0]
            script = r'''
EXCLUDES="$TEST_EXCLUDES"
CONF="$CONF"
RCLONE_BIN="$RCLONE_BIN"
RC=("${RCLONE_BIN}" --config "$CONF")
ts(){ printf 'test'; }
purge_verified_sources() {
''' + body + r'''
}
purge_verified_sources "$TEST_SOURCE" "$TEST_DEST"
'''
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            # Non-zero so the operator still gets paged about the file that could
            # not be verified...
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("keeping bad.mp4", result.stdout + result.stderr)
            recorded = calls.read_text().splitlines()
            removed = [line for line in recorded if line.startswith("rm ")]
            # ...but the verified sibling was still reclaimed.
            self.assertEqual(removed, [f"rm -- {root}/videoFile/good.mp4"])

    def _notify_harness(self, root: Path, *, tg_env: bool = True):
        """Build a runnable shell around the script's notify() + previous-rc."""
        executable_dir = root / "bin"
        executable_dir.mkdir(exist_ok=True)
        calls = root / "calls"
        fake_curl = executable_dir / "curl"
        fake_curl.write_text(r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_CALLS"
exit 0
''')
        fake_curl.chmod(0o755)
        log = root / "offload.log"
        if tg_env:
            env_file = root / "telegram_env"
            env_file.write_text("TG_TOKEN=fake\nTG_CHAT=123\n")
        else:
            env_file = root / "missing_env"
        source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
        previous = source.split("offload_previous_rc() {", 1)[1].split("\n}", 1)[0]
        notify = source.split("notify() {", 1)[1].split("\n}", 1)[0]
        harness = r'''
LOG="$TEST_LOG"
TGENV="$TEST_TG_ENV"
ts(){ printf '2026-10-01T00:00:00Z'; }
hostname(){ printf 'testhost'; }
offload_previous_rc() {
''' + previous + r'''
}
notify() {
''' + notify + r'''
}
notify "$1" "$2"
'''
        env = {
            **os.environ,
            "PATH": f"{executable_dir}:{os.environ['PATH']}",
            "TEST_CALLS": str(calls),
            "TEST_LOG": str(log),
            "TEST_TG_ENV": str(env_file),
        }
        return harness, env, calls, log

    def test_notify_pages_on_failure_with_a_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, env, calls, _log = self._notify_harness(Path(tmp))
            subprocess.run(["bash", "-c", harness, "_", "1", "0"], env=env, text=True, capture_output=True)
            sent = calls.read_text() if calls.exists() else ""
            # A timestamp is what makes a late delivery recognisable as old.
            self.assertIn("FAILED", sent)
            self.assertIn("2026-10-01T00:00:00Z", sent)

    def test_notify_announces_recovery_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, env, calls, _log = self._notify_harness(Path(tmp))
            subprocess.run(["bash", "-c", harness, "_", "0", "1"], env=env, text=True, capture_output=True)
            self.assertIn("RECOVERED", calls.read_text())

            # healthy -> healthy must stay silent, or it becomes a heartbeat.
            calls.unlink()
            subprocess.run(["bash", "-c", harness, "_", "0", "0"], env=env, text=True, capture_output=True)
            self.assertFalse(calls.exists())

    def test_notify_is_silent_without_a_previous_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, env, calls, _log = self._notify_harness(Path(tmp))
            subprocess.run(["bash", "-c", harness, "_", "0", ""], env=env, text=True, capture_output=True)
            self.assertFalse(calls.exists())

    def test_notify_noops_without_a_telegram_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, env, calls, _log = self._notify_harness(Path(tmp), tg_env=False)
            subprocess.run(["bash", "-c", harness, "_", "1", "0"], env=env, text=True, capture_output=True)
            self.assertFalse(calls.exists())

    def test_previous_rc_reads_the_second_newest_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness, env, _calls, log = self._notify_harness(root)
            log.write_text(
                "[t] offload done rc=1 local videoFile=1 uploads=0\n"
                "[t] offload done rc=0 local videoFile=1 uploads=0\n"
                "[t] offload done rc=0 local videoFile=1 uploads=0\n"
            )
            result = subprocess.run(
                ["bash", "-c", harness.replace('notify "$1" "$2"', 'offload_previous_rc'),
                 "_", "0", "0"],
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.stdout.strip(), "0")



if __name__ == "__main__":
    unittest.main()
