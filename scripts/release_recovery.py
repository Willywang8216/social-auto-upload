#!/usr/bin/env python3
"""Seal/verify a release's private recovery bundle; never restore the live DB.

The manifest contains only fixed relative filenames and their SHA256 hashes.
It detects missing files or accidental drift, not malicious modification by the
host operator. Verification creates its disposable SQLite restore inside the
supplied bundle and never reseals it or changes the saved recovery artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
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
