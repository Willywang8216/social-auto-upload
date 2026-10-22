import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def _fn(source: str, name: str) -> str:
    """Body of a shell function in offload_to_drive.sh (functions end at a bare `}`)."""
    body = source.split(f"{name}() {{", 1)[1]
    return body[: body.index("\n}")]


def _purge_harness(source: str, *, ledger: Path | None = None) -> str:
    """The purge helpers, wired the way the real script wires them."""
    return r"""
EXCLUDES="$TEST_EXCLUDES"
CONF="$CONF"
RCLONE_BIN="$RCLONE_BIN"
RC=("${RCLONE_BIN}" --config "$CONF")
DEFERRED_LEDGER="$TEST_LEDGER"
DEFERRED_COUNT=0
DEFERRED_ALERT_AT=${SAU_OFFLOAD_DEFER_ALERT_AT:-3}
SAU_OFFLOAD_VERIFY_RETRY_SLEEP=0
register_verified_source(){ return 0; }
ts(){ printf 'test'; }
escape_rclone_filter_path() {
""" + _fn(source, "escape_rclone_filter_path") + r"""
}
verify_remote_copy() {
""" + _fn(source, "verify_remote_copy") + r"""
}
note_deferred() {
""" + _fn(source, "note_deferred") + r"""
}
purge_verified_sources() {
""" + _fn(source, "purge_verified_sources") + r"""
}
purge_verified_sources "$TEST_SOURCE" "$TEST_DEST"
"""


class OffloadScriptTests(unittest.TestCase):
    def test_copy_verification_uses_rclone_check_before_privileged_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "videoFile" / "nested").mkdir(parents=True)
            (root / "videoFile" / "nested" / "file.mp4").write_bytes(b"video")
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
                "TEST_LEDGER": str(root / "offload-deferred.txt"),
            }
            source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            script = _purge_harness(source)
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
                "TEST_LEDGER": str(root / "offload-deferred.txt"),
            }
            source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            script = _purge_harness(source)
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            recorded = calls.read_text().splitlines() if calls.exists() else []
            self.assertFalse(any(line.startswith("sudo-called") or line.startswith("rm-called") for line in recorded))

    def test_one_unverifiable_file_does_not_block_cleanup_of_the_others(self):
        """A single mismatch must not strand every other verified source."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "videoFile").mkdir()
            (root / "videoFile" / "good.mp4").write_bytes(b"good")
            (root / "videoFile" / "bad.mp4").write_bytes(b"bad")
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
                "TEST_LEDGER": str(root / "offload-deferred.txt"),
            }
            source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            script = _purge_harness(source)
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            # A single unverifiable file is *deferred*, not lost: it stays local,
            # the run stays green, and only a repeat offender (3 consecutive runs)
            # fails and pages. One transient Drive hiccup must not page anyone.
            self.assertEqual(result.returncode, 0, result.stderr)
            # note_deferred appends to the run's ledger; the main script rotates
            # `.new` into place at the end of the run (see the done line).
            ledger = (root / "offload-deferred.txt.new").read_text()
            self.assertIn("bad.mp4\t1", ledger)
            recorded = calls.read_text().splitlines()
            removed = [line for line in recorded if line.startswith("rm ")]
            # ...but the verified sibling was still reclaimed.
            self.assertEqual(removed, [f"rm -- {root}/videoFile/good.mp4"])

    def _deferral_harness(self, root: Path, *, hash_rc: int, size_rc: int):
        """Run the purge with a stubbed rclone answering check / check --size-only."""
        executable_dir = root / "bin"
        executable_dir.mkdir(exist_ok=True)
        calls = root / "calls"
        fake_rclone = executable_dir / "rclone"
        fake_rclone.write_text(f'''#!/usr/bin/env bash
[ "$1" = "--config" ] || exit 90
shift 2
printf '%s\\n' "$*" >> "$TEST_CALLS"
case " $* " in
  *' lsf '*) printf 'clip.mp4\\n' ;;
  *' check '*--size-only*) exit {size_rc} ;;
  *' check '*) exit {hash_rc} ;;
  *) exit 91 ;;
esac
''')
        fake_sudo = executable_dir / "sudo"
        fake_sudo.write_text(
            "#!/usr/bin/env bash\n"
            "[ \"$1\" = \"-n\" ] || exit 92\n"
            "shift\n"
            "exec \"$@\"\n"
        )
        fake_rm = executable_dir / "rm"
        fake_rm.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'rm %s\\n' \"$*\" >> \"$TEST_CALLS\"\n"
            "exit 0\n"
        )
        for path in (fake_rclone, fake_sudo, fake_rm):
            path.chmod(0o755)
        excludes = root / "excludes"
        excludes.write_text("")
        (root / "videoFile").mkdir(exist_ok=True)
        (root / "videoFile" / "clip.mp4").write_bytes(b"data")
        source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
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
            "TEST_LEDGER": str(root / "offload-deferred.txt"),
        }
        return _purge_harness(source), env, calls

    def test_hash_lag_falls_back_to_a_size_check_instead_of_failing(self):
        """Google Drive can serve an object before its MD5 is populated.

        Campaign-2563 on 2026-10-22: uploaded 21:25:54, "remote copy is missing
        or differs" at 21:27:20, verified clean at 21:37. That one race paged the
        operator and left 49 MB on the VPS. A size match must be accepted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, env, calls = self._deferral_harness(root, hash_rc=1, size_rc=0)
            result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = calls.read_text().splitlines()
            self.assertTrue(any("--size-only" in line for line in recorded))
            removed = [line for line in recorded if line.startswith("rm ")]
            self.assertEqual(len(removed), 1)
            self.assertTrue(removed[0].endswith("videoFile/clip.mp4"), removed)
            self.assertIn("verified by size only", result.stdout)

    def test_repeat_offender_pages_after_three_consecutive_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, env, calls = self._deferral_harness(root, hash_rc=1, size_rc=1)
            ledger = root / "offload-deferred.txt"
            for run in (1, 2, 3):
                pending = root / "offload-deferred.txt.new"
                if pending.exists():
                    ledger.write_text(pending.read_text())
                result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
                if run < 3:
                    self.assertEqual(result.returncode, 0, f"run {run} must not page: {result.stderr}")
                else:
                    self.assertEqual(result.returncode, 1, "the third consecutive deferral must page")
                    self.assertIn("DEFERRED 3 times", result.stdout)
            self.assertFalse([line for line in calls.read_text().splitlines() if line.startswith("rm ")])

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
        # notify() now sends through the app's own channel (ops_alerts), with the
        # mailserver curl only as a fallback. Point SRC at a fake tree holding a
        # stub .venv/bin/python so the test can observe what would be sent.
        fake_src = root / "fake-src"
        (fake_src / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        stub_python = fake_src / ".venv" / "bin" / "python"
        stub_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'ops_alerts %s\\n' \"$*\" >> \"$TEST_CALLS\"\n"
            "exit 0\n"
        )
        stub_python.chmod(0o755)
        harness = r'''
SRC="$TEST_SRC"
LOG="$TEST_LOG"
TGENV="$TEST_TG_ENV"
DEFERRED_COUNT=0
ts(){ printf '2026-10-01T00:00:00Z'; }
hostname(){ printf 'testhost'; }
offload_previous_rc() {
''' + _fn(source, "offload_previous_rc") + r'''
}
notify() {
''' + _fn(source, "notify") + r'''
}
notify "$1" "$2"
'''
        env = {
            **os.environ,
            "PATH": f"{executable_dir}:{os.environ['PATH']}",
            "TEST_CALLS": str(calls),
            "TEST_LOG": str(log),
            "TEST_TG_ENV": str(env_file),
            "TEST_SRC": str(fake_src),
        }
        return harness, env, calls, log

    def test_notify_pages_on_failure_with_a_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, env, calls, _log = self._notify_harness(Path(tmp))
            subprocess.run(["bash", "-c", harness, "_", "1", "0"], env=env, text=True, capture_output=True)
            sent = calls.read_text() if calls.exists() else ""
            self.assertIn("ops_alerts", sent)
            # A timestamp is what makes a late delivery recognisable as old.
            self.assertIn("2026-10-01T00:00:00Z", sent)

    def test_notify_announces_recovery_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, env, calls, _log = self._notify_harness(Path(tmp))
            subprocess.run(["bash", "-c", harness, "_", "0", "1"], env=env, text=True, capture_output=True)
            self.assertIn("恢復", calls.read_text())

            # healthy -> healthy must stay silent, or it becomes a heartbeat.
            calls.unlink()
            subprocess.run(["bash", "-c", harness, "_", "0", "0"], env=env, text=True, capture_output=True)
            self.assertFalse(calls.exists())

    def test_notify_is_silent_without_a_previous_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness, env, calls, _log = self._notify_harness(Path(tmp))
            subprocess.run(["bash", "-c", harness, "_", "0", ""], env=env, text=True, capture_output=True)
            self.assertFalse(calls.exists())

    def test_notify_falls_back_when_ops_alerts_is_unavailable(self):
        """ops_alerts is primary; the mailserver curl is the fallback.

        With no app checkout the fallback runs, and with no TG env either the
        run still stays silent (alerting must never fail the offload).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness, env, calls, _log = self._notify_harness(root, tg_env=True)
            env.pop("TEST_SRC", None)  # no stub .venv -> the python branch is skipped
            subprocess.run(["bash", "-c", harness, "_", "1", "0"], env=env, text=True, capture_output=True)
            self.assertTrue(calls.exists(), "the mailserver fallback must still fire")

            calls.unlink()
            harness, env, calls, _log = self._notify_harness(root, tg_env=False)
            env.pop("TEST_SRC", None)
            subprocess.run(["bash", "-c", harness, "_", "1", "0"], env=env, text=True, capture_output=True)
            self.assertFalse(calls.exists())

    def test_preflight_fails_closed_when_generated_backend_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            source = root / "source"
            for dirname in ("videoFile", "uploads", "generated"):
                (source / dirname).mkdir(parents=True)
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE storage_backends (id INTEGER PRIMARY KEY, provider TEXT, bucket TEXT, endpoint TEXT, enabled INTEGER)")
                conn.executemany(
                    "INSERT INTO storage_backends (provider,bucket,endpoint,enabled) VALUES (?,?,?,1)",
                    [("rclone", "drive", "sau/videoFile"), ("rclone", "drive", "sau/uploads")],
                )
            source_text = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source_text.split("preflight_storage_backends() {", 1)[1].split("\n}", 1)[0]
            script = 'DB="$TEST_DB"\nDST=drive:sau\nSRC="$TEST_SRC"\npreflight_storage_backends() {\n' + body + '\n}\npreflight_storage_backends\n'
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "TEST_DB": str(db), "TEST_SRC": str(source)}, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sau/generated", result.stderr)

    def test_registration_failure_does_not_create_partial_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source"
            media = src / "generated" / "campaigns" / "clip.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"verified bytes")
            db = root / "db.sqlite"
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE storage_backends (id INTEGER PRIMARY KEY, provider TEXT, bucket TEXT, endpoint TEXT, enabled INTEGER)")
                conn.execute("CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT, file_path TEXT, filesize INTEGER, storage_key TEXT, storage_backend_id INTEGER)")
                conn.execute("INSERT INTO file_records (filename,file_path,filesize) VALUES (?,?,?)", ("clip.mp4", "generated/campaigns/clip.mp4", media.stat().st_size))
            source_text = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source_text.split("register_verified_source() {", 1)[1].split("\n}", 1)[0]
            script = 'DB="$TEST_DB"\nDST=drive:sau\nregister_verified_source() {\n' + body + '\n}\nregister_verified_source "$TEST_SOURCE_ROOT" campaigns/clip.mp4\n'
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "TEST_DB": str(db), "TEST_SOURCE_ROOT": str(src / "generated")}, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(media.is_file(), "registration failure must never remove the source")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT storage_key,storage_backend_id FROM file_records").fetchone(), (None, None))

    def test_registration_persists_relative_restore_key_for_verified_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source"
            media = src / "generated" / "campaigns" / "clip.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"verified bytes")
            db = root / "db.sqlite"
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE storage_backends (id INTEGER PRIMARY KEY, provider TEXT, bucket TEXT, endpoint TEXT, enabled INTEGER)")
                conn.execute("INSERT INTO storage_backends VALUES (7,'rclone','drive','sau/generated',1)")
                conn.execute("CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT, file_path TEXT, filesize INTEGER, storage_key TEXT, storage_backend_id INTEGER)")
                conn.execute("INSERT INTO file_records (filename,file_path,filesize) VALUES (?,?,?)", ("clip.mp4", "generated/campaigns/clip.mp4", media.stat().st_size))
            source_text = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source_text.split("register_verified_source() {", 1)[1].split("\n}", 1)[0]
            script = 'DB="$TEST_DB"\nDST=drive:sau\nregister_verified_source() {\n' + body + '\n}\nregister_verified_source "$TEST_SOURCE_ROOT" campaigns/clip.mp4\n'
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "TEST_DB": str(db), "TEST_SOURCE_ROOT": str(src / "generated")}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT storage_key,storage_backend_id FROM file_records").fetchone(), ("campaigns/clip.mp4", 7))

    def test_rclone_filter_escape_is_literal_for_glob_characters(self):
        source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
        body = source.split("escape_rclone_filter_path() {", 1)[1].split("\n}", 1)[0]
        script = "escape_rclone_filter_path() {\n" + body + "\n}\nescape_rclone_filter_path \"$1\""
        result = subprocess.run(
            ["bash", "-c", script, "_", "campaigns/name*[1]{draft}.mp4"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), r"campaigns/name\*\[1\]\{draft\}.mp4")

    def test_verified_source_repairs_legacy_filesize_before_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source"
            media = src / "videoFile" / "_homealone" / "clip.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"verified media bytes")
            db = root / "db.sqlite"
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE storage_backends (id INTEGER PRIMARY KEY, provider TEXT, bucket TEXT, endpoint TEXT, enabled INTEGER)")
                conn.execute("INSERT INTO storage_backends VALUES (7,'rclone','drive','sau/videoFile',1)")
                conn.execute("CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT, file_path TEXT, filesize REAL, storage_key TEXT, storage_backend_id INTEGER)")
                conn.execute("INSERT INTO file_records (filename,file_path,filesize) VALUES (?,?,?)", ("clip.mp4", "_homealone/clip.mp4", 0.25))
            source_text = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source_text.split("register_verified_source() {", 1)[1].split("\n}", 1)[0]
            script = 'DB="$TEST_DB"\nDST=drive:sau\nregister_verified_source() {\n' + body + '\n}\nregister_verified_source "$TEST_SOURCE_ROOT" _homealone/clip.mp4\n'
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "TEST_DB": str(db), "TEST_SOURCE_ROOT": str(src / "videoFile")}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(db) as conn:
                filesize, key, backend = conn.execute("SELECT filesize,storage_key,storage_backend_id FROM file_records").fetchone()
            self.assertEqual(filesize, media.stat().st_size)
            self.assertEqual((key, backend), ("_homealone/clip.mp4", 7))

    def test_concurrent_run_skips_when_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            log = root / "logs" / "offload.log"
            lock_dir = root / "lock-dir"
            lock_dir.mkdir()
            lock = lock_dir / "drive-offload.lock"
            source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source.split("acquire_offload_lock() {", 1)[1].split("\n}", 1)[0]
            harness = '''SRC="$TEST_SRC"\nLOG="$TEST_LOG"\nHOME="$TEST_HOME"\nOFFLOAD_LOCK_DIR="$TEST_LOCK_DIR"\nacquire_offload_lock() {\n''' + body + '''\n}\nacquire_offload_lock\n'''
            # Hold the same lock from another process; the second acquisition
            # must return non-zero before touching any offload work.
            env = {
                **os.environ,
                "TEST_SRC": str(root),
                "TEST_LOG": str(log),
                "TEST_HOME": str(root),
                "TEST_LOCK_DIR": str(lock_dir),
            }
            holder = subprocess.Popen(
                ["bash", "-c", 'exec 8>"$TEST_LOCK_DIR/drive-offload.lock"; flock -n 8; sleep 5'],
                env=env,
            )
            try:
                import time
                time.sleep(0.1)
                result = subprocess.run(["bash", "-c", harness], env=env, text=True, capture_output=True)
            finally:
                holder.terminate()
                holder.wait(timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("another run holds the lock", log.read_text())

    def test_symlink_lock_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            lock_dir = root / "lock-dir"
            lock_dir.mkdir()
            victim = root / "victim"
            victim.write_text("must not be truncated")
            (lock_dir / "drive-offload.lock").symlink_to(victim)
            source = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source.split("acquire_offload_lock() {", 1)[1].split("\n}", 1)[0]
            harness = 'SRC="$TEST_SRC"\nLOG="$TEST_LOG"\nHOME="$TEST_HOME"\nOFFLOAD_LOCK_DIR="$TEST_LOCK_DIR"\nacquire_offload_lock() {\n' + body + '\n}\nacquire_offload_lock\n'
            result = subprocess.run(
                ["bash", "-c", harness],
                env={**os.environ, "TEST_SRC": str(root), "TEST_LOG": str(root / "logs/offload.log"), "TEST_HOME": str(root), "TEST_LOCK_DIR": str(lock_dir)},
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(victim.read_text(), "must not be truncated")

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

    def test_register_verified_source_honours_tiered_endpoint(self):
        """The offloader must record the endpoint it actually copied to."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source"
            media = src / "generated" / "campaigns" / "clip.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"tiered bytes")
            db = root / "db.sqlite"
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE storage_backends (id INTEGER PRIMARY KEY, provider TEXT, bucket TEXT, endpoint TEXT, enabled INTEGER)")
                conn.execute("INSERT INTO storage_backends VALUES (9,'rclone','drive','sau/inbox/generated',1)")
                conn.execute("CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT, file_path TEXT, filesize INTEGER, storage_key TEXT, storage_backend_id INTEGER)")
                conn.execute("INSERT INTO file_records (filename,file_path,filesize) VALUES (?,?,?)", ("clip.mp4", "generated/campaigns/clip.mp4", media.stat().st_size))
            source_text = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source_text.split("register_verified_source() {", 1)[1].split("\n}", 1)[0]
            script = 'DB="$TEST_DB"\nDST=drive:sau\nREGISTER_ENDPOINT=sau/inbox/generated\nregister_verified_source() {\n' + body + '\n}\nregister_verified_source "$TEST_SOURCE_ROOT" campaigns/clip.mp4\n'
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "TEST_DB": str(db), "TEST_SOURCE_ROOT": str(src / "generated")}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute("SELECT storage_key,storage_backend_id FROM file_records").fetchone(),
                    ("campaigns/clip.mp4", 9),
                )

    def test_build_route_plan_splits_published_from_inbox(self):
        """Published records and never-published records get separate lists."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            import sqlite3
            import sys as _sys

            repo = Path(__file__).resolve().parents[1]
            if str(repo) not in _sys.path:
                _sys.path.insert(0, str(repo))
            import db.createTable as create_table

            create_table.bootstrap(db)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO storage_backends (slug,label,provider,bucket,region,endpoint,access_key,secret_key,enabled) "
                    "VALUES ('legacy','L','rclone','drive','','sau/videoFile','','',1)"
                )
                backend_id = conn.execute(
                    "SELECT id FROM storage_backends WHERE endpoint='sau/videoFile'"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) VALUES (?,?,?,?,?)",
                    ("pub.mp4", 1, "videoFile/pub.mp4", "pub.mp4", backend_id),
                )
                published_id = conn.execute("SELECT id FROM file_records").fetchone()[0]
                conn.execute(
                    "INSERT INTO file_records (filename,filesize,file_path,storage_key,storage_backend_id) VALUES (?,?,?,?,?)",
                    ("new.mp4", 1, "videoFile/new.mp4", "new.mp4", backend_id),
                )
                conn.execute(
                    "INSERT INTO publish_jobs (idempotency_key,platform,status,payload_json) VALUES (?,?,?,?)",
                    ("k", "tiktok", "succeeded", '{"artifacts":[{"source_file_record_id":%d}]}' % published_id),
                )
                job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO publish_job_targets (job_id,account_ref,file_ref,status) VALUES (?,?,?,?)",
                    (job_id, "acct", "campaign_post:1", "succeeded"),
                )
            excludes = root / "excludes"
            excludes.write_text("/_library/keep.mp4\n")
            plan_dir = root / "plan"
            source_text = (Path(__file__).resolve().parents[1] / "offload_to_drive.sh").read_text()
            body = source_text.split("build_route_plan() {", 1)[1].split("\n}", 1)[0]
            script = (
                'DB="$TEST_DB"\nPLAN_DIR="$TEST_PLAN"\nSRC="$TEST_SRC"\nEXCLUDES="$TEST_EXCLUDES"\n'
                "build_route_plan() {\n" + body + "\n}\nbuild_route_plan\n"
            )
            result = subprocess.run(
                ["bash", "-c", script],
                env={
                    **os.environ,
                    "TEST_DB": str(db),
                    "TEST_PLAN": str(plan_dir),
                    "TEST_SRC": str(repo),
                    "TEST_EXCLUDES": str(excludes),
                },
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("# route_plan", result.stdout)
            self.assertEqual((plan_dir / "videoFile.published").read_text().split(), ["pub.mp4"])
            self.assertEqual((plan_dir / "videoFile.inbox").read_text().split(), ["new.mp4"])


if __name__ == "__main__":
    unittest.main()
