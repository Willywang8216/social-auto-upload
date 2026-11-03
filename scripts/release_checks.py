#!/usr/bin/env python3
"""Shared static checks and clean-commit receipts for release-gates.sh."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MAINTAINED_DIRS = ("myUtils", "utils", "uploader", "mcp_server", "sau_app", "sau_inbox",
                   "scripts", "db", "migrations", "docker", "tests", "examples", "skills", "tools")
# Python 3.10 is end-of-life and is no longer tested or shipped; the matrix
# covers the supported production runtime and the next version.
COMPONENTS = ("backend-3.12", "backend-3.13", "frontend")
LOCKS = ("uv.lock", "pyproject.toml", "sau_frontend/package-lock.json")
FORMAT = "sau-release-gates-v1"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def clean_revision(root: Path, expected: str | None = None) -> str:
    revision = git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release requires a full git commit")
    if expected is not None and revision != expected:
        raise ValueError("checkout is not the tested revision")
    # The only untracked exception is the release-owned runtime pause marker.
    # Ignored runtime config/data are never copied to the test checkout.
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    if any(line != "?? db/publish.pause" for line in status.splitlines()):
        raise ValueError("dirty or unknown local work; release refused")
    return revision


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(root: Path, revision: str) -> dict:
    clean_revision(root, revision)
    return {"format": FORMAT, "revision": revision, "tree": git(root, "rev-parse", "HEAD^{tree}"),
            "locks": {name: digest(root / name) for name in LOCKS}}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("receipt must not be a symlink")
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def record(root: Path, revision: str, directory: Path, component: str) -> None:
    value = identity(root, revision)
    if sys.platform != "linux":
        raise ValueError("release test receipts require Linux")
    checks = ["npm-ci", "frontend-tests", "frontend-build"]
    if component.startswith("backend-"):
        if component != f"backend-{sys.version_info.major}.{sys.version_info.minor}":
            raise ValueError("receipt interpreter does not match test matrix")
        checks = ["compile", "undefined-names", "backend-tests-including-security"]
    value.update(component=component, checks=checks, passed=True, platform="linux",
                 completed_at=datetime.now(timezone.utc).isoformat())
    write_json(directory / f"{component}.json", value)


def verify_receipts(root: Path, revision: str, directory: Path, image_id: str | None = None) -> str:
    expected = identity(root, revision)
    evidence = hashlib.sha256()
    for component in COMPONENTS:
        path = directory / f"{component}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if any(value.get(key) != item for key, item in expected.items()):
            raise ValueError("receipt belongs to a different source/lock revision")
        if value.get("passed") is not True or value.get("component") != component or value.get("platform") != "linux":
            raise ValueError("missing passing Linux gate")
        required = (["compile", "undefined-names", "backend-tests-including-security"]
                    if component.startswith("backend-") else ["npm-ci", "frontend-tests", "frontend-build"])
        if value.get("checks") != required:
            raise ValueError("incomplete release gates")
        evidence.update(path.read_bytes())
    receipt_hash = evidence.hexdigest()
    if image_id is not None:
        value = json.loads((directory / "image.json").read_text(encoding="utf-8"))
        if any(value.get(key) != item for key, item in expected.items()):
            raise ValueError("image receipt source mismatch")
        if value.get("image_id") != image_id or value.get("gates_sha256") != receipt_hash:
            raise ValueError("image is not bound to the passing gates")
    return receipt_hash


def record_build(root: Path, revision: str, directory: Path, image_id: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("an immutable Docker image ID is required")
    gate_hash = verify_receipts(root, revision, directory)
    value = identity(root, revision)
    value.update(image_id=image_id, gates_sha256=gate_hash,
                 built_at=datetime.now(timezone.utc).isoformat())
    write_json(directory / "image.json", value)


def static_checks(root: Path) -> int:
    sys.path.insert(0, str(root))
    from scripts.check_undefined_names import check_file

    paths = set(root.glob("*.py")) - {root / "conf.py"}
    for directory in MAINTAINED_DIRS:
        paths.update((root / directory).rglob("*.py"))
    paths = {p for p in paths if "__pycache__" not in p.parts}
    errors = 0
    for path in sorted(paths):
        try:
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            print(f"{path.relative_to(root)}: compile failed: {type(exc).__name__}")
            errors += 1
            continue
        for _, line, name in check_file(path):
            print(f"{path.relative_to(root)}:{line}: undefined name '{name}'")
            errors += 1
    print(f"static checks: {len(paths)} maintained Python files; {errors} error(s)")
    return int(errors != 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    clean = sub.add_parser("clean")
    clean.add_argument("--revision")
    sub.add_parser("static")
    for command in ("record", "verify", "record-build"):
        action = sub.add_parser(command)
        action.add_argument("--revision", required=True)
        action.add_argument("--receipts", required=True, type=Path)
        if command == "record":
            action.add_argument("--component", required=True, choices=COMPONENTS)
        else:
            action.add_argument("--image-id", required=command == "record-build")
    args = parser.parse_args(argv)
    try:
        if args.command == "clean":
            print(clean_revision(args.root, args.revision))
        elif args.command == "static":
            return static_checks(args.root)
        elif args.command == "record":
            record(args.root, args.revision, args.receipts, args.component)
        elif args.command == "record-build":
            record_build(args.root, args.revision, args.receipts, args.image_id)
        else:
            verify_receipts(args.root, args.revision, args.receipts, args.image_id)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Release check refused: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
