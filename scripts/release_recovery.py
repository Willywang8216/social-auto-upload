#!/usr/bin/env python3
"""Seal/verify a release's private recovery bundle; never restore the live DB.

The manifest contains only fixed relative filenames and their SHA256 hashes.
It detects missing files or accidental drift, not malicious modification by the
host operator. Verification creates its disposable SQLite restore inside the
supplied bundle and never reseals it or changes the saved recovery artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import sqlite_backup

MANIFEST = "recovery-manifest.json"
FORMAT = "sau-recovery-v1"
REQUIRED_FILES = (
    "candidate.compose.json", "baseline/compose.json", "baseline/images.json",
    "baseline/web-image", "baseline/worker-image", "baseline/git-revision",
    "baseline/.env", "baseline/conf.py", "database.db", "database.db.sha256",
)


class RecoveryError(ValueError):
    """Recovery evidence or the selected config is unsafe/incomplete."""


def require_config_file(source: Path) -> Path:
    """Resolve the selected bind (valid symlinks allowed), without creating it."""
    try:
        path = Path(source).resolve(strict=True)
        if not stat.S_ISREG(path.stat().st_mode):
            raise RecoveryError("selected conf.py bind must be a readable regular file")
        with path.open("rb") as stream:
            stream.read(1)  # actually test readability, not only existence/mode
        return path
    except (OSError, RuntimeError):
        raise RecoveryError("selected conf.py bind must be a readable regular file") from None


def _bundle_root(bundle: Path) -> Path:
    path = Path(bundle)
    if path.is_symlink() or not path.is_dir():
        raise RecoveryError("recovery bundle must be an existing nonsymlink directory")
    return path.resolve(strict=True)


def _file(root: Path, name: str) -> Path:
    # Check the whitelist BEFORE joining any manifest-supplied path. Internal
    # parent directories must not redirect even an otherwise whitelisted file.
    if name not in REQUIRED_FILES and name != MANIFEST:
        raise RecoveryError("unexpected recovery artifact path")
    parent = root
    for part in Path(name).parts[:-1]:
        parent /= part
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise RecoveryError("recovery artifact parent must be a nonsymlink directory")
    path = root / name
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RecoveryError("recovery artifact must be a nonsymlink regular file")
    return path


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RecoveryError("recovery JSON must be an object")
    return value


def _image_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise RecoveryError("recovery requires immutable image IDs")
    return value


def _service_image(compose: dict, name: str) -> str:
    services = compose.get("services")
    if not isinstance(services, dict) or not isinstance(services.get(name), dict):
        raise RecoveryError("required recovery service is missing")
    return _image_id(services[name].get("image"))


def _identities(root: Path, candidate_image: str | None, previous_web: str | None,
                previous_worker: str | None) -> dict:
    candidate = _json(_file(root, "candidate.compose.json"))
    current = _service_image(candidate, "social-auto-upload")
    if _service_image(candidate, "worker") != current:
        raise RecoveryError("candidate image IDs do not match")
    web = _image_id(_file(root, "baseline/web-image").read_text(encoding="ascii").strip())
    worker = _file(root, "baseline/worker-image").read_text(encoding="ascii").strip()
    if worker and _image_id(worker) != web:
        raise RecoveryError("previous image IDs do not match")
    revision = _file(root, "baseline/git-revision").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RecoveryError("previous git revision is invalid")
    baseline = _json(_file(root, "baseline/compose.json"))
    if _service_image(baseline, "social-auto-upload") != web:
        raise RecoveryError("rollback Compose image differs from the saved image ID")
    images = {"social-auto-upload": {"image": web}}
    if worker:
        if _service_image(baseline, "worker") != worker:
            raise RecoveryError("rollback worker image differs from the saved image ID")
        images["worker"] = {"image": worker}
    # A legacy baseline may contain an unused worker definition, but its image
    # override must name only the services that actually existed previously.
    if _json(_file(root, "baseline/images.json")) != {"services": images}:
        raise RecoveryError("rollback image override is inconsistent")
    actual = dict(candidate_image=current, previous_web=web, previous_worker=worker)
    expected = dict(candidate_image=candidate_image, previous_web=previous_web,
                    previous_worker=previous_worker)
    if any(value is not None and value != actual[key] for key, value in expected.items()):
        raise RecoveryError("recovery image IDs differ from the expected release")
    return actual


def _hashes(root: Path) -> dict:
    return {name: sqlite_backup.sha256(_file(root, name)) for name in REQUIRED_FILES}


def seal(bundle: Path, *, candidate_image: str | None = None,
         previous_web: str | None = None, previous_worker: str | None = None) -> None:
    root = _bundle_root(bundle)
    destination = root / MANIFEST
    if destination.exists() or destination.is_symlink():
        raise RecoveryError("recovery bundle is already sealed; refusing to replace evidence")
    _identities(root, candidate_image, previous_web, previous_worker)
    sqlite_backup.verify_snapshot(_file(root, "database.db"))
    manifest = {"format": FORMAT, "files": _hashes(root)}
    fd, name = tempfile.mkstemp(prefix=".recovery-seal-", dir=root)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, destination)  # atomic and no-clobber; mkstemp is mode 0600
    finally:
        temp.unlink(missing_ok=True)


def verify(bundle: Path, *, candidate_image: str | None = None,
           previous_web: str | None = None, previous_worker: str | None = None) -> dict:
    root = _bundle_root(bundle)
    manifest = _json(_file(root, MANIFEST))
    if set(manifest) != {"format", "files"} or manifest["format"] != FORMAT:
        raise RecoveryError("invalid recovery manifest format")
    expected = manifest["files"]
    if not isinstance(expected, dict) or set(expected) != set(REQUIRED_FILES):
        raise RecoveryError("unexpected or missing recovery artifact paths")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
           for value in expected.values()):
        raise RecoveryError("invalid recovery artifact checksum")
    if _hashes(root) != expected:
        raise RecoveryError("recovery artifacts changed since sealing")
    identities = _identities(root, candidate_image, previous_web, previous_worker)
    # Recheck the DB's own checksum/quick_check AND a fresh isolated restore.
    # All writes are confined to a disposable directory within this bundle.
    sqlite_backup.verify_restore(_file(root, "database.db"), temp_parent=root)
    if _hashes(root) != expected:
        raise RecoveryError("recovery artifacts changed during verification")
    return identities


def _regular_state(path: Path) -> tuple[bytes, dict]:
    """Read an ownership marker without following a link or accepting a race."""
    def identity(info: os.stat_result) -> list[int]:
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryError("held release markers must be nonsymlink regular files")
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    before = identity(path.lstat())
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if identity(os.fstat(stream.fileno())) != before:
            raise RecoveryError("held release ownership changed while reading")
        data = stream.read()
    if identity(path.lstat()) != before:
        raise RecoveryError("held release ownership changed while reading")
    return data, dict(identity=before, sha256=hashlib.sha256(data).hexdigest())


def held_state(runtime: Path, revision: str) -> dict:
    """Validate held metadata only; the caller must also verify recovery/live state.

    The returned fingerprints are rechecked at adoption under ci-watch.lock.
    They are not a replacement for that lock or explicit operator ownership.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RecoveryError("held revision must be a full lowercase SHA")
    runtime = runtime.resolve(strict=True)
    pause, pause_state = _regular_state(runtime / "db/publish.pause")
    if pause not in (revision.encode("ascii"), (revision + "\n").encode("ascii")):
        raise RecoveryError("pause does not belong to the requested held release")
    raw_stage, stage_state = _regular_state(runtime / "logs/release-staged.json")
    stage = json.loads(raw_stage)
    if not isinstance(stage, dict) or stage.get("revision") != revision:
        raise RecoveryError("staged revision does not match the held pause")
    image = _image_id(stage.get("image_id"))
    value = stage.get("release_dir")
    if not isinstance(value, str):
        raise RecoveryError("held recovery directory is missing")
    directory = Path(value)
    releases = runtime / "backups/releases"
    if not directory.is_absolute() or ".." in directory.parts or directory == releases:
        raise RecoveryError("held recovery bundle must be beneath runtime/backups/releases")
    if not directory.is_relative_to(releases):
        raise RecoveryError("held recovery bundle must be beneath runtime/backups/releases")
    # Resolve only AFTER checking every component, so a symlink cannot launder
    # an outside bundle (or a different release) into an apparently safe path.
    parent = runtime
    for part in directory.relative_to(runtime).parts:
        parent /= part
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise RecoveryError("held recovery path must contain only nonsymlink directories")
    return dict(revision=revision, image_id=image, release_dir=str(directory.resolve(strict=True)),
                pause=pause_state, stage=stage_state)


def adopt_held_pause(runtime: Path, expected: dict, revision: str, release: Path) -> None:
    """Compare ownership, retain prior metadata privately, then atomically replace.

    Called only AFTER all bundle/image/live/config/drain checks, with the shared
    release lock still held. Never unlink the pause or modify the old bundle/DB.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == expected["revision"]:
        raise RecoveryError("replacement must name a different full revision")
    if held_state(runtime, expected["revision"]) != expected:
        raise RecoveryError("held release ownership changed before adoption")
    destination = _bundle_root(release)
    if destination == Path(expected["release_dir"]):
        raise RecoveryError("replacement must preserve the original recovery bundle")
    baseline = destination / "baseline"
    if not stat.S_ISDIR(baseline.lstat().st_mode):
        raise RecoveryError("replacement baseline must be a nonsymlink directory")
    raw_stage, fingerprint = _regular_state(runtime / "logs/release-staged.json")
    if fingerprint != expected["stage"]:
        raise RecoveryError("staged release changed before adoption")
    # Exclusive creation, mode 0600 even when this helper is called without the
    # shell's private umask. The old stage itself is not overwritten until success.
    saved = baseline / "replaced-stage.json"
    fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw_stage)
        stream.flush()
        os.fsync(stream.fileno())
    pause = runtime / "db/publish.pause"
    fd, name = tempfile.mkstemp(prefix=".publish-pause-", dir=pause.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((revision + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        if held_state(runtime, expected["revision"]) != expected:
            raise RecoveryError("held release ownership changed before adoption")
        os.replace(temporary, pause)  # one rename, never an unpaused interval
    finally:
        temporary.unlink(missing_ok=True)


def _maintenance_bundle(runtime: Path, bundle: Path) -> Path:
    """Accept only an explicit nonsymlink bundle beneath this runtime."""
    directory = Path(bundle)
    releases = runtime / "backups/releases"
    if (not directory.is_absolute() or ".." in directory.parts
            or directory == releases or not directory.is_relative_to(releases)):
        raise RecoveryError("maintenance recovery must be beneath runtime/backups/releases")
    parent = runtime
    for part in directory.relative_to(runtime).parts:
        parent /= part
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise RecoveryError("maintenance recovery path must contain nonsymlink directories")
    return _bundle_root(directory)


def _maintenance_live_pair(runtime: Path, bundle: Path, revision: str, image: str) -> dict:
    """Compare the actual split pair to the frozen rollback, without printing secrets."""
    baseline = _json(_file(bundle, "baseline/compose.json"))["services"]
    try:
        objects = json.loads(subprocess.check_output(
            ["docker", "inspect", "social-auto-upload", "social-auto-upload-worker"],
            text=True, stderr=subprocess.DEVNULL, timeout=20))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RecoveryError("maintenance live pair could not be inspected") from None
    if not isinstance(objects, list) or len(objects) != 2:
        raise RecoveryError("maintenance recovery requires the existing split pair")
    fingerprints = {}
    shared_binds = None
    for obj, name, role in zip(objects, ("social-auto-upload", "worker"), ("web", "worker")):
        saved = baseline[name]
        env = dict(item.split("=", 1) for item in obj["Config"]["Env"])
        saved_env = {key: value.replace("$$", "$") for key, value in saved["environment"].items()}
        binds = {m["Destination"]: {"source": str(Path(m["Source"]).resolve(strict=True)), "rw": m["RW"]}
                 for m in obj["Mounts"] if m["Type"] == "bind"}
        saved_binds = {m["target"]: {"source": str(Path(m["source"]).resolve(strict=True)),
                                   "rw": not m.get("read_only", False)}
                       for m in saved.get("volumes", []) if m.get("type") == "bind"}
        if (obj["Image"] != image or not obj["State"]["Running"] or obj["State"]["OOMKilled"]
                or obj["Config"]["Labels"].get("org.opencontainers.image.revision") != revision
                or env != saved_env or obj["Config"]["Cmd"] != saved.get("command")
                or obj["Config"]["Entrypoint"] != saved.get("entrypoint")
                or obj["Config"]["User"] != saved.get("user") or binds != saved_binds
                or env.get("SAU_PROCESS_ROLE") != role or env.get("SAU_BUILD_REVISION") != revision
                or env.get("SAU_DB_PATH") != "/app/db/database.db"
                or env.get("SAU_PAUSE_CLAIMS_FILE") != "/app/db/publish.pause"
                or binds.get("/app/db", {}).get("source") != str(runtime / "db")
                or not binds.get("/app/db", {}).get("rw")
                or (shared_binds is not None and binds != shared_binds)):
            raise RecoveryError("maintenance live images/configuration/mounts differ from recovery")
        if role == "worker" and obj["HostConfig"].get("PortBindings"):
            raise RecoveryError("maintenance worker must not expose ingress")
        shared_binds = binds
        values = {"environment": env, "binds": binds, "command": obj["Config"]["Cmd"],
                  "entrypoint": obj["Config"]["Entrypoint"], "user": obj["Config"]["User"]}
        fingerprints[name] = {"id": obj["Id"], "image_id": image,
                              "restart_count": obj["RestartCount"],
                              "configuration_sha256": hashlib.sha256(json.dumps(
                                  values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    state = sqlite_backup.activity(runtime / "db/database.db")
    if state.get("drained") is not True or state.get("active_targets") != 0 or state.get("active_preps") != 0:
        raise RecoveryError("maintenance recovery refuses active or ambiguous work")
    return fingerprints


def maintenance_state(runtime: Path, revision: str, recovery: Path) -> dict:
    """Evidence for explicit maintenance-only adoption; JSON itself grants no authority.

    Unlike held_state this requires ABSENT stage/active records and a verified
    current-version rollback whose predecessor is the current split pair itself.
    The CLI must additionally verify fresh paused role probes under ci-watch.lock.
    """
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RecoveryError("maintenance revision must be a full lowercase SHA")
    runtime = runtime.resolve(strict=True)
    if any(os.path.lexists(runtime / "logs" / name)
           for name in ("release-staged.json", "release-active.json")):
        raise RecoveryError("maintenance adoption requires absent stage and active records")
    pause, pause_state = _regular_state(runtime / "db/publish.pause")
    if pause not in (revision.encode("ascii"), (revision + "\n").encode("ascii")):
        raise RecoveryError("pause does not belong to the requested maintenance revision")
    bundle = _maintenance_bundle(runtime, recovery)
    identities = _identities(bundle, None, None, None)
    image = identities["candidate_image"]
    if (identities["previous_web"] != image or identities["previous_worker"] != image
            or _file(bundle, "baseline/git-revision").read_text(encoding="ascii").strip() != revision):
        raise RecoveryError("maintenance recovery must freeze the current split version itself")
    verify(bundle, candidate_image=image, previous_web=image, previous_worker=image)
    candidate = _json(_file(bundle, "candidate.compose.json"))["services"]
    baseline = _json(_file(bundle, "baseline/compose.json"))["services"]
    if any(candidate[name].get("volumes") != baseline[name].get("volumes")
           for name in ("social-auto-upload", "worker")):
        raise RecoveryError("maintenance current-version recovery mount definitions differ")
    conf = require_config_file(Path(next(v["source"] for v in candidate["social-auto-upload"]["volumes"]
                                        if v["target"] == "/app/conf.py")))
    configuration = {}
    for name, source in ((".env", runtime / ".env"), ("conf.py", conf),
                         ("rclone-cache.conf", runtime / "rclone-cache.conf")):
        saved = bundle / "baseline" / name
        if name == "rclone-cache.conf" and not os.path.lexists(source) and not os.path.lexists(saved):
            continue
        raw, current = _regular_state(source)
        saved_raw, _ = _regular_state(saved)
        if raw != saved_raw:
            raise RecoveryError("maintenance runtime configuration differs from its recovery snapshot")
        configuration[name] = dict(path=str(source), **current)
    live = _maintenance_live_pair(runtime, bundle, revision, image)
    _, manifest = _regular_state(bundle / MANIFEST)
    return dict(revision=revision, image_id=image, release_dir=str(bundle), pause=pause_state,
                recovery_manifest=manifest, configuration=configuration, live=live)


def adopt_maintenance_pause(runtime: Path, expected: dict, revision: str, release: Path) -> None:
    """Atomically hand off a verified maintenance pause; never synthesize a stage or restore DB.

    Called only by the explicit --adopt-maintenance/--maintenance-recovery --hold
    path after all new-image gates, rollback/config/live/drain and paused probes.
    """
    if (not isinstance(expected, dict) or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == expected.get("revision")):
        raise RecoveryError("maintenance replacement must name a different full revision")
    runtime = runtime.resolve(strict=True)
    recovery = Path(expected["release_dir"])
    if maintenance_state(runtime, expected["revision"], recovery) != expected:
        raise RecoveryError("maintenance ownership changed before adoption")
    destination = _maintenance_bundle(runtime, release)
    if destination == recovery or (destination / MANIFEST).exists():
        raise RecoveryError("maintenance adoption must preserve the sealed recovery bundle")
    baseline = destination / "baseline"
    if not stat.S_ISDIR(baseline.lstat().st_mode):
        raise RecoveryError("replacement baseline must be a nonsymlink directory")
    raw, fingerprint = _regular_state(runtime / "db/publish.pause")
    if fingerprint != expected["pause"]:
        raise RecoveryError("maintenance pause changed before adoption")
    for path, data in ((baseline / "adopted-maintenance.pause", raw),
                       (baseline / "maintenance-recovery-reference.json",
                        (json.dumps(expected, sort_keys=True, indent=2) + "\n").encode())):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    pause = runtime / "db/publish.pause"
    fd, name = tempfile.mkstemp(prefix=".publish-pause-", dir=pause.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((revision + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        if maintenance_state(runtime, expected["revision"], recovery) != expected:
            raise RecoveryError("maintenance ownership changed before adoption")
        os.replace(temporary, pause)  # one rename, never an unpaused interval
        directory_fd = os.open(pause.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("seal", "verify"):
        action = sub.add_parser(command)
        action.add_argument("bundle", type=Path)
        action.add_argument("--candidate-image")
        action.add_argument("--previous-web")
        action.add_argument("--previous-worker")
    args = parser.parse_args(argv)
    try:
        operation = seal if args.command == "seal" else verify
        operation(args.bundle, candidate_image=args.candidate_image,
                  previous_web=args.previous_web, previous_worker=args.previous_worker)
        return 0
    except (OSError, ValueError, sqlite3.Error, sqlite_backup.BackupError, RuntimeError):
        # JSON/config/source exceptions may contain sensitive data. Emit only a
        # fixed message and leave the held candidate/rollback decisions untouched.
        print("Recovery bundle verification failed; keep the candidate paused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
