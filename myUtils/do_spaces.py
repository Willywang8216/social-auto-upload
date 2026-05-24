"""DigitalOcean Spaces (S3-compatible) storage client.

Provides helpers to upload files and bytes to DO Spaces and return
public CDN URLs.  Credentials are read from environment variables.

The module-level functions (upload_file, upload_bytes, etc.) delegate to
a lazily-initialized default client built from env vars, preserving
backward compatibility with existing callers (analytics thumbnails).

For multi-account support, instantiate ``SpacesClient`` directly or use
``client_from_row()`` with a ``storage_backends`` DB row.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)


class SpacesClient:
    """Config-driven S3-compatible client. Holds its own boto3 session."""

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
        endpoint: str,
        cdn_url: str = "",
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self.endpoint = endpoint
        self.cdn_url = cdn_url or f"https://{bucket}.{region}.digitaloceanspaces.com"
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=BotoConfig(s3={"addressing_style": "virtual"}),
            )
        return self._client

    def ensure_bucket(self) -> None:
        """Create the bucket if it doesn't exist. Idempotent."""
        if not self.access_key:
            logger.warning("DO Spaces access_key not set — skipping bucket check")
            return
        try:
            client = self._get_client()
            existing = [b["Name"] for b in client.list_buckets().get("Buckets", [])]
            if self.bucket in existing:
                logger.info("DO Spaces bucket '%s' already exists", self.bucket)
                return
            client.create_bucket(Bucket=self.bucket)
            logger.info("Created DO Spaces bucket '%s'", self.bucket)
        except Exception:
            logger.exception("Failed to ensure DO Spaces bucket")

    def upload_file(self, local_path: str | Path, key: str, content_type: str = "") -> str:
        """Upload a local file. Returns the public CDN URL."""
        client = self._get_client()
        extra: dict = {"ACL": "public-read"}
        if content_type:
            extra["ContentType"] = content_type
        client.upload_file(str(local_path), self.bucket, key, ExtraArgs=extra)
        url = f"{self.cdn_url}/{key}"
        logger.debug("Uploaded %s -> %s", local_path, url)
        return url

    def upload_bytes(self, data: bytes, key: str, content_type: str = "") -> str:
        """Upload raw bytes. Returns the public CDN URL."""
        client = self._get_client()
        extra: dict = {"ACL": "public-read"}
        if content_type:
            extra["ContentType"] = content_type
        client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        url = f"{self.cdn_url}/{key}"
        logger.debug("Uploaded %d bytes -> %s", len(data), url)
        return url

    def download_file(self, key: str, local_path: str | Path) -> Path:
        """Download an object to a local file. Returns the resolved Path."""
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        client = self._get_client()
        client.download_file(self.bucket, key, str(dest))
        logger.debug("Downloaded %s -> %s", key, dest)
        return dest

    def delete_object(self, key: str) -> None:
        """Delete an object from the bucket."""
        client = self._get_client()
        client.delete_object(Bucket=self.bucket, Key=key)
        logger.debug("Deleted %s", key)

    def exists(self, key: str) -> bool:
        """Check if an object exists in the bucket."""
        try:
            client = self._get_client()
            client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def cdn_url_for(self, key: str) -> str:
        """Return the public CDN URL for a given key."""
        return f"{self.cdn_url}/{key}"


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def client_from_row(row: dict) -> SpacesClient:
    """Build a SpacesClient from a storage_backends DB row."""
    return SpacesClient(
        access_key=row["access_key"],
        secret_key=row["secret_key"],
        bucket=row["bucket"],
        region=row["region"],
        endpoint=row["endpoint"],
        cdn_url=row.get("cdn_url", ""),
    )


def client_from_env() -> SpacesClient:
    """Build from env vars (backward compat with analytics thumbnails)."""
    region = os.environ.get("DO_SPACES_REGION", "sgp1")
    return SpacesClient(
        access_key=os.environ.get("DO_SPACES_KEY", ""),
        secret_key=os.environ.get("DO_SPACES_SECRET", ""),
        bucket=os.environ.get("DO_SPACES_BUCKET", "sau-media"),
        region=region,
        endpoint=f"https://{region}.digitaloceanspaces.com",
        cdn_url=os.environ.get("DO_SPACES_CDN_URL", ""),
    )


# ---------------------------------------------------------------------------
# Lazy default client (backward-compatible module-level API)
# ---------------------------------------------------------------------------

_default: SpacesClient | None = None


def _default_client() -> SpacesClient:
    global _default
    if _default is None:
        _default = client_from_env()
    return _default


# Module-level functions delegate to the default client so existing callers
# (analytics_sync.py, sau_backend.py thumbnail proxy) work unchanged.

def ensure_bucket() -> None:
    _default_client().ensure_bucket()


def upload_file(local_path: str | Path, key: str, content_type: str = "") -> str:
    return _default_client().upload_file(local_path, key, content_type)


def upload_bytes(data: bytes, key: str, content_type: str = "") -> str:
    return _default_client().upload_bytes(data, key, content_type)


def exists(key: str) -> bool:
    return _default_client().exists(key)


def cdn_url(key: str) -> str:
    return _default_client().cdn_url_for(key)


# Module-level constants for backward compatibility (analytics_sync.py)
DO_SPACES_KEY = os.environ.get("DO_SPACES_KEY", "")
DO_SPACES_SECRET = os.environ.get("DO_SPACES_SECRET", "")
DO_SPACES_BUCKET = os.environ.get("DO_SPACES_BUCKET", "sau-media")
DO_SPACES_REGION = os.environ.get("DO_SPACES_REGION", "sgp1")
DO_SPACES_ENDPOINT = f"https://{DO_SPACES_REGION}.digitaloceanspaces.com"
DO_SPACES_BASE_URL = f"https://{DO_SPACES_BUCKET}.{DO_SPACES_REGION}.digitaloceanspaces.com"
DO_SPACES_CDN_URL = os.environ.get("DO_SPACES_CDN_URL", "") or DO_SPACES_BASE_URL
