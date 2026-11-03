"""Release safety contracts; no Docker daemon, credentials or live DB required."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import runpy
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from scripts import release_checks, release_probe, release_recovery, sqlite_backup

ROOT = Path(__file__).resolve().parents[1]


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE entries(value TEXT)")
        conn.execute("INSERT INTO entries VALUES ('original')")
        conn.commit()
    finally:
        conn.close()


def values(path: Path) -> list[str]:
    with sqlite_backup.read_connection(path) as conn:
        return [row[0] for row in conn.execute("SELECT value FROM entries ORDER BY rowid")]


def test_online_snapshot_includes_committed_wal_not_pending_transaction(tmp_path):
    source, backup = tmp_path / "live.db", tmp_path / "snapshot.db"
    make_db(source)
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO entries VALUES ('committed WAL')")
        writer.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        before = source.read_bytes()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO entries VALUES ('uncommitted')")
        result = sqlite_backup.snapshot(source, backup)
        assert source.read_bytes() == before
        assert values(backup) == ["original", "committed WAL"]
        assert result == sqlite_backup.verify_snapshot(backup)
        assert sqlite_backup.verify_restore(backup, temp_parent=tmp_path)["restore_verified"] is True
        assert not Path(str(backup) + "-wal").exists()
        if os.name == "posix":
            assert backup.stat().st_mode & 0o077 == 0
            assert sqlite_backup.checksum_path(backup).stat().st_mode & 0o077 == 0
    finally:
        writer.rollback()
        writer.close()


def test_read_connection_cannot_write_or_create_source(tmp_path):
    source = tmp_path / "source.db"
    make_db(source)
    with sqlite_backup.read_connection(source) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO entries VALUES ('unsafe')")
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite_backup.BackupError):
        with sqlite_backup.read_connection(missing):
            pass
    assert not missing.exists()


@pytest.mark.parametrize("kind", ["missing", "empty", "corrupt"])
def test_snapshot_fails_closed_for_bad_source(tmp_path, kind):
    source, target = tmp_path / "bad.db", tmp_path / "backup.db"
    if kind != "missing":
        source.write_bytes(b"" if kind == "empty" else b"not SQLite" * 1000)
    with pytest.raises((sqlite_backup.BackupError, sqlite3.DatabaseError)):
        sqlite_backup.snapshot(source, target)
    assert not target.exists()
    assert not sqlite_backup.checksum_path(target).exists()
    assert not list(tmp_path.glob(".sqlite-*"))


def test_failed_verification_does_not_publish_partial_snapshot(tmp_path, monkeypatch):
    source, target = tmp_path / "source.db", tmp_path / "backup.db"
    make_db(source)
    def reject(_conn):
        raise sqlite_backup.BackupError("injected quick_check failure")
    monkeypatch.setattr(sqlite_backup, "quick_check", reject)
    with pytest.raises(sqlite_backup.BackupError):
        sqlite_backup.snapshot(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".sqlite-*"))


def test_snapshot_never_clobbers_existing_destination(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "unknown.db"
    make_db(source)
    target.write_bytes(b"unknown local work")
    with pytest.raises(sqlite_backup.BackupError, match="already exists"):
        sqlite_backup.snapshot(source, target)
    assert target.read_bytes() == b"unknown local work"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal", ".sha256"])
def test_restore_refuses_unsafe_destination_sidecars(tmp_path, suffix):
    source, backup, target = (tmp_path / name for name in ("source.db", "backup.db", "target.db"))
    make_db(source)
    sqlite_backup.snapshot(source, backup)
    marker = Path(str(target) + suffix)
    marker.write_bytes(b"do not discard")
    with pytest.raises(sqlite_backup.BackupError):
        sqlite_backup.restore(backup, target, offline=True)
    assert marker.read_bytes() == b"do not discard"
    assert not target.exists()


def test_restore_refuses_active_existing_db_even_with_offline_flag(tmp_path):
    source, backup, live = (tmp_path / name for name in ("source.db", "backup.db", "live.db"))
    make_db(source)
    sqlite_backup.snapshot(source, backup)
    make_db(live)
    writer = sqlite3.connect(live)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite_backup.BackupError, match="already exists"):
            sqlite_backup.restore(backup, live, offline=True)
    finally:
        writer.rollback()
        writer.close()
    assert values(live) == ["original"]


def test_restore_requires_offline_and_dry_run_is_isolated(tmp_path):
    source, backup, destination = (tmp_path / name for name in ("source.db", "backup.db", "restored.db"))
    make_db(source)
    sqlite_backup.snapshot(source, backup)
    with pytest.raises(sqlite_backup.BackupError, match="offline"):
        sqlite_backup.restore(backup, destination)
    assert sqlite_backup.restore(backup, destination, dry_run=True)["restore_verified"]
    assert not destination.exists()
    sqlite_backup.restore(backup, destination, offline=True)
    assert values(destination) == ["original"]
    assert sqlite_backup.verify_snapshot(destination)["quick_check"] == "ok"


def test_restore_rejects_checksum_mismatch(tmp_path):
    source, backup = tmp_path / "source.db", tmp_path / "backup.db"
    make_db(source)
    sqlite_backup.snapshot(source, backup)
    with backup.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(sqlite_backup.BackupError, match="SHA256 mismatch"):
        sqlite_backup.restore(backup, tmp_path / "restore.db", offline=True)
    assert not (tmp_path / "restore.db").exists()


def test_drain_does_not_expire_away_unknown_active_work(tmp_path):
    path = tmp_path / "activity.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE publish_job_targets(status TEXT, lease_token TEXT);"
                           "CREATE TABLE campaigns(status TEXT, metadata_json TEXT);")
        conn.execute("INSERT INTO publish_job_targets VALUES ('retrying', NULL)")
        conn.execute("INSERT INTO campaigns VALUES ('preparing', ?)",
                     (json.dumps({"prepRequest": {"title": "queued"}}),))
        conn.commit()
        assert sqlite_backup.activity(path)["drained"] is True
        assert sqlite_backup.activity(path, legacy=True)["drained"] is False
        conn.execute("INSERT INTO campaigns VALUES ('preparing', ?)",
                     (json.dumps({"_prepLease": {"owner": "lost", "claimedAt": "1999-01-01"}}),))
        conn.commit()
        assert sqlite_backup.activity(path)["active_preps"] == 1
        conn.execute("INSERT INTO publish_job_targets VALUES ('running', 'token')")
        conn.commit()
        assert sqlite_backup.activity(path)["active_targets"] == 1
    finally:
        conn.close()
    with pytest.raises(sqlite_backup.BackupError):
        sqlite_backup.activity(tmp_path / "missing.db")


def test_heartbeat_probe_is_read_only_and_uses_public_worker_health(tmp_path, monkeypatch):
    from myUtils import jobs
    path = tmp_path / "heartbeat.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE worker_heartbeats(worker_id TEXT, heartbeat_at TEXT, role TEXT, draining INTEGER)")
        conn.execute("INSERT INTO worker_heartbeats VALUES (?, ?, 'publisher', 0)",
                     ("7-current", datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))
        conn.commit()
    finally:
        conn.close()
    before = path.read_bytes()
    def forbidden_rw(_path=None):
        pytest.fail("legacy read/write connector must not be used by the release probe")
    monkeypatch.setattr(jobs, "_connect", forbidden_rw)
    assert release_probe.worker_snapshot(path)["ok"] is True
    assert path.read_bytes() == before
    assert jobs._connect is forbidden_rw  # adapter is restored after the probe


def test_paused_heartbeat_is_not_normal_readiness_and_old_worker_cannot_pass(tmp_path, monkeypatch):
    from myUtils import health_checks
    monkeypatch.delenv("SAU_PAUSE_CLAIMS_FILE", raising=False)
    monkeypatch.setattr(health_checks, "database_ready", lambda _path: ("ok", True))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(release_probe, "worker_process", lambda: ("7", (now - timedelta(seconds=5)).timestamp()))
    worker = dict(worker_id="7-current", heartbeat_at=now.isoformat(), role="publisher", draining=True, alive=True)
    view = {"ok": False, "workers": [worker]}
    monkeypatch.setattr(release_probe, "worker_snapshot", lambda _path: view)
    pause, db = tmp_path / "publish.pause", tmp_path / "db.db"
    pause.touch()
    assert release_probe.worker_ready(db, allow_paused=True)
    assert not release_probe.worker_ready(db, allow_paused=False)
    pause.unlink()
    assert not release_probe.worker_ready(db, allow_paused=False)
    worker["draining"] = False
    view["ok"] = True
    assert release_probe.worker_ready(db, allow_paused=False)
    worker["heartbeat_at"] = (now - timedelta(seconds=10)).isoformat()
    assert not release_probe.worker_ready(db, allow_paused=False)


def test_paused_web_gate_only_tolerates_worker_not_ready(monkeypatch):
    class Response(io.BytesIO):
        code = 503
    body = {"revision": "a" * 40, "status": "not_ready", "checks": {"database": "ok", "worker": "worker_unavailable"}}
    monkeypatch.setattr(release_probe, "urlopen", lambda *_a, **_k: Response(json.dumps(body).encode()))
    assert release_probe.web_ready("a" * 40, allow_paused=True)
    assert not release_probe.web_ready("a" * 40, allow_paused=False)
    body["checks"]["database"] = "migration_required"
    assert not release_probe.web_ready("a" * 40, allow_paused=True)
    body["checks"] = {"database": "ok", "worker": "error"}
    assert not release_probe.web_ready("a" * 40, allow_paused=True)


def test_wsgi_selects_web_role_before_preserved_factory(monkeypatch):
    app = object()
    def create_app():
        assert os.environ["SAU_PROCESS_ROLE"] == "web"
        return app
    monkeypatch.setenv("SAU_PROCESS_ROLE", "embedded")
    monkeypatch.setitem(sys.modules, "sau_app", SimpleNamespace(create_app=create_app))
    assert runpy.run_path(str(ROOT / "wsgi.py"))["app"] is app


def test_clean_revision_refuses_unknown_work_and_wrong_commit(monkeypatch):
    revision = "a" * 40
    monkeypatch.setattr(release_checks, "git", lambda _root, *args: revision if args[0] == "rev-parse" else "?? unknown.py")
    with pytest.raises(ValueError, match="dirty"):
        release_checks.clean_revision(ROOT, revision)
    with pytest.raises(ValueError, match="tested revision"):
        release_checks.clean_revision(ROOT, "b" * 40)


def test_receipts_bind_all_gates_to_revision_locks_and_image(tmp_path, monkeypatch):
    revision = "a" * 40
    monkeypatch.setattr(release_checks, "git", lambda _root, *args: (revision if args[-1] == "HEAD" else "b" * 40) if args[0] == "rev-parse" else "")
    for name in release_checks.LOCKS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("locked", encoding="utf-8")
    directory = tmp_path / "receipts"
    identity = release_checks.identity(tmp_path, revision)
    for component in release_checks.COMPONENTS:
        checks = (["compile", "undefined-names", "backend-tests-including-security"] if component.startswith("backend-")
                  else ["npm-ci", "frontend-tests", "frontend-build"])
        release_checks.write_json(directory / f"{component}.json",
                                  dict(identity, component=component, checks=checks, passed=True, platform="linux"))
    image_id = "sha256:" + "c" * 64
    release_checks.record_build(tmp_path, revision, directory, image_id)
    release_checks.verify_receipts(tmp_path, revision, directory, image_id)
    with pytest.raises(ValueError, match="image"):
        release_checks.verify_receipts(tmp_path, revision, directory, "sha256:" + "d" * 64)
    (tmp_path / "uv.lock").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="different source/lock"):
        release_checks.verify_receipts(tmp_path, revision, directory)


def test_compose_roles_share_image_data_env_and_preserve_other_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    web, worker = (compose["services"][name] for name in ("social-auto-upload", "worker"))
    for key in ("image", "volumes", "networks", "env_file", "user", "logging", "init", "stop_grace_period"):
        assert web[key] == worker[key]
    for service, role in ((web, "web"), (worker, "worker")):
        assert service["environment"]["SAU_PROCESS_ROLE"] == role
        assert service["environment"]["SAU_DB_PATH"] == "/app/db/database.db"
        assert service["environment"]["SAU_PAUSE_CLAIMS_FILE"] == "/app/db/publish.pause"
        assert service["init"] is True
        assert service["pull_policy"] == "never"
        assert service["labels"]["com.centurylinklabs.watchtower.enable"] == "false"
    assert web["environment"]["SAU_REQUIRE_WORKER_HEALTH"] == "1"
    assert "/readyz" in " ".join(web["healthcheck"]["test"])
    assert worker["command"] == ["python", "-m", "myUtils.worker", "--max-concurrent", "3"]
    assert worker["ports"] == []
    assert worker["healthcheck"]["test"][-2:] == ["scripts/release_probe.py", "worker"]
    assert compose["services"]["stt"]["volumes"] == ["sau-stt-cache:/cache"]
    assert compose["services"]["postgres"]["profiles"] == ["dev"]
    assert compose["networks"]["1panel-network"]["external"] is True
    assert "./rclone-cache.conf:/app/rclone-cache.conf" in web["volumes"]


def test_portable_migration_and_pause_preflight():
    deploy = (ROOT / "scripts/deploy-local.sh").read_text()
    migration = next(line for line in deploy.splitlines() if '"${COMPOSE[@]}" run ' in line)
    assert "--pull" not in migration
    assert "--rm --no-deps --entrypoint python" in migration
    assert "social-auto-upload -m myUtils.db_bootstrap" in migration
    policy = "assert web.get('pull_policy') == worker.get('pull_policy') == 'never'"
    ignored = 'git -C "$RUNTIME" check-ignore -q -- db/publish.pause'
    pause = deploy.index("(set -o noclobber;", deploy.index("Existing pause/staged release requires"))
    assert deploy.index(policy) < pause
    assert deploy.index(ignored) < pause


@pytest.mark.parametrize("rule", ["tracked", "local-exclude", "missing"])
def test_pause_marker_ignore_is_explicit_and_preserves_other_changes(tmp_path, rule):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
    empty_excludes = tmp_path / "empty-excludes"
    empty_excludes.write_text("")

    def git(*args):
        return subprocess.run(
            ["git", "-c", f"core.excludesFile={empty_excludes}", "-C", str(repo), *args],
            capture_output=True, text=True, check=False,
        )

    if rule == "tracked":
        (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    elif rule == "local-exclude":
        (repo / ".git/info/exclude").write_text("/db/publish.pause\n")
    expected = 1 if rule == "missing" else 0
    assert git("check-ignore", "-q", "--", "db/publish.pause").returncode == expected
    (repo / "db").mkdir()
    (repo / "db/publish.pause").write_text("a" * 40 + "\n")
    (repo / "operator-change.txt").write_text("preserve this untracked work\n")
    assert git("check-ignore", "-q", "--", "db/publish.pause").returncode == expected
    status = git("status", "--porcelain", "--untracked-files=all")
    assert status.returncode == 0 and "?? operator-change.txt" in status.stdout
    assert ("?? db/publish.pause" in status.stdout) == (rule == "missing")


def test_release_static_invariants():
    dockerfile = (ROOT / "Dockerfile").read_text()
    gates = (ROOT / "scripts/release-gates.sh").read_text()
    deploy = (ROOT / "scripts/deploy-local.sh").read_text()
    watcher = (ROOT / "scripts/ci-watch.sh").read_text()
    restore = (ROOT / "scripts/restore.sh").read_text()
    assert "uv export --frozen" in dockerfile and "--require-hashes" in dockerfile
    assert "pip install" not in dockerfile and "requirements.txt" not in dockerfile
    # Python 3.10 is end-of-life; the shipped runtime is asserted explicitly.
    assert "sys.version_info[:3] == (3, 12, 14)" in dockerfile
    assert re.search(r"^FROM ghcr\.io/willywang8216/sau-base:slim@sha256:[a-f0-9]{64}$", dockerfile, re.M)
    assert "COPY db/createTable.py ./myUtils/db_bootstrap.py" in dockerfile
    assert dockerfile.index("chmod -R u=rwX,go=rX /app") > dockerfile.index("COPY --from=builder /app/dist/vite.svg")
    assert "--user 65534:65534" in deploy and "--tmpfs /app/logs:rw,nosuid,nodev,mode=1777" in deploy
    assert "assert os.getuid() != 0" in deploy
    assert "--tmpfs /app/cookies:rw,nosuid,nodev,mode=1777" in deploy
    assert 'ENV SAU_BUILD_REVISION=${SAU_BUILD_REVISION}' in dockerfile
    assert 'org.opencontainers.image.revision="${SAU_BUILD_REVISION}"' in dockerfile
    assert "sau_frontend/package-lock.json" not in (ROOT / "Dockerfile.dockerignore").read_text().splitlines()
    assert "uv sync --frozen" in gates and "-m pytest tests/ -q" in gates and "--ignore" not in gates
    assert "run_clean npm ci" in gates and "run_clean npm test" in gates and "run_clean npm run build" in gates
    assert "env -i" in gates and "backend 3.12; backend 3.13" in gates
    assert {"uploader", "mcp_server", "sau_app"}.issubset(release_checks.MAINTAINED_DIRS)
    assert "ci-watch.lock" in watcher and "ci-watch.lock" in deploy and "ci-watch.lock" in restore
    assert "SAU_CI_WATCH_FORCE_DEPLOY" not in watcher and "MAX_DEFER" not in watcher
    assert "git reset" not in deploy and "--ff-only" in deploy
    assert "--signal=TERM" in deploy and "--signal=KILL" not in deploy
    assert '"$BACKUP" restore' not in deploy
    assert '"$BACKUP" snapshot' in deploy and "--restore-check" in deploy
    assert "--pull never --no-deps social-auto-upload worker" in deploy
    assert deploy.index("wait_drained || fail") < deploy.index("social-auto-upload -m myUtils.db_bootstrap")
    assert deploy.index("social-auto-upload -m myUtils.db_bootstrap") < deploy.index("wait_ready paused || fail 'Paused")
    assert deploy.index('wait_ready active "$ACTIVE_SINCE"') < deploy.index('mv "$LOG_DIR/.ci-watch.state.$$"')
    assert "assert_offline" in restore and "--offline" in restore and "fuser" in restore
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    # CI must exercise the same two versions the release gates do.
    assert ci["jobs"]["backend-tests"]["strategy"]["matrix"]["python"] == ["3.12", "3.13"]
    image = yaml.safe_load((ROOT / ".github/workflows/image.yml").read_text())
    assert image["jobs"]["gates"]["uses"] == "./.github/workflows/ci.yml"
    assert image["jobs"]["build-and-push"]["needs"] == "gates"


def test_atomic_snapshot_publication_does_not_overwrite_a_racing_file(tmp_path, monkeypatch):
    source, target = tmp_path / "source.db", tmp_path / "backup.db"
    make_db(source)
    real_link = os.link
    def raced_link(src, dst):
        if Path(dst) == target:
            target.write_bytes(b"concurrent unknown file")
        real_link(src, dst)
    monkeypatch.setattr(sqlite_backup.os, "link", raced_link)
    with pytest.raises(FileExistsError):
        sqlite_backup.snapshot(source, target)
    assert target.read_bytes() == b"concurrent unknown file"
    assert not sqlite_backup.checksum_path(target).exists()


def test_worker_probe_ignores_tini_wrapper(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "uptime").write_text("100.0 0.0")
    for pid, args in (("1", b"/sbin/docker-init\0--\0python\0-m\0myUtils.worker\0"),
                      ("7", b"python\0-m\0myUtils.worker\0--max-concurrent\0" b"3\0")):
        directory = proc / pid
        directory.mkdir()
        (directory / "cmdline").write_bytes(args)
        fields = ["S"] + ["0"] * 18 + ["1000"]
        (directory / "stat").write_text(f"{pid} (python) " + " ".join(fields))
    def proc_path(value):
        text = str(value)
        return proc / text.removeprefix("/proc/") if text.startswith("/proc/") else proc
    monkeypatch.setattr(release_probe, "Path", proc_path)
    monkeypatch.setattr(release_probe.os, "sysconf", lambda _key: 100, raising=False)
    assert release_probe.worker_process()[0] == "7"


def test_revision_and_timestamp_probe_contract(monkeypatch):
    expected = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc).timestamp()
    assert release_probe.utc_timestamp("2026-01-01T00:00:00.123456789Z") == expected
    assert release_probe.utc_timestamp("2025-12-31T19:00:00.123456-05:00") == expected
    monkeypatch.setenv("SAU_BUILD_REVISION", "a" * 40)
    monkeypatch.setenv("SAU_PROCESS_ROLE", "worker")
    assert release_probe.main(["worker", "--revision", "b" * 40]) == 1


def test_embedded_release_python_snippets_compile():
    for name in ("deploy-local.sh", "restore.sh"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        snippets = re.findall(r"<<'PY'\n(.*?)\nPY", source, flags=re.DOTALL)
        assert snippets
        for index, snippet in enumerate(snippets):
            compile(snippet, f"{name}:heredoc-{index}", "exec")
    deploy = (ROOT / "scripts/deploy-local.sh").read_text(encoding="utf-8")
    assert "value.replace('$', '$$')" in deploy


_CANDIDATE_IMAGE = "sha256:" + "a" * 64
_PREVIOUS_IMAGE = "sha256:" + "b" * 64
_OTHER_IMAGE = "sha256:" + "c" * 64


@pytest.fixture
def recovery_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "baseline").mkdir(parents=True, mode=0o700)
    runtime = tmp_path / "runtime"
    (runtime / "db").mkdir(parents=True)
    selected = runtime / "selected-conf.py"
    selected.write_bytes(b"# preserve the selected configuration\n")
    (runtime / "conf.py").write_bytes(b"# not the selected configuration\n")
    (runtime / ".env").write_text("SYNTHETIC_SECRET=fixture-only\n", encoding="utf-8")
    web = {
        "image": _CANDIDATE_IMAGE, "init": True, "pull_policy": "never",
        "volumes": [
            {"type": "bind", "source": str(runtime / "db"), "target": "/app/db"},
            {"type": "bind", "source": str(selected), "target": "/app/conf.py"},
        ],
        "environment": {"SAU_PROCESS_ROLE": "web", "SAU_REQUIRE_WORKER_HEALTH": "1",
                        "SAU_DB_PATH": "/app/db/database.db", "SAU_PAUSE_CLAIMS_FILE": "/app/db/publish.pause"},
    }
    worker = dict(web, environment=dict(web["environment"], SAU_PROCESS_ROLE="worker", SAU_REQUIRE_WORKER_HEALTH="0"),
                  command=["python", "-m", "myUtils.worker", "--max-concurrent", "3"])
    candidate = {"services": {"social-auto-upload": web, "worker": worker}}
    previous = {"services": {"social-auto-upload": {"image": _PREVIOUS_IMAGE},
                              "worker": {"image": _PREVIOUS_IMAGE}}}
    for name, value in (("candidate.compose.json", candidate), ("baseline/compose.json", previous),
                        ("baseline/images.json", previous)):
        (bundle / name).write_text(json.dumps(value), encoding="utf-8")
    for name, value in (("web-image", _PREVIOUS_IMAGE), ("worker-image", _PREVIOUS_IMAGE),
                        ("git-revision", "d" * 40), (".env", "SYNTHETIC_SECRET=fixture-only"),
                        ("conf.py", selected.read_text(encoding="utf-8"))):
        (bundle / "baseline" / name).write_text(value + "\n", encoding="utf-8")
    source = tmp_path / "source.db"
    make_db(source)
    sqlite_backup.snapshot(source, bundle / "database.db")
    return SimpleNamespace(path=bundle, runtime=runtime, selected=selected)


@pytest.mark.parametrize("legacy", [False, True])
def test_recovery_seal_and_isolated_verification_preserve_bundle(recovery_bundle, monkeypatch, legacy):
    bundle = recovery_bundle.path
    previous_worker = "" if legacy else _PREVIOUS_IMAGE
    if legacy:
        (bundle / "baseline/worker-image").write_text("", encoding="ascii")
        (bundle / "baseline/images.json").write_text(
            json.dumps({"services": {"social-auto-upload": {"image": _PREVIOUS_IMAGE}}}), encoding="utf-8")
    expected = dict(candidate_image=_CANDIDATE_IMAGE, previous_web=_PREVIOUS_IMAGE, previous_worker=previous_worker)
    release_recovery.seal(bundle, **expected)
    manifest = bundle / release_recovery.MANIFEST
    before = {name: (bundle / name).read_bytes() for name in (*release_recovery.REQUIRED_FILES, release_recovery.MANIFEST)}
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    assert set(metadata["files"]) == set(release_recovery.REQUIRED_FILES)
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in metadata["files"].values())
    assert "SYNTHETIC_SECRET" not in manifest.read_text(encoding="utf-8")
    original_verify = sqlite_backup.verify_restore
    calls = []
    def verify_inside_bundle(source, *, temp_parent):
        calls.append((source, temp_parent))
        return original_verify(source, temp_parent=temp_parent)
    monkeypatch.setattr(sqlite_backup, "verify_restore", verify_inside_bundle)
    assert release_recovery.verify(bundle, **expected) == expected
    assert calls == [(bundle / "database.db", bundle)]
    assert all((bundle / name).read_bytes() == value for name, value in before.items())
    assert not list(bundle.glob("sau-restore-check-*"))
    if os.name == "posix":
        assert manifest.stat().st_mode & 0o077 == 0
    with pytest.raises(release_recovery.RecoveryError, match="already sealed"):
        release_recovery.seal(bundle)


@pytest.mark.parametrize("name", release_recovery.REQUIRED_FILES)
@pytest.mark.parametrize("change", ["missing", "corrupt"])
def test_recovery_rejects_missing_or_changed_required_artifacts(recovery_bundle, name, change):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    manifest_before = (bundle / release_recovery.MANIFEST).read_bytes()
    artifact = bundle / name
    if change == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"accidental corruption\n")
    with pytest.raises((release_recovery.RecoveryError, OSError)):
        release_recovery.verify(bundle, candidate_image=_CANDIDATE_IMAGE)
    assert (bundle / release_recovery.MANIFEST).read_bytes() == manifest_before


@pytest.mark.parametrize("change", ["missing", "corrupt"])
def test_recovery_requires_original_manifest(recovery_bundle, change):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    manifest = bundle / release_recovery.MANIFEST
    if change == "missing":
        manifest.unlink()
    else:
        manifest.write_text("not JSON", encoding="utf-8")
    with pytest.raises((OSError, ValueError)):
        release_recovery.verify(bundle)
    assert not manifest.exists() if change == "missing" else manifest.read_text() == "not JSON"


@pytest.mark.parametrize("field", ["candidate_image", "previous_web", "previous_worker"])
def test_recovery_rejects_wrong_expected_image_ids(recovery_bundle, field):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    with pytest.raises(release_recovery.RecoveryError, match="expected release"):
        release_recovery.verify(bundle, **{field: _OTHER_IMAGE})


def _update_fixture_artifact_hash(bundle, name):
    # Simulate a consistently hashed but semantically invalid bundle, so these
    # tests exercise JSON/ID/SQLite validation rather than just the outer hash.
    path = bundle / release_recovery.MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][name] = sqlite_backup.sha256(bundle / name)
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("name", ["candidate.compose.json", "baseline/compose.json", "baseline/images.json"])
@pytest.mark.parametrize("bad_json", [True, False])
def test_recovery_revalidates_json_and_image_consistency(recovery_bundle, name, bad_json):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    path = bundle / name
    if bad_json:
        path.write_text("{invalid", encoding="utf-8")
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["services"]["worker"]["image"] = _OTHER_IMAGE
        path.write_text(json.dumps(value), encoding="utf-8")
    _update_fixture_artifact_hash(bundle, name)
    with pytest.raises(ValueError):
        release_recovery.verify(bundle, candidate_image=_CANDIDATE_IMAGE)


def test_recovery_rechecks_sqlite_checksum_sidecar(recovery_bundle):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    (bundle / "database.db.sha256").write_text("0" * 64 + "  database.db\n", encoding="ascii")
    _update_fixture_artifact_hash(bundle, "database.db.sha256")
    with pytest.raises(sqlite_backup.BackupError, match="SHA256 mismatch"):
        release_recovery.verify(bundle)


@pytest.mark.parametrize("path_kind", ["relative", "absolute"])
def test_recovery_rejects_unexpected_paths_before_reading_them(recovery_bundle, monkeypatch, path_kind):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    path = bundle / release_recovery.MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    unexpected = "../outside.env" if path_kind == "relative" else str(bundle.parent / "outside.env")
    manifest["files"][unexpected] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(sqlite_backup, "sha256", lambda _path: pytest.fail("must reject path set before file access"))
    with pytest.raises(release_recovery.RecoveryError, match="artifact paths"):
        release_recovery.verify(bundle)


def test_recovery_artifacts_must_be_regular_nonsymlink_files(recovery_bundle):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    artifact = bundle / "baseline/conf.py"
    artifact.unlink()
    artifact.mkdir()
    with pytest.raises(release_recovery.RecoveryError, match="regular file"):
        release_recovery.verify(bundle)
    artifact.rmdir()
    try:
        artifact.symlink_to(recovery_bundle.selected)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    with pytest.raises(release_recovery.RecoveryError, match="nonsymlink"):
        release_recovery.verify(bundle)


def test_recovery_cli_errors_do_not_expose_config(recovery_bundle, capsys):
    bundle = recovery_bundle.path
    release_recovery.seal(bundle)
    (bundle / "baseline/compose.json").write_text("SYNTHETIC_SECRET=do-not-print", encoding="utf-8")
    assert release_recovery.main(["verify", str(bundle), "--candidate-image", _CANDIDATE_IMAGE]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Recovery bundle verification failed; keep the candidate paused\n"


def _config_preflight_snippet():
    source = (ROOT / "scripts/deploy-local.sh").read_text(encoding="utf-8")
    return next(snippet for snippet in re.findall(r"<<'PY'\n(.*?)\nPY", source, flags=re.DOTALL)
                if "conf_source = require_config_file" in snippet)


@pytest.mark.parametrize("service", ["social-auto-upload", "worker"])
@pytest.mark.parametrize("policy", [None, "always", "missing"])
def test_config_preflight_rejects_unsafe_pull_policy(recovery_bundle, monkeypatch, service, policy):
    bundle, runtime = recovery_bundle.path, recovery_bundle.runtime
    config_path = bundle / "candidate.compose.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if policy == "missing":
        config["services"][service].pop("pull_policy")
    else:
        config["services"][service]["pull_policy"] = policy
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (bundle / "baseline/conf.py").unlink()
    (bundle / "baseline/images.json").unlink()
    monkeypatch.setattr(sys, "argv", ["-", str(bundle), str(runtime), _CANDIDATE_IMAGE, _PREVIOUS_IMAGE, _PREVIOUS_IMAGE])
    with pytest.raises(AssertionError):
        exec(compile(_config_preflight_snippet(), "deploy-local.sh:config-preflight", "exec"), {})
    assert not (runtime / "db/publish.pause").exists()
    assert not (bundle / "baseline/conf.py").exists()
    assert not (bundle / "baseline/images.json").exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_config_bind_preflight_fails_before_pause(recovery_bundle, monkeypatch, kind):
    bundle, runtime, selected = recovery_bundle.path, recovery_bundle.runtime, recovery_bundle.selected
    (bundle / "baseline/conf.py").unlink()
    (bundle / "baseline/images.json").unlink()  # created only after config preflight
    if kind in {"missing", "directory"}:
        selected.unlink()
        if kind == "directory":
            selected.mkdir()
    else:
        original_open = Path.open
        def unreadable(path, *args, **kwargs):
            if path == selected:
                raise PermissionError("synthetic permission denied")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", unreadable)
    monkeypatch.setattr(sys, "argv", ["-", str(bundle), str(runtime), _CANDIDATE_IMAGE, _PREVIOUS_IMAGE, _PREVIOUS_IMAGE])
    with pytest.raises(SystemExit, match="readable regular file"):
        exec(compile(_config_preflight_snippet(), "deploy-local.sh:config-preflight", "exec"), {})
    assert not (runtime / "db/publish.pause").exists()
    assert not (bundle / "baseline/conf.py").exists()
    assert not (bundle / "baseline/images.json").exists()
    assert (runtime / "conf.py").read_bytes() == b"# not the selected configuration\n"


def test_config_preflight_preserves_selected_file(recovery_bundle, monkeypatch):
    bundle, runtime, selected = recovery_bundle.path, recovery_bundle.runtime, recovery_bundle.selected
    before = selected.read_bytes()
    monkeypatch.setattr(sys, "argv", ["-", str(bundle), str(runtime), _CANDIDATE_IMAGE, _PREVIOUS_IMAGE, _PREVIOUS_IMAGE])
    exec(compile(_config_preflight_snippet(), "deploy-local.sh:config-preflight", "exec"), {})
    assert (bundle / "baseline/conf.py").read_bytes() == selected.read_bytes() == before
    assert not (runtime / "db/publish.pause").exists()


def test_config_preflight_accepts_resolved_valid_symlink(tmp_path):
    selected, link = tmp_path / "selected.py", tmp_path / "conf-link.py"
    selected.write_bytes(b"# keep existing config\n")
    try:
        link.symlink_to(selected)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    assert release_recovery.require_config_file(link) == selected.resolve(strict=True)
    assert selected.read_bytes() == b"# keep existing config\n"


def test_resume_verification_and_config_preflight_precede_activation():
    source = (ROOT / "scripts/deploy-local.sh").read_text(encoding="utf-8")
    resume = source.split("if (( RESUME == 1 )); then", 1)[1].split("\nelse\n", 1)[0]
    assert resume.index('"$RECOVERY" verify') < resume.index("docker image inspect") < resume.index("PAUSE_OWNED=1 TOUCHED=1")
    assert '"$RECOVERY" seal' not in resume and 'rm "$PAUSE"' not in resume
    assert "|| fail 'Recovery bundle verification failed" in resume
    assert "|| fail 'Previous release image is unavailable" in resume
    normal = source.split('else\n  [[ ! -e "$PAUSE"', 1)[1]
    assert normal.index("conf_source = require_config_file") < normal.index('(set -o noclobber;') < normal.index('stop_gracefully "$WORKER"')
    assert normal.index('"$BACKUP" verify "$RELEASE_DIR/database.db" --restore-check') < normal.index('"$RECOVERY" seal')
    assert normal.index('"$RECOVERY" seal') < normal.index("social-auto-upload -m myUtils.db_bootstrap")
    assert source.index('"$RECOVERY" verify') < source.index('rm "$PAUSE"')
    assert '"$BACKUP" restore' not in source
