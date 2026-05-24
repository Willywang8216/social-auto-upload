"""DigitalOcean Spaces (S3-compatible) storage client.

Provides helpers to upload files and bytes to DO Spaces and return
public CDN URLs.  Credentials are read from environment variables.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)

DO_SPACES_KEY = os.environ.get("DO_SPACES_KEY", "")
DO_SPACES_SECRET = os.environ.get("DO_SPACES_SECRET", "")
DO_SPACES_BUCKET = os.environ.get("DO_SPACES_BUCKET", "sau-media")
DO_SPACES_REGION = os.environ.get("DO_SPACES_REGION", "sgp1")
DO_SPACES_ENDPOINT = f"https://{DO_SPACES_REGION}.digitaloceanspaces.com"
DO_SPACES_BASE_URL = f"https://{DO_SPACES_BUCKET}.{DO_SPACES_REGION}.digitaloceanspaces.com"
DO_SPACES_CDN_URL = os.environ.get("DO_SPACES_CDN_URL", "") or DO_SPACES_BASE_URL


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=DO_SPACES_ENDPOINT,
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET,
        region_name=DO_SPACES_REGION,
        config=BotoConfig(s3={"addressing_style": "virtual"}),
    )


def ensure_bucket() -> None:
    """Create the bucket if it doesn't exist. Idempotent."""
    if not DO_SPACES_KEY:
        logger.warning("DO_SPACES_KEY not set — skipping bucket check")
        return
    try:
        client = _get_client()
        existing = [b["Name"] for b in client.list_buckets().get("Buckets", [])]
        if DO_SPACES_BUCKET in existing:
            logger.info("DO Spaces bucket '%s' already exists", DO_SPACES_BUCKET)
            return
        client.create_bucket(Bucket=DO_SPACES_BUCKET)
        logger.info("Created DO Spaces bucket '%s'", DO_SPACES_BUCKET)
    except Exception:
        logger.exception("Failed to ensure DO Spaces bucket")


def upload_file(local_path: str | Path, key: str, content_type: str = "") -> str:
    """Upload a local file to DO Spaces. Returns the public CDN URL."""
    client = _get_client()
    extra: dict = {"ACL": "public-read"}
    if content_type:
        extra["ContentType"] = content_type
    client.upload_file(str(local_path), DO_SPACES_BUCKET, key, ExtraArgs=extra)
    url = f"{DO_SPACES_CDN_URL}/{key}"
    logger.debug("Uploaded %s -> %s", local_path, url)
    return url


def upload_bytes(data: bytes, key: str, content_type: str = "") -> str:
    """Upload raw bytes to DO Spaces. Returns the public CDN URL."""
    client = _get_client()
    extra: dict = {"ACL": "public-read"}
    if content_type:
        extra["ContentType"] = content_type
    client.put_object(Bucket=DO_SPACES_BUCKET, Key=key, Body=data, **extra)
    url = f"{DO_SPACES_CDN_URL}/{key}"
    logger.debug("Uploaded %d bytes -> %s", len(data), url)
    return url


def exists(key: str) -> bool:
    """Check if an object exists in the bucket."""
    try:
        client = _get_client()
        client.head_object(Bucket=DO_SPACES_BUCKET, Key=key)
        return True
    except Exception:
        return False


def cdn_url(key: str) -> str:
    """Return the public CDN URL for a given key."""
    return f"{DO_SPACES_CDN_URL}/{key}"
