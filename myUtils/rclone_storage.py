"""rclone-backed remote storage helpers."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import tempfile
import time
from typing import Sequence

from myUtils import rclone_config

RCLONE_COMMAND = "rclone"
DEFAULT_REMOTE_ENV = "SAU_DEFAULT_RCLONE_REMOTE"
DEFAULT_PATH_ENV = "SAU_DEFAULT_RCLONE_PATH"
PUBLIC_URL_TEMPLATE_ENV = "SAU_PUBLIC_URL_TEMPLATE"


@dataclass(frozen=True, slots=True)
class RemoteArtifact:
    local_path: str
    remote_name: str
    remote_path: str
    public_url: str | None = None

    @property
    def remote_spec(self) -> str:
        return f"{self.remote_name}:{self.remote_path}"

    def to_dict(self) -> dict:
        out = asdict(self)
        out["remote_spec"] = self.remote_spec
        return out


def run_subprocess(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    # rclone rewrites its config when it refreshes a token; see myUtils.rclone_config
    # for why the mounted config's directory makes that fail.
    return subprocess.run(rclone_config.with_writable_config(list(command)), check=True, **kwargs)


def _resolve_remote_name(remote_name: str | None) -> str:
    resolved = remote_name or os.environ.get(DEFAULT_REMOTE_ENV, "").strip()
    if not resolved:
        raise ValueError(
            "No rclone remote configured. Set remote_name or SAU_DEFAULT_RCLONE_REMOTE."
        )
    return resolved


def _resolve_remote_root(remote_root: str | None) -> str:
    configured = os.environ.get(DEFAULT_PATH_ENV, "") if remote_root is None else remote_root
    return configured.strip("/")


def _render_public_url_template(
    template: str,
    *,
    remote_name: str,
    remote_path: str,
    filename: str,
    campaign_id: int | None,
) -> str:
    return template.format(
        remote=remote_name,
        remote_name=remote_name,
        remote_path=remote_path,
        filename=filename,
        campaign_id="" if campaign_id is None else campaign_id,
    )


def build_remote_path(
    local_path: str | Path,
    *,
    campaign_id: int,
    remote_root: str | None = None,
    artifact_subdir: str | None = None,
) -> str:
    source = Path(local_path)
    parts = [part for part in (_resolve_remote_root(remote_root), "campaigns", str(campaign_id), artifact_subdir) if part]
    remote_dir = PurePosixPath(*parts) if parts else PurePosixPath()
    return str(remote_dir / source.name)




def _link_command(remote_spec: str, *, remote_name: str) -> list[str]:
    lower_remote = remote_name.lower()
    if 'onedrive' in lower_remote:
        return [
            RCLONE_COMMAND,
            'link',
            remote_spec,
            '--onedrive-link-scope',
            'anonymous',
            '--onedrive-link-type',
            'view',
        ]
    return [RCLONE_COMMAND, 'link', remote_spec]


def ensure_public_link(
    remote_path: str,
    *,
    remote_name: str | None = None,
    public_url_template: str | None = None,
    campaign_id: int | None = None,
    runner=run_subprocess,
) -> str:
    resolved_remote_name = _resolve_remote_name(remote_name)
    template = public_url_template
    if template is None:
        template = os.environ.get(PUBLIC_URL_TEMPLATE_ENV, "").strip() or None
    if template:
        return _render_public_url_template(
            template,
            remote_name=resolved_remote_name,
            remote_path=remote_path,
            filename=PurePosixPath(remote_path).name,
            campaign_id=campaign_id,
        )

    remote_spec = f"{resolved_remote_name}:{remote_path}"
    try:
        completed = runner(
            _link_command(remote_spec, remote_name=resolved_remote_name),
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if "Invalid request" in stderr or "accessDenied" in stderr:
            raise RuntimeError(
                "OneDrive anonymous link creation failed. Tenant sharing policy "
                "likely blocks public links; configure SAU_PUBLIC_URL_TEMPLATE or "
                "enable anonymous sharing for the target drive."
            ) from exc
        raise

    public_url = (completed.stdout or "").strip()
    if not public_url:
        raise RuntimeError(f"rclone link returned an empty URL for {remote_spec}")
    return public_url


def _read_command(command: list[str], *, runner, connect_timeout: float,
                  operation_timeout: float, total_deadline: float):
    """Single read attempt, including a subprocess wall-clock deadline."""
    if min(connect_timeout, operation_timeout, total_deadline) <= 0:
        raise TimeoutError("remote read budget exhausted")
    command = command + [
        "--contimeout", f"{min(connect_timeout, total_deadline):g}s",
        "--timeout", f"{min(operation_timeout, total_deadline):g}s",
        "--retries", "1", "--low-level-retries", "1",
    ]
    return runner(command, capture_output=True, text=True, timeout=total_deadline)


def stat_artifact(remote_path: str, *, remote_name: str | None = None,
                  remote_root: str | None = None, runner=run_subprocess,
                  connect_timeout: float = 10.0, operation_timeout: float = 15.0,
                  total_deadline: float = 20.0) -> dict:
    """Fresh exact-object read-only stat; never list/search by basename."""
    remote = _resolve_remote_name(remote_name)
    root = _resolve_remote_root(remote_root)
    key = str(PurePosixPath(root, remote_path)) if root else remote_path
    result = _read_command([RCLONE_COMMAND, "lsjson", f"{remote}:{key}", "--stat", "--hash"],
                           runner=runner, connect_timeout=connect_timeout,
                           operation_timeout=operation_timeout, total_deadline=total_deadline)
    data = json.loads(result.stdout or "null")
    if not isinstance(data, dict) or data.get("IsDir") or not isinstance(data.get("Size"), int):
        raise ValueError("remote stat did not describe a file")
    proofs = []
    for name, value in (data.get("Hashes") or {}).items():
        algorithm = str(name).lower().replace("-", "")
        if algorithm in {"sha256", "md5", "sha1"} and value:
            proofs.append({"algorithm": algorithm, "value": value})
    proofs.sort(key=lambda proof: {"sha256": 0, "sha1": 1, "md5": 2}[proof["algorithm"]])
    return {"size_bytes": data["Size"], "remote_checksum": proofs[0] if proofs else None,
            "checksums": proofs}


def download_artifact(remote_path: str, local_path: str | Path, *,
                      remote_name: str | None = None, remote_root: str | None = None,
                      runner=run_subprocess, connect_timeout: float = 10.0,
                      operation_timeout: float = 30.0, total_deadline: float = 120.0,
                      max_bytes: int = 512 * 1024 * 1024) -> Path:
    """Bounded exact read, installing only a complete nonempty sibling file.

    Integrity-aware callers pass their own staging destination and verify it
    before final exposure; this low-level function does not invent checksums.
    """
    started = time.monotonic()
    remote = _resolve_remote_name(remote_name)
    root = _resolve_remote_root(remote_root)
    key = str(PurePosixPath(root, remote_path)) if root else remote_path
    destination = Path(local_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        _read_command([RCLONE_COMMAND, "copyto", f"{remote}:{key}", str(temporary),
                       "--max-transfer", str(max_bytes), "--cutoff-mode", "CAUTIOUS"],
                      runner=runner, connect_timeout=connect_timeout,
                      operation_timeout=operation_timeout,
                      total_deadline=total_deadline - (time.monotonic() - started))
        if time.monotonic() - started > total_deadline:
            raise TimeoutError("remote read budget exhausted")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("rclone restore produced a missing or empty file")
        if temporary.stat().st_size > max_bytes:
            raise ValueError("remote object exceeded byte limit")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def delete_artifact(
    remote_path: str,
    *,
    remote_name: str | None = None,
    remote_root: str | None = None,
    runner=run_subprocess,
) -> None:
    """Remove one object from the rclone remote.

    Counterpart to :func:`download_artifact`: the write side pairs
    ``rclone copyto`` upload with read ``copyto`` download, and the purge half
    of the media lifecycle (posted -> archived -> deleted) needs the matching
    ``rclone deletefile``. Without it a caller that only knows the row's
    provider has no way to drop a Drive object, which is why the S3-only
    delete in ``sau_backend`` silently no-ops for rclone rows.
    """
    resolved_remote_name = _resolve_remote_name(remote_name)
    root = _resolve_remote_root(remote_root)
    spec_path = str(PurePosixPath(root, remote_path)) if root else remote_path
    runner(
        [RCLONE_COMMAND, "deletefile", f"{resolved_remote_name}:{spec_path}"],
        capture_output=True,
        text=True,
    )


def upload_artifact(
    local_path: str | Path,
    *,
    campaign_id: int,
    remote_name: str | None = None,
    remote_root: str | None = None,
    artifact_subdir: str | None = None,
    public_url_template: str | None = None,
    link: bool = True,
    runner=run_subprocess,
) -> RemoteArtifact:
    source = Path(local_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    resolved_remote_name = _resolve_remote_name(remote_name)
    remote_path = build_remote_path(
        source,
        campaign_id=campaign_id,
        remote_root=remote_root,
        artifact_subdir=artifact_subdir,
    )
    remote_spec = f"{resolved_remote_name}:{remote_path}"
    runner(
        [
            RCLONE_COMMAND,
            "copyto",
            str(source),
            remote_spec,
        ],
        capture_output=True,
        text=True,
    )

    public_url = None
    if link:
        public_url = ensure_public_link(
            remote_path,
            remote_name=resolved_remote_name,
            public_url_template=public_url_template,
            campaign_id=campaign_id,
            runner=runner,
        )
    return RemoteArtifact(
        local_path=str(source),
        remote_name=resolved_remote_name,
        remote_path=remote_path,
        public_url=public_url,
    )
