"""rclone-backed remote storage helpers."""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

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
    return subprocess.run(command, check=True, **kwargs)


def _resolve_remote_name(remote_name: str | None) -> str:
    resolved = remote_name or os.environ.get(DEFAULT_REMOTE_ENV, "").strip()
    if not resolved:
        raise ValueError(
            "No rclone remote configured. Set remote_name or SAU_DEFAULT_RCLONE_REMOTE."
        )
    return resolved


def _resolve_remote_root(remote_root: str | None) -> str:
    return (remote_root or os.environ.get(DEFAULT_PATH_ENV, "")).strip("/")


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
            [
                RCLONE_COMMAND,
                "link",
                remote_spec,
                "--onedrive-link-scope",
                "anonymous",
                "--onedrive-link-type",
                "view",
            ],
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
