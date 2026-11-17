"""Private immutable files shared by preparation, preview and trusted handoff."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .contracts import MAX_ASSET, RepurposeError, json_bytes, loads, require, validate_manifest


def private_path(value, *, root=None):
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts and not str(path).startswith('\\\\'), 'private_path')
    for part in (path, *path.parents):
        require(not part.is_symlink() and not (hasattr(part, 'is_junction') and part.is_junction()), 'private_symlink')
    repo = Path(__file__).resolve().parents[2]
    require(not path.resolve().is_relative_to(repo), 'private_storage_in_repository')
    if root is not None:
        require(path.resolve().is_relative_to(Path(root).resolve()), 'private_path_scope')
    return path


def write_new(path, raw):
    path = Path(path)
    require(isinstance(raw, bytes), 'file_bytes')
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == 'posix':
        os.chmod(path, 0o600)


def read_file(path, limit=MAX_ASSET):
    path = private_path(path)
    require(path.is_file() and path.stat().st_size <= limit, 'private_file_missing_or_large')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) <= limit and (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns), 'private_file_changed')
    return raw


def verify_package(path):
    path = private_path(path)
    require(path.name == 'manifest.json', 'manifest_filename')
    manifest = validate_manifest(loads(read_file(path, 512 * 1024)))
    source = read_file(path.parent / 'manuscript.md', 512 * 1024)
    require(source == manifest['material']['source']['manuscript'].encode('utf-8'), 'private_source_changed')
    for asset in manifest['material']['assets']:
        raw = read_file(path.parent / asset['name'])
        require(len(raw) == asset['size_bytes'] and hashlib.sha256(raw).hexdigest() == asset['sha256'], 'asset_changed')
    return manifest


def store_manifest(root, manifest):
    root = private_path(root)
    validate_manifest(manifest)
    path = root / 'manifest.json'
    write_new(path, json_bytes(manifest) + b'\n')
    return path


def sync_file(path):
    """Flush a task-owned output before its immutable manifest is committed."""
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'sync_file_path')
    with path.open('r+b') as stream:
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == 'posix':
        os.chmod(path, 0o600)
