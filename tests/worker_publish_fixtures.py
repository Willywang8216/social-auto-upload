"""Owned SQLite facts and read-only native proof for the worker hook tests.

These helpers do not patch admission, preparation, restoration or queue CAS.
Only the codec probe / provider transports are replaced by individual tests.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from myUtils import campaigns, profiles, publish_readiness
from myUtils.sqlite_utils import connect

WORKSPACE_ID = "eeeeeeee-1111-4111-8111-000000000001"


def create_test_profile(path: Path, name: str):
    with connect(path) as conn:
        conn.execute("INSERT OR IGNORE INTO workspaces (id,name,slug) VALUES (?,?,?)",
                     (WORKSPACE_ID, "Worker fixture", "worker-fixture"))
    return profiles.create_profile(name, workspace_id=WORKSPACE_ID, db_path=path)


def native_config(platform: str) -> dict:
    """Explicit offline identities and native credentials, not a managed grant."""
    if platform == "bluesky":
        return {"did": "did:plc:worker-fixture", "handle": "worker-fixture.test",
                "appPassword": "fixture-only"}
    identity = {"twitter": "twitterUserId", "reddit": "userId"}[platform]
    return {identity: f"offline-{platform}-fixture", "accessToken": "fixture-access",
            "refreshToken": "fixture-refresh", "accessTokenExpiresAt": "2099-01-01T00:00:00+00:00",
            **({"twitterAuthType": "api"} if platform == "twitter" else {})}


def record_native_proof(path: Path, account):
    """Persist an exact read-only native-provider response, never ready=True JSON."""
    settings = profiles.get_profile(account.profile_id, db_path=path).settings
    context = publish_readiness._context(account, settings)
    assert context["provider"] == "direct" and context["identity"]
    return publish_readiness.record_destination_readiness(account, settings, provider="direct", db_path=path,
        proof={"verified": True, "live_check": True, "source": "provider_verification",
               "requires_approval": False, "config_fingerprint": context["fingerprint"],
               "identity": context["identity"], "permissions": {"posts.create": True},
               "capabilities": ["text", "image", "carousel", "video"],
               "verified_at": datetime.now(timezone.utc).isoformat()})


def video_probe(path) -> dict:
    """Codec-only fixture; production byte/hash/size and limit checks still run."""
    assert Path(path).is_file()
    return {"duration": 30.0, "width": 1280, "height": 720, "fps": 30.0,
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2", "major_brand": "isom",
            "codec": "h264", "pix_fmt": "yuv420p", "profile": "High",
            "field_order": "progressive", "sample_aspect_ratio": "1:1",
            "rotation": 0, "has_audio": False, "video_bit_rate": 1_000_000}


def campaign_media_payload(path: Path, account, media: Path, draft: dict, *, public_url=None) -> dict:
    """Register exact approved bytes, campaign ownership and its real SQL post."""
    digest, size = hashlib.sha256(media.read_bytes()).hexdigest(), media.stat().st_size
    with connect(path) as conn:
        group_id = conn.execute("INSERT INTO media_groups (name,status,workspace_id) VALUES (?,?,?)",
                                ("Worker fixture", "ready", account.workspace_id)).lastrowid
        file_id = conn.execute(
            "INSERT INTO file_records (filename,file_path,filesize,sha256,size_bytes,workspace_id) VALUES (?,?,?,?,?,?)",
            (media.name, str(media), size, digest, size, account.workspace_id)).lastrowid
    campaign = campaigns.create_campaign(profile_id=account.profile_id, media_group_id=group_id,
        status="publishing", selected_account_ids=[account.id], workspace_id=account.workspace_id, db_path=path)
    kind = "source_image" if media.suffix.lower() in {".png", ".jpg", ".jpeg"} else "source_video"
    artifact = campaigns.add_campaign_artifact(campaign.id, artifact_kind=kind, source_file_record_id=file_id,
        local_path=str(media), public_url=public_url,
        metadata={"role": "image" if kind == "source_image" else "video", "sha256": digest, "size_bytes": size},
        db_path=path)
    post = campaigns.add_campaign_post(campaign.id, account.platform, account_ids=[account.id], draft=draft,
                                       status="queued", file_record_ids=[file_id], db_path=path)
    return {"campaignId": campaign.id, "campaignPostId": post.id, "draft": draft,
            "artifacts": [{"id": artifact.id, "campaign_id": campaign.id, "artifact_kind": artifact.artifact_kind,
                "source_file_record_id": file_id, "local_path": artifact.local_path, "public_url": artifact.public_url,
                "sha256": digest, "size_bytes": size, "metadata": dict(artifact.metadata)}]}
