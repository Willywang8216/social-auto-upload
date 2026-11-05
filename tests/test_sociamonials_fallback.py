"""Sociamonials HTTP contracts. Every GET/validate/transfer/create is mocked.

The preparation guard is injected for transport unit tests; that never bypasses
live-grant or strict-provider-validation mocks. Safety tests cover refusal paths.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from PIL import Image

from myUtils import delivery
from myUtils import sociamonials_fallback as sm
from myUtils import sociamonials_policy as policy


class Response:
    def __init__(self, payload=None, status=200, headers=None, content=b""):
        self.status_code, self.payload = status, payload
        self.headers, self.content = headers or {}, content
        self.text = "never log a provider body"
        self.closed = False

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return copy.deepcopy(self.payload)

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        self.closed = True


class Account:
    def __init__(self, account_id=77, platform="twitter", config=None, enabled=True):
        self.id, self.platform, self.config, self.enabled = account_id, platform, config or {}, enabled


def profile(network="tw", ref="14100", **changes):
    return {"network": network, "profile_ref": ref, "connected": True, "agent_can_publish_to": True,
            "unavailable_reason": None,
            "capabilities": ["text", "image", "video", "carousel", "first_comment", "story", "title", "description"],
            **changes}


def storage(**changes):
    return {"status": "ok", "max_upload_bytes": {"image": policy.IMAGE_MAX_BYTES, "video": policy.HOSTED_VIDEO_MAX_BYTES},
            "available_bytes": 4_000_000_000, "enforcement": True, "pooled": True,
            "unmetered": False, "unconfigured": False, "ratchet": {"active": False}, **changes}


def asset_for(path, asset_id=77, ws=26985, kind=None, **changes):
    return {"asset_id": asset_id, "workspace_registration_id": ws, "filename": Path(path).name,
            "size_bytes": Path(path).stat().st_size, "sha256": sm._sha256(Path(path)),
            "media_type": kind or sm._media_kind(str(path)), "processing_status": "ready",
            "parent_asset_id": None, "source": "upload", "url": f"https://cdn.example/assets/{asset_id}.mp4", **changes}


class ContractSession:
    """Explicit schema-shaped fake; unexpected endpoints/assertions fail loudly."""
    def __init__(self, *, profiles=None, assets=None, storage_block=None, override=None, part_size=None,
                 delivery_state="delivered", envelope=False):
        self.calls = []
        self.profiles = profiles if profiles is not None else [profile(entry["network"], ref)
            for entry in sm.SAU_ACCOUNT_TO_SOCIAMONIALS.values() for ref in entry["profile_refs"]]
        self.assets = {row["asset_id"]: copy.deepcopy(row) for row in assets or []}
        self.storage = storage_block if storage_block is not None else storage()
        self.override, self.part_size, self.delivery_state, self.envelope = override, part_size, delivery_state, envelope
        self.grants, self.grant_keys, self.parts, self.created = {}, {}, {}, {}
        self.last_body = None
        self.public = {}
        self.published = {}
        self.permissions = {name: True for name in ["posts.read", "posts.create", "profiles.read", "posts.publish_direct", "assets.read", "assets.write"]}
        self.approval = False
        self.agent_type = "workspace"
        self.ready_after = 0
        self.asset_polls = {}

    def _wrap(self, body):
        return {"data": body} if self.envelope else body

    def _resolution(self, body):
        return {"mode": "explicit", "preset": None, "warnings": [], "resolved_profiles": [
            {"network": network, "profile_ref": ref} for network, detail in body["networks"].items() for ref in detail["profile_refs"]]}

    def _call(self, method, url, **kwargs):
        recorded = dict(kwargs)
        if "json" in recorded:
            recorded["json"] = copy.deepcopy(recorded["json"])
        self.calls.append((method, url, recorded))
        if self.override:
            response = self.override(method, url, kwargs)
            if response is not None:
                return response
        path = urlparse(url).path
        if method == "GET" and path == "/api/v1/me":
            assigned = [{"network": row["network"], "profile_ref": row["profile_ref"]} for row in self.profiles]
            return Response(self._wrap({"agent": {"id": 1, "status": "active", "type": self.agent_type}, "credential": {"name": "fixture"},
                "entitlement": {"state": "active"}, "workspaces": {ws: {"permissions": self.permissions,
                    "social_profiles": assigned, "requires_approval_for_posting": self.approval} for ws in ("26985", "34293", "123")}}))
        if method == "GET" and path.endswith("/social-profiles"):
            ws = int(path.split("/")[-2])
            return Response(self._wrap({"workspace_registration_id": ws, "profiles": self.profiles}))
        if method == "GET" and path.endswith("/context"):
            ws = int(path.split("/")[-2])
            return Response(self._wrap({"workspace_registration_id": ws, "storage": self.storage,
                                       "approval": {"credential_routes_through_approval": False, "client_requested_hold": False}}))
        if method == "GET" and path == "/api/v1/media/assets":
            rows = list(self.assets.values())
            params = kwargs.get("params", {})
            start, limit = params.get("offset", 0), params.get("limit", 200)
            return Response(self._wrap({"assets": rows[start:start + limit]}))
        if method == "GET" and path.startswith("/api/v1/media/assets/"):
            asset_id = int(path.rsplit("/", 1)[1])
            if asset_id not in self.assets:
                return Response({}, 404)
            row = copy.deepcopy(self.assets[asset_id])
            count = self.asset_polls.get(asset_id, 0)
            self.asset_polls[asset_id] = count + 1
            if count < self.ready_after:
                row["processing_status"] = "processing"
            return Response(self._wrap(row))
        if method == "POST" and path == "/api/v1/media/uploads":
            body = kwargs["json"]
            key = body["idempotency_key"]
            upload_id = self.grant_keys.setdefault(key, 77 + len(self.grant_keys))
            self.grants[upload_id] = copy.deepcopy(body)
            return Response(self._wrap({"upload_id": upload_id, "workspace_registration_id": body["workspace_registration_id"],
                "mode": "multipart" if self.part_size else "single", "method": "PUT", "media_type": sm._media_kind(body["filename"]),
                "max_bytes": policy.HOSTED_VIDEO_MAX_BYTES, "required_headers": {"Content-Type": body["mime_type"]},
                "expires_at": "2099-01-01T00:00:00Z", "upload_url": f"https://upload.example/upload/{upload_id}/single?signature=fixture",
                "part_size_bytes": self.part_size, "part_count": (body["size_bytes"] + self.part_size - 1) // self.part_size if self.part_size else None,
                "parts": []}))
        if method == "POST" and path.endswith("/parts"):
            upload_id = int(path.split("/")[-2])
            first, count = kwargs["json"]["from"], kwargs["json"]["count"]
            parts = [{"part_number": n, "url": f"https://upload.example/upload/{upload_id}/part/{n}?signature=fixture"}
                     for n in range(first, first + count)]
            uploaded = [{"part_number": n, "etag": hashlib.md5(data).hexdigest(), "size_bytes": len(data)}
                        for n, data in self.parts.get(upload_id, {}).items()]
            return Response(self._wrap({"parts": parts, "uploaded": uploaded}))
        if method == "PUT":
            assert not kwargs["headers"].get("Authorization") and not kwargs["headers"].get("Cookie")
            assert kwargs["allow_redirects"] is False
            stream = kwargs["data"]
            assert not isinstance(stream, bytes)
            data = b"".join(iter(lambda: stream.read(8192), b""))
            assert len(data) == int(kwargs["headers"]["Content-Length"])
            split = path.split("/")
            upload_id = int(split[2])
            number = int(split[-1]) if "part" in split else 1
            self.parts.setdefault(upload_id, {})[number] = data
            return Response({}, headers={"ETag": '"' + hashlib.md5(data).hexdigest() + '"'})
        if method == "POST" and path.endswith("/complete"):
            upload_id = int(path.split("/")[-2])
            original = self.grants[upload_id]
            parts = self.parts[upload_id]
            data = b"".join(parts[n] for n in sorted(parts))
            assert len(data) == original["size_bytes"] and hashlib.sha256(data).hexdigest() == original["sha256"]
            row = {"asset_id": upload_id, "workspace_registration_id": original["workspace_registration_id"],
                   "media_type": sm._media_kind(original["filename"]), "filename": original["filename"], "size_bytes": len(data),
                   "sha256": original["sha256"], "processing_status": "ready", "parent_asset_id": None,
                   "url": f"https://cdn.example/assets/{upload_id}.mp4"}
            self.assets[upload_id] = row
            result = copy.deepcopy(row)
            if self.ready_after:
                result["processing_status"] = "processing"
            return Response(self._wrap(result))
        if method == "POST" and path == "/api/v1/posts":
            body = kwargs["json"]
            assert body["strict"] is True and body["mode"] == "publish_now"
            if body.get("validate_only") is True:
                return Response(self._wrap({"valid": True, "validate_only": True, "mode": body["mode"], "requires_approval": False,
                    "profile_resolution": self._resolution(body), "warnings": []}))
            self.last_body = copy.deepcopy(body)
            self.created.setdefault(body["idempotency_key"], 999)
            self.published[999] = self.last_body
            return Response(self._wrap({"post_id": 999, "status": "scheduled", "requires_approval": False,
                                        "message": body["message"], "profile_resolution": self._resolution(body), "warnings": []}))
        if method == "GET" and path.startswith("/api/v1/posts/"):
            body = self.published[int(path.rsplit("/", 1)[1])]
            state = self.delivery_state
            return Response(self._wrap({"post_id": 999, "workspace_registration_id": body["workspace_registration_id"], "approved": True,
                "networks": {network: {"delivered": state == "delivered", "delivery_status": state,
                    "profiles": [{"profile_ref": ref, "delivery_state": {"delivered": "published", "failed": "needs_attention", "pending": "accepted",
                                "processing": "publishing"}[state], "platform_post_id": "native-post-999" if state == "delivered" else None} for ref in detail["profile_refs"]]}
                             for network, detail in body["networks"].items()}}))
        if method == "GET" and url in self.public:
            return self.public[url]
        raise AssertionError(f"Unexpected mocked {method} {path}")

    def get(self, url, **kwargs): return self._call("GET", url, **kwargs)
    def post(self, url, **kwargs): return self._call("POST", url, **kwargs)
    def put(self, url, **kwargs): return self._call("PUT", url, **kwargs)

    def calls_for(self, method, contains="", *, create=False):
        return [call for call in self.calls if call[0] == method and contains in call[1]
                and (not create or not call[2].get("json", {}).get("validate_only"))]


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for name in (sm.API_KEY_ENV, sm.TEACHING_API_KEY_ENV, sm.WORKSPACE_ENV, sm.TEACHING_WORKSPACE_ENV,
                 sm.SECRETS_FILE_ENV, sm.ENABLED_ENV, sm.REUSE_ASSETS_ENV, sm.TIMEOUT_ENV, sm.ASSET_READY_TIMEOUT_ENV,
                 sm.DELIVERY_TIMEOUT_ENV, "SAU_SOCIAMONIALS_REUSE_MAX_PAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAU_TESTING", "1")
    monkeypatch.setenv("SAU_ALERTS_DISABLED", "1")
    monkeypatch.setattr(sm, "_resolve_public_host", lambda host: None)
    monkeypatch.setattr(sm.time, "sleep", lambda value: None)
    sm._RATE_WINDOWS.clear()
    from myUtils import publish_media_guard
    monkeypatch.setattr(publish_media_guard, "ensure_single_payload", lambda payload, **kw: None)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "pic.png"
    Image.new("RGB", (16, 16), "red").save(path)
    return path


@pytest.fixture
def video(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"exact-video-fixture" * 100)
    monkeypatch.setattr(sm.media_pipeline, "probe_video_duration", lambda path: 30.0)
    return path


def media_payload(path, *, message="Approved original", public_url=None, **metadata):
    return {"draft": {"message": message}, "artifacts": [{"local_path": str(path), "public_url": public_url,
        "metadata": {"role": sm._media_kind(str(path)), "sha256": sm._sha256(Path(path)), "size_bytes": Path(path).stat().st_size, **metadata}}]}


def publish(session, *, payload=None, account=None, platform="twitter", **kwargs):
    options = {"platform": platform, "account": account or Account(), "payload": payload if payload is not None else {"message": "Approved original"},
               "target_id": 4321, "api_key": "fixture-only", "session": session, "delivery_timeout": 0}
    options.update(kwargs)
    return sm.publish_via_sociamonials(**options)


def test_configuration_and_budget_guards(monkeypatch):
    assert not sm.is_configured({}) and not sm.is_enabled({})
    assert sm.is_configured({sm.API_KEY_ENV: "fixture"})
    monkeypatch.setenv(sm.API_KEY_ENV, "fixture")
    monkeypatch.setenv(sm.ENABLED_ENV, "1")
    assert sm.is_enabled()
    assert not sm.should_attempt_fallback(retryable=False, attempts=3, max_attempts=3, enabled=True)
    assert not sm.should_attempt_fallback(retryable=True, attempts=2, max_attempts=3, enabled=True)
    assert sm.should_attempt_fallback(retryable=True, attempts=3, max_attempts=3, enabled=True)


def test_explicit_credential_file_and_teaching_isolation(tmp_path, monkeypatch):
    file = tmp_path / "test-secrets.json"
    file.write_text(json.dumps({sm.API_KEY_ENV: "adult-fixture", sm.TEACHING_API_KEY_ENV: "teaching-fixture"}), encoding="utf-8")
    monkeypatch.setenv(sm.SECRETS_FILE_ENV, str(file))
    assert sm.get_api_key() == "adult-fixture"
    assert sm.resolve_credentials(sm.resolve_mapping(107)) == ("teaching-fixture", "34293")
    assert sm.resolve_credentials(sm.resolve_mapping(77)) == ("adult-fixture", "26985")


def test_config_mapping_precedence_and_no_guessed_new_maps():
    config = {"sociamonials": {"network": "tw", "profile_refs": ["custom"]}}
    assert sm.resolve_mapping(77, account=Account(config=config))["profile_refs"] == ["custom"]
    assert sm.resolve_mapping(77, platform="twitter", settings={"sociamonials": {"77": {"profile_ref": "settings"}}})["profile_refs"] == ["settings"]
    assert sm.resolve_mapping(107)["profile_refs"] == ["17650"]
    for account_id in (133, 134, 135, 136, 424242):
        assert sm.resolve_mapping(account_id) is None
    assert sm.resolve_mapping(77, account=Account(config={"sociamonials": {"network": "tw"}})) is None


@pytest.mark.parametrize("network", ["tw", "blsk", "fb", "tiktok"])
def test_compose_never_truncates_or_moves_original_urls(network):
    original = "  Long " + "x" * 400 + " https://a.example/original\n "
    assert sm.compose_message_with_links({"message": original}, network=network) == (original, [])


def test_structured_copy_preserves_title_body_and_tags_once():
    payload = {"draft": {"message": {"title": "Title", "description": "Body", "hashtags": ["#one", "two"]}}}
    assert sm.compose_message(payload, network="fb") == "Title\n\nBody\n\n#one #two"


def test_text_publish_has_verified_get_and_no_create_strict_gate():
    session = ContractSession()
    result = publish(session, payload={"draft": {"message": "Hello", "hashtags": ["one"]}})
    assert result["ok"] is True and result["delivered"] is True
    assert result["post_id"] == 999 and result["platform_post_id"] == "native-post-999"
    body = session.last_body
    assert body["message"] == "Hello\n\n#one" and body["networks"] == {"tw": {"profile_refs": ["14100"]}}
    assert body["idempotency_key"] == "sau-target-4321"
    assert [urlparse(c[1]).path for c in session.calls[:3]] == ["/api/v1/me", "/api/v1/workspaces/26985/social-profiles", "/api/v1/posts"]
    preview = session.calls_for("POST", "/posts")[0][2]["json"]
    assert preview["validate_only"] is True and preview["strict"] is True and preview["mode"] == "publish_now"
    assert not any("/posts/validate" in call[1] for call in session.calls)


@pytest.mark.parametrize("envelope", [False, True])
def test_verified_preparation_caps_are_secret_free(envelope):
    session = ContractSession(envelope=envelope)
    result = sm.verify_destination(platform="twitter", account=Account(), payload={"message": "Original"}, session=session, api_key="secret-fixture")
    assert result["verified"] is True and result["mapping"]["profile_refs"] == ["14100"]
    assert result["limits"]["hosted_video_max_bytes"] == 1_073_741_824
    assert result["limits"]["external_video_max_bytes"] == 1_024_000_000
    assert result["storage"]["pooled"] is True
    assert "secret-fixture" not in json.dumps(result)
    assert not session.calls_for("POST")


def test_upload_streams_exact_image_and_checkpoints_ready_receipt(image):
    session = ContractSession()
    payload = media_payload(image)
    snapshots = []
    payload["_save_checkpoint"] = lambda state: snapshots.append(copy.deepcopy(state))
    result = publish(session, platform="instagram", account=Account(72, "instagram"), payload=payload)
    assert result["ok"] is True and session.last_body["image_urls"] == ["asset://77"]
    grant = session.calls_for("POST", "/media/uploads")[0][2]["json"]
    assert grant["sha256"] == sm._sha256(image) and grant["idempotency_key"] == f"sau-media-26985-{grant['sha256']}"
    assert session.calls_for("PUT") and len(session.calls_for("POST", "/posts", create=True)) == 1
    saved = delivery.state(payload)["sociamonials_uploads"][grant["sha256"]]
    assert saved["asset_id"] == 77 and saved["processing_status"] == "ready"
    assert "signature" not in json.dumps(snapshots) and "fixture-only" not in json.dumps(snapshots)
    validations = [call for call in session.calls_for("POST", "/posts") if call[2]["json"].get("validate_only")]
    assert len(validations) == 2 and validations[-1][2]["json"]["image_urls"] == ["asset://77"]


def test_exact_ready_sha_asset_is_reused_even_with_renamed_file(image):
    existing = asset_for(image, asset_id=555, filename="a-different-name.png")
    session = ContractSession(assets=[existing])
    result = publish(session, platform="instagram", account=Account(72, "instagram"), payload=media_payload(image))
    assert result["ok"] and session.last_body["image_urls"] == ["asset://555"]
    assert not session.calls_for("PUT") and not session.calls_for("POST", "/media/uploads")


def test_filename_size_without_hash_is_not_byte_equality(image):
    existing = asset_for(image, asset_id=555, sha256=None)
    session = ContractSession(assets=[existing])
    publish(session, platform="instagram", account=Account(72, "instagram"), payload=media_payload(image))
    assert session.calls_for("POST", "/media/uploads")
    assert sm.select_reusable_asset([existing], filename=image.name, size_bytes=image.stat().st_size, kind="image") is None


def test_multipart_uses_documented_batched_authenticated_parts_and_etags(video):
    session = ContractSession(part_size=64)
    result = publish(session, payload=media_payload(video))
    assert result["ok"]
    parts = session.calls_for("POST", "/parts")
    assert all(set(call[2]["json"]) == {"from", "count"} for call in parts)
    assert all(call[2]["headers"]["Authorization"] == "Bearer fixture-only" for call in parts)
    complete = session.calls_for("POST", "/complete")[0][2]["json"]["parts"]
    assert len(complete) == (video.stat().st_size + 63) // 64
    assert all(part["etag"] for part in complete)


def test_hosted_video_processing_is_bounded_and_resume_does_not_reupload(video):
    session = ContractSession()
    session.ready_after = 99
    payload = media_payload(video)
    with pytest.raises(delivery.DeliveryPending):
        publish(session, payload=payload)
    assert len(session.calls_for("GET", "/media/assets/")) == sm.MAX_POLL_REQUESTS
    assert not session.calls_for("POST", "/posts", create=True)
    session.ready_after = 0
    result = publish(session, payload=payload)
    assert result["ok"] and len(session.calls_for("POST", "/media/uploads")) == 2  # grant + complete, not another grant
    assert len(session.calls_for("PUT")) == 1


@pytest.mark.parametrize("state", ["pending", "processing", "failed"])
def test_acceptance_is_not_delivery_and_receipt_is_retained(state):
    session = ContractSession(delivery_state=state)
    payload = {"message": "Approved"}
    result = publish(session, payload=payload)
    assert not result["ok"] and result["delivery_state"] == state
    assert delivery.receipt_for(payload, "sociamonials")["id"] == "999"
    publish(session, payload=payload)
    assert len(session.calls_for("POST", "/posts", create=True)) == 1


def test_transient_create_retry_reuses_key_and_previously_uploaded_asset(image):
    counts = {"create": 0}
    def override(method, url, kwargs):
        if method == "POST" and url == sm.POSTS_URL and not kwargs["json"].get("validate_only"):
            counts["create"] += 1
            if counts["create"] == 1:
                return Response({"error": {"code": "rate_limited"}}, 429, {"Retry-After": "120"})
    session = ContractSession(override=override)
    payload = media_payload(image)
    with pytest.raises(sm.SociamonialsFallbackError) as caught:
        publish(session, platform="instagram", account=Account(72, "instagram"), payload=payload)
    assert caught.value.retryable and caught.value.details["retryAfter"] == "120"
    assert caught.value.details["retrySafe"] is True
    assert delivery.state(payload)["submission_started"] is False
    publish(session, platform="instagram", account=Account(72, "instagram"), payload=payload)
    keys = [call[2]["json"]["idempotency_key"] for call in session.calls_for("POST", "/posts", create=True)]
    assert keys == ["sau-target-4321", "sau-target-4321"] and len(session.calls_for("PUT")) == 1


def test_recovery_generation_only_changes_deliberate_recovery_key():
    session = ContractSession()
    for generation in (None, "a" * 64, "a" * 64, "b" * 64):
        payload = {"message": "Original"}
        if generation:
            payload["_delivery"] = {"recovery_generation": generation}
        publish(session, payload=payload)
    keys = [call[2]["json"]["idempotency_key"] for call in session.calls_for("POST", "/posts", create=True)]
    assert keys == ["sau-target-4321", "sau-target-4321-recovery-" + "a" * 64,
                    "sau-target-4321-recovery-" + "a" * 64, "sau-target-4321-recovery-" + "b" * 64]


def test_teaching_primary_uses_only_teaching_key_and_workspace(monkeypatch):
    monkeypatch.setenv(sm.TEACHING_API_KEY_ENV, "teach-fixture")
    monkeypatch.setenv(sm.API_KEY_ENV, "adult-fixture")
    session = ContractSession()
    result = publish(session, account=Account(107), api_key=None)
    assert result["workspace_id"] == 34293 and session.last_body["networks"] == {"tw": {"profile_refs": ["17650"]}}
    assert all(call[2].get("headers", {}).get("Authorization") == "Bearer teach-fixture" for call in session.calls)
