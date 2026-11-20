"""Explicit maintenance-only recovery, without inventing a held stage or refunding history."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import release_recovery, sqlite_backup

ROOT = Path(__file__).resolve().parents[1]
OLD = "a" * 40
NEW = "b" * 40
IMAGE = "sha256:" + "c" * 64
OTHER = "sha256:" + "d" * 64


def tree_bytes(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    for name in ("db", "logs", "backups/releases/current/baseline"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    bundle = runtime / "backups/releases/current"
    pause = runtime / "db/publish.pause"
    pause.write_bytes((OLD + "\n").encode())
    (runtime / ".env").write_text("SYNTHETIC_SECRET=fixture-only$literal$$value\n")
    (runtime / "conf.py").write_text("SYNTHETIC_CONFIG = 'fixture-only'\n")
    (runtime / "rclone-cache.conf").write_text("[fixture]\ntype = drive\nsynthetic = fixture-only\n")
    db = runtime / "db/database.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE history (target INTEGER, attempts INTEGER, checkpoint TEXT)")
        conn.execute("INSERT INTO history VALUES (10373, 8, ?)",
                     (json.dumps({"provider_budget": 0, "prerequisite_budget": 8,
                                  "delivery_state": "original deep receipt"}),))
    volumes = [{"type": "bind", "source": str(runtime / "db"), "target": "/app/db"},
               {"type": "bind", "source": str(runtime / "conf.py"), "target": "/app/conf.py"},
               {"type": "bind", "source": str(runtime / "rclone-cache.conf"),
                "target": "/app/rclone-cache.conf"}]
    services = {}
    objects = []
    for index, (name, role) in enumerate((("social-auto-upload", "web"), ("worker", "worker")), 1):
        env = {"SAU_PROCESS_ROLE": role, "SAU_BUILD_REVISION": OLD,
               "SAU_DB_PATH": "/app/db/database.db", "SAU_PAUSE_CLAIMS_FILE": "/app/db/publish.pause",
               "SYNTHETIC_SECRET": "fixture-only$literal$$value"}
        command = ["gunicorn", "wsgi:app"] if role == "web" else ["python", "-m", "myUtils.worker"]
        services[name] = {"image": IMAGE, "environment": {k: v.replace("$", "$$") for k, v in env.items()},
                          "volumes": copy.deepcopy(volumes), "command": command,
                          "entrypoint": None, "user": "1000:1000"}
        objects.append({"Id": str(index) * 64, "Image": IMAGE, "RestartCount": 0,
                        "State": {"Running": True, "OOMKilled": False},
                        "Config": {"Env": [f"{k}={v}" for k, v in env.items()], "Cmd": command,
                                   "Entrypoint": None, "User": "1000:1000",
                                   "Labels": {"org.opencontainers.image.revision": OLD}},
                        "Mounts": [{"Type": "bind", "Source": v["source"], "Destination": v["target"],
                                    "RW": True} for v in volumes], "HostConfig": {"PortBindings": {}}})
    for name in ("candidate.compose.json", "baseline/compose.json"):
        (bundle / name).write_text(json.dumps({"services": services}))
    (bundle / "baseline/images.json").write_text(json.dumps({"services": {
        "social-auto-upload": {"image": IMAGE}, "worker": {"image": IMAGE}}}))
    for name, text in (("web-image", IMAGE + "\n"), ("worker-image", IMAGE + "\n"),
                       ("git-revision", OLD + "\n")):
        (bundle / "baseline" / name).write_text(text)
    for name in (".env", "conf.py", "rclone-cache.conf"):
        (bundle / "baseline" / name).write_bytes((runtime / name).read_bytes())
    sqlite_backup.snapshot(db, bundle / "database.db")
    release_recovery.seal(bundle, candidate_image=IMAGE, previous_web=IMAGE, previous_worker=IMAGE)
    activity = {"drained": True, "active_targets": 0, "active_preps": 0}

    def inspect(args, **kwargs):
        assert args == ["docker", "inspect", "social-auto-upload", "social-auto-upload-worker"]
        assert kwargs["timeout"] == 20 and kwargs["stderr"] == subprocess.DEVNULL
        return json.dumps(copy.deepcopy(objects))

    monkeypatch.setattr(release_recovery.subprocess, "check_output", inspect)
    monkeypatch.setattr(release_recovery.sqlite_backup, "activity", lambda path: dict(activity))
    return SimpleNamespace(runtime=runtime, bundle=bundle, pause=pause, db=db,
                           objects=objects, activity=activity)


def new_destination(fixture):
    path = fixture.bundle.parent / "replacement"
    (path / "baseline").mkdir(parents=True)
    return path


def test_maintenance_state_verifies_current_version_and_redacts_values(maintenance):
    f = maintenance
    before = tree_bytes(f.bundle)
    state = release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    assert state["image_id"] == IMAGE and state["revision"] == OLD
    assert set(state["live"]) == {"social-auto-upload", "worker"}
    assert set(state["configuration"]) == {".env", "conf.py", "rclone-cache.conf"}
    assert "fixture-only" not in json.dumps(state)
    assert tree_bytes(f.bundle) == before
    assert not (f.runtime / "logs/release-staged.json").exists()


def test_maintenance_adoption_is_atomic_and_preserves_original_history(maintenance, monkeypatch):
    f = maintenance
    expected = release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    before = tree_bytes(f.bundle)
    db_before = f.db.read_bytes()
    original = f.pause.read_bytes()
    destination = new_destination(f)
    real_replace = os.replace
    calls = []

    def replace(source, target):
        if Path(target) == f.pause:
            assert f.pause.read_bytes() == original
            assert (destination / "baseline/adopted-maintenance.pause").read_bytes() == original
            assert not (f.runtime / "logs/release-staged.json").exists()
            calls.append((source, target))
        real_replace(source, target)
        if Path(target) == f.pause:
            assert f.pause.read_bytes() == (NEW + "\n").encode()

    monkeypatch.setattr(release_recovery.os, "replace", replace)
    release_recovery.adopt_maintenance_pause(f.runtime, expected, NEW, destination)
    assert len(calls) == 1 and not list(f.pause.parent.glob(".publish-pause-*"))
    assert tree_bytes(f.bundle) == before and f.db.read_bytes() == db_before
    assert json.loads((destination / "baseline/maintenance-recovery-reference.json").read_text()) == expected
    with sqlite3.connect(f.db) as conn:
        row = conn.execute("SELECT attempts,checkpoint FROM history WHERE target=10373").fetchone()
        assert row[0] == 8 and json.loads(row[1])["delivery_state"] == "original deep receipt"
    assert not (f.runtime / "logs/release-staged.json").exists()
    if os.name == "posix":
        assert (destination / "baseline/adopted-maintenance.pause").stat().st_mode & 0o077 == 0
        assert (destination / "baseline/maintenance-recovery-reference.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("name", ["release-staged.json", "release-active.json"])
@pytest.mark.parametrize("kind", ["file", "directory", "broken-symlink"])
def test_maintenance_refuses_any_existing_release_record(maintenance, name, kind):
    f = maintenance
    record = f.runtime / "logs" / name
    if kind == "file":
        record.write_text(json.dumps({"revision": OLD, "allow_adoption": True}))
    elif kind == "directory":
        record.mkdir()
    else:
        record.symlink_to(f.runtime / "missing")
    before = f.pause.read_bytes()
    with pytest.raises(release_recovery.RecoveryError, match="absent stage"):
        release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    assert f.pause.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "wrong-owner"])
def test_maintenance_marker_is_real_and_revision_owned(maintenance, kind):
    f = maintenance
    original = f.pause.read_bytes()
    saved = f.pause.with_suffix(".saved")
    f.pause.rename(saved)
    if kind == "directory":
        f.pause.mkdir()
    elif kind == "symlink":
        f.pause.symlink_to(saved)
    elif kind == "wrong-owner":
        f.pause.write_bytes((NEW + "\n").encode())
    with pytest.raises((OSError, release_recovery.RecoveryError)):
        release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    assert saved.read_bytes() == original


@pytest.mark.parametrize("revision", [OLD[:12], OLD.upper(), "", "mutable:latest"])
def test_maintenance_rejects_nonfull_revision(maintenance, revision):
    with pytest.raises(release_recovery.RecoveryError, match="full lowercase"):
        release_recovery.maintenance_state(maintenance.runtime, revision, maintenance.bundle)


@pytest.mark.parametrize("kind", ["relative", "outside", "parent", "symlink", "root"])
def test_maintenance_bundle_cannot_redirect_outside_runtime(maintenance, kind):
    f = maintenance
    if kind == "relative":
        path = Path("backups/releases/current")
    elif kind == "outside":
        path = f.runtime.parent / "outside"
        path.mkdir()
    elif kind == "parent":
        path = f.bundle / ".." / f.bundle.name
    elif kind == "root":
        path = f.bundle.parent
    else:
        path = f.bundle.parent / "link"
        path.symlink_to(f.bundle, target_is_directory=True)
    with pytest.raises((OSError, release_recovery.RecoveryError)):
        release_recovery.maintenance_state(f.runtime, OLD, path)


@pytest.mark.parametrize("name", [".env", "conf.py", "rclone-cache.conf"])
def test_maintenance_rejects_runtime_configuration_drift(maintenance, name):
    f = maintenance
    with (f.runtime / name).open("a") as stream:
        stream.write("# newer configuration\n")
    with pytest.raises(release_recovery.RecoveryError, match="configuration differs") as error:
        release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    assert "fixture-only" not in str(error.value)


@pytest.mark.parametrize("name", ["candidate.compose.json", "baseline/compose.json", "database.db",
                                  "baseline/.env", "baseline/conf.py", "recovery-manifest.json"])
def test_maintenance_rejects_unsealed_or_modified_recovery(maintenance, name):
    f = maintenance
    path = f.bundle / name
    if name == "recovery-manifest.json":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b" changed")
    with pytest.raises((OSError, ValueError)):
        release_recovery.maintenance_state(f.runtime, OLD, f.bundle)


@pytest.mark.parametrize("kind", ["image", "revision", "environment", "command", "entrypoint", "user",
                                  "db-rw", "shared-bind", "not-running", "oom", "worker-ingress"])
def test_maintenance_refuses_changed_actual_pair(maintenance, kind):
    f = maintenance
    obj = f.objects[1]
    if kind == "image":
        obj["Image"] = OTHER
    elif kind == "revision":
        obj["Config"]["Labels"]["org.opencontainers.image.revision"] = NEW
    elif kind == "environment":
        obj["Config"]["Env"].append("OTHER_GRANT=do-not-adopt")
    elif kind == "command":
        obj["Config"]["Cmd"] = ["python", "other-worker.py"]
    elif kind == "entrypoint":
        obj["Config"]["Entrypoint"] = ["other-entrypoint"]
    elif kind == "user":
        obj["Config"]["User"] = "0"
    elif kind == "db-rw":
        obj["Mounts"][0]["RW"] = False
    elif kind == "shared-bind":
        obj["Mounts"].pop()
    elif kind == "not-running":
        obj["State"]["Running"] = False
    elif kind == "oom":
        obj["State"]["OOMKilled"] = True
    else:
        obj["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "80"}]}
    with pytest.raises(release_recovery.RecoveryError):
        release_recovery.maintenance_state(f.runtime, OLD, f.bundle)


@pytest.mark.parametrize("field", ["drained", "active_targets", "active_preps"])
def test_maintenance_refuses_active_or_ambiguous_work(maintenance, field):
    f = maintenance
    f.activity[field] = False if field == "drained" else 1
    with pytest.raises(release_recovery.RecoveryError, match="active or ambiguous"):
        release_recovery.maintenance_state(f.runtime, OLD, f.bundle)


@pytest.mark.parametrize("kind", ["inode", "config", "container", "json-authority"])
def test_maintenance_adoption_rechecks_frozen_ownership(maintenance, kind):
    f = maintenance
    expected = release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    destination = new_destination(f)
    if kind == "inode":
        other = f.pause.with_suffix(".other")
        other.write_bytes(f.pause.read_bytes())
        os.replace(other, f.pause)
    elif kind == "config":
        (f.runtime / "conf.py").write_text("newer actual config\n")
    elif kind == "container":
        f.objects[0]["Id"] = "e" * 64
    else:
        expected["operator_approved"] = True
    before = f.pause.read_bytes()
    db_before = f.db.read_bytes()
    with pytest.raises((OSError, ValueError)):
        release_recovery.adopt_maintenance_pause(f.runtime, expected, NEW, destination)
    assert f.pause.read_bytes() == before and f.db.read_bytes() == db_before
    assert not (f.runtime / "logs/release-staged.json").exists()


def test_maintenance_late_stage_race_preserves_pause_and_original_bundle(maintenance, monkeypatch):
    f = maintenance
    expected = release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    destination = new_destination(f)
    before = tree_bytes(f.bundle)
    real_mkstemp = release_recovery.tempfile.mkstemp

    def race(*args, **kwargs):
        if kwargs.get("prefix") == ".publish-pause-":
            (f.runtime / "logs/release-staged.json").write_text("another genuine release")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(release_recovery.tempfile, "mkstemp", race)
    with pytest.raises(release_recovery.RecoveryError, match="absent stage"):
        release_recovery.adopt_maintenance_pause(f.runtime, expected, NEW, destination)
    assert f.pause.read_bytes() == (OLD + "\n").encode() and tree_bytes(f.bundle) == before
    assert not list(f.pause.parent.glob(".publish-pause-*"))


@pytest.mark.parametrize("kind", ["same-revision", "same-bundle", "sealed-destination"])
def test_maintenance_adoption_cannot_overwrite_recovery(maintenance, kind):
    f = maintenance
    expected = release_recovery.maintenance_state(f.runtime, OLD, f.bundle)
    destination = f.bundle if kind == "same-bundle" else new_destination(f)
    if kind == "sealed-destination":
        (destination / release_recovery.MANIFEST).write_text("do not overwrite")
    with pytest.raises(release_recovery.RecoveryError):
        release_recovery.adopt_maintenance_pause(f.runtime, expected, OLD if kind == "same-revision" else NEW,
                                                destination)
    assert f.pause.read_bytes() == (OLD + "\n").encode()


@pytest.mark.parametrize("flags", [
    ["--adopt-maintenance", OLD, "--hold"],
    ["--maintenance-recovery", "/private/bundle", "--hold"],
    ["--adopt-maintenance", OLD[:12], "--hold"],
    ["--adopt-maintenance", OLD, "--maintenance-recovery", "relative", "--hold"],
    ["--adopt-maintenance", OLD, "--maintenance-recovery", "/private/bundle"],
    ["--adopt-maintenance", OLD, "--maintenance-recovery", "/private/bundle", "--resume"],
    ["--adopt-maintenance", OLD, "--maintenance-recovery", "/private/bundle", "--build-only"],
    ["--adopt-maintenance", OLD, "--maintenance-recovery", "/private/bundle", "--hold", "--legacy-stop-web"],
    ["--adopt-maintenance", OLD, "--maintenance-recovery", "/private/bundle", "--hold", "--replace-held", OLD],
    ["--adopt-maintenance", OLD, "--adopt-maintenance", OLD, "--maintenance-recovery", "/private/bundle", "--hold"],
])
def test_maintenance_cli_authority_is_explicit_and_hold_only(flags):
    result = subprocess.run(["bash", str(ROOT / "scripts/deploy-local.sh"), *flags],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2


def test_maintenance_cli_keeps_existing_gate_and_pause_ordering():
    source = (ROOT / "scripts/deploy-local.sh").read_text()
    assert source.index('bash "$SOURCE/scripts/release-gates.sh" verify') < source.index("maintenance_state_read()")
    adoption = source.index("from scripts.release_recovery import adopt_maintenance_pause")
    assert source.index('"$CHECKS" --root "$RUNTIME" clean --revision') < adoption
    assert "maintenance_ready\n    PYTHONPATH=" in source
    assert adoption < source.index("wait_drained || fail")
    assert source.index('"$RECOVERY" seal') < source.index("social-auto-upload -m myUtils.db_bootstrap")
    assert "--resume" in source and "--replace-held" in source
    assert "(set -o noclobber;" in source and "Existing pause/staged release requires" in source
    function = source[source.index("maintenance_ready() {"):source.index('if [[ -n "$ADOPT_MAINTENANCE" ]]; then',
                                                                        source.index("maintenance_ready() {"))]
    assert "pair_ready paused" in function and "activity_check || fail" in function


def run_maintenance_deploy(fixture, monkeypatch, *, fault=""):
    """Real deploy shell and recovery code; only Git/Docker/gate boundaries are faked."""
    from tests import test_release_safety as safety

    for name, value in (("_HELD_REVISION", OLD), ("_NEW_REVISION", NEW),
                        ("_CANDIDATE_IMAGE", IMAGE), ("_OTHER_IMAGE", OTHER)):
        monkeypatch.setattr(safety, name, value)
    f = fixture
    with sqlite3.connect(f.db) as conn:
        conn.executescript("CREATE TABLE entries(value TEXT);"
                           "INSERT INTO entries VALUES ('original');"
                           "CREATE TABLE publish_job_targets(status TEXT, lease_token TEXT);"
                           "CREATE TABLE campaigns(status TEXT, metadata_json TEXT);")
        conn.execute("INSERT INTO publish_job_targets VALUES ('blocked', NULL)")
    services = copy.deepcopy(json.loads((f.bundle / "candidate.compose.json").read_text())["services"])
    for index, (name, role) in enumerate((("social-auto-upload", "web"), ("worker", "worker"))):
        service = services[name]
        literal = {k: v.replace("$$", "$") for k, v in service["environment"].items()}
        literal["SAU_REQUIRE_WORKER_HEALTH"] = "1" if role == "web" else "0"
        service.update(init=True, pull_policy="never")
        if role == "worker":
            service["command"] = ["python", "-m", "myUtils.worker", "--max-concurrent", "3"]
        f.objects[index]["Config"]["Env"] = [f"{k}={v}" for k, v in literal.items()]
        f.objects[index]["Config"]["Cmd"] = service["command"]
        service["environment"] = {k: v for k, v in literal.items() if k != "SAU_BUILD_REVISION"}
    f.render_compose = {"services": services}
    current = f.bundle.parent / "current-deployment"
    (current / "baseline").mkdir(parents=True)
    for name in ("web-image", "worker-image", "git-revision", "images.json", ".env", "conf.py", "rclone-cache.conf"):
        (current / "baseline" / name).write_bytes((f.bundle / "baseline" / name).read_bytes())
    frozen = copy.deepcopy(f.render_compose)
    for index, name in enumerate(("social-auto-upload", "worker")):
        env = dict(item.split("=", 1) for item in f.objects[index]["Config"]["Env"])
        frozen["services"][name]["environment"] = {k: v.replace("$", "$$") for k, v in env.items()}
    for name in ("candidate.compose.json", "baseline/compose.json"):
        (current / name).write_text(json.dumps(frozen))
    sqlite_backup.snapshot(f.db, current / "database.db")
    release_recovery.seal(current, candidate_image=IMAGE, previous_web=IMAGE, previous_worker=IMAGE)
    f.path = current
    f.stage = f.runtime / "logs/release-staged.json"
    (f.runtime / "logs/ci-watch.state").write_text("previous successful revision")
    fakebin = f.runtime / "fake-bin"
    fakebin.mkdir()
    pairfile = f.runtime / "fake-pair.json"
    pairfile.write_text(json.dumps(f.objects))
    docker = fakebin / "docker"
    docker.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
                      "assert sys.argv[1:]==['inspect','social-auto-upload','social-auto-upload-worker']\n"
                      "print(Path(" + repr(str(pairfile)) + ").read_text())\n")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
    before = tree_bytes(current)
    result = safety._run_held_deploy(f, fault=fault, runtime_head=OLD,
                                    flags=["--adopt-maintenance", OLD, "--maintenance-recovery", str(current), "--hold"])
    return result, current, before


def test_maintenance_deploy_shell_finishes_held_without_fake_old_stage(maintenance, monkeypatch):
    f = maintenance
    result, current, before = run_maintenance_deploy(f, monkeypatch)
    assert result.result.returncode == 0, result.diagnostic
    assert tree_bytes(current) == before
    state = json.loads(f.stage.read_text())
    assert state["revision"] == NEW and state["image_id"] == OTHER
    release = Path(state["release_dir"])
    assert (release / "baseline/adopted-maintenance.pause").read_bytes() == (OLD + "\n").encode()
    assert not (release / "baseline/replaced-stage.json").exists()
    assert release_recovery.verify(release) == dict(candidate_image=OTHER, previous_web=IMAGE, previous_worker=IMAGE)
    assert f.pause.read_bytes() == (NEW + "\n").encode()
    assert (f.runtime / "logs/ci-watch.state").read_text() == "previous successful revision"
    with sqlite3.connect(f.db) as conn:
        assert conn.execute("SELECT attempts FROM history WHERE target=10373").fetchone()[0] == 8
        assert conn.execute("SELECT value FROM entries").fetchall() == [("original",), ("migration receipt",)]
    assert {line.rsplit("|", 1)[1] for line in result.commands.splitlines()} == {OLD, NEW}
    assert "rollback-images" not in result.commands and "--signal=KILL" not in result.commands


@pytest.mark.parametrize("fault", ["gates", "non-draining", "config"])
def test_maintenance_deploy_preflight_failure_does_not_adopt(maintenance, monkeypatch, fault):
    f = maintenance
    result, current, before = run_maintenance_deploy(f, monkeypatch, fault=fault)
    assert result.result.returncode != 0, result.diagnostic
    assert tree_bytes(current) == before and f.pause.read_bytes() == (OLD + "\n").encode()
    assert not f.stage.exists()
    assert "docker:tag" not in result.commands and "docker:kill" not in result.commands
    assert "start-candidate" not in result.commands and "rollback-images" not in result.commands


def test_maintenance_rejects_valid_bundle_with_a_different_predecessor(maintenance):
    f = maintenance
    other = f.bundle.parent / "older-predecessor"
    (other / "baseline").mkdir(parents=True)
    for name in release_recovery.REQUIRED_FILES:
        dest = other / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((f.bundle / name).read_bytes())
    for name in ("web-image", "worker-image"):
        (other / "baseline" / name).write_text(OTHER + "\n")
    baseline = json.loads((other / "baseline/compose.json").read_text())
    for service in baseline["services"].values():
        service["image"] = OTHER
    (other / "baseline/compose.json").write_text(json.dumps(baseline))
    (other / "baseline/images.json").write_text(json.dumps({"services": {
        "social-auto-upload": {"image": OTHER}, "worker": {"image": OTHER}}}))
    release_recovery.seal(other, candidate_image=IMAGE, previous_web=OTHER, previous_worker=OTHER)
    assert release_recovery.verify(other)["previous_web"] == OTHER
    with pytest.raises(release_recovery.RecoveryError, match="current split version itself"):
        release_recovery.maintenance_state(f.runtime, OLD, other)
