"""Provider receipt/restart contracts. All responses and credentials are synthetic.

The shared conftest network guard also prevents an accidentally unmocked call.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from tests.test_prepared_publishers import _FakeResponse as Response
from tests.test_prepared_publishers import _RecordingSession
from myUtils import delivery, generic_delivery
from myUtils import prepared_publishers as publishers


class Session(_RecordingSession):
    def _next(self):
        if not self.responses:
            pytest.fail(f"Unexpected HTTP request: {self.calls[-1][:2]}")
        return super()._next()


class Checkpoints:
    def __init__(self, original):
        self.original = copy.deepcopy(original)
        self.saved = []
        self.reject = lambda state: False

    def save(self, state):
        if self.reject(state):
            raise delivery.LeaseLost("synthetic fenced checkpoint rejection")
        self.saved.append(copy.deepcopy(state))

    def restart(self, **overrides):
        return {**copy.deepcopy(self.original), **overrides,
                "_delivery": copy.deepcopy(self.saved[-1]) if self.saved else {},
                "_save_checkpoint": self.save}

    def items(self, provider):
        return self.saved[-1]["provider_operations"][provider]["items"]


PROVIDERS = ["twitter", "discord", "facebook", "teaching_blog", "nw_sw_blog", "bluesky"]


def case(provider):
    config = {}
    prefix = []
    posted = {"id": "post-1"}
    payload = {"message": "Synthetic post"}
    if provider == "twitter":
        config = {"accessToken": "synthetic-oauth-token"}
        posted = {"data": {"id": "post-1", "text": "Synthetic post"}}
    elif provider == "discord":
        config = {"webhookUrl": "https://discord.invalid/api/webhooks/id/synthetic-webhook-secret?wait=false"}
    elif provider == "facebook":
        config = {"pageId": "page-1", "accessToken": "synthetic-page-token"}
    elif provider in {"teaching_blog", "nw_sw_blog"}:
        config = {"repoOwner": "synthetic", "repoName": "fixture", "githubToken": "synthetic-github-token",
                  "persona": "nakedwill", "locales": "en"}
        prefix = [Response(status_code=404)] * (2 if provider == "nw_sw_blog" else 1)
        posted = {"content": {"sha": "content-1", "html_url": "https://github.invalid/fixture"}}
    elif provider == "bluesky":
        config = {"handle": "fixture.invalid", "appPassword": "synthetic-app-password"}
        prefix = [Response({"accessJwt": "synthetic-jwt", "did": "did:plc:fixture", "handle": "fixture.invalid"})]
        posted = {"uri": "at://did:plc:fixture/app.bsky.feed.post/1", "cid": "cid-1"}
    return getattr(publishers, f"publish_{provider}_sync"), SimpleNamespace(config=config), payload, prefix, posted


@pytest.mark.parametrize("provider", PROVIDERS)
def test_known_429_clears_only_current_request_and_can_retry(provider):
    publish, account, original, prefix, posted = case(provider)
    store = Checkpoints(original)
    first = Session([*prefix, Response({"error": {"message": "synthetic rate limit"}},
                                     status_code=429, headers={"Retry-After": "7"})])
    with pytest.raises(publishers.PreparedPublishError) as error:
        publish(account, store.restart(), session=first)
    assert error.value.retryable
    assert error.value.details["retryAfter"] == "7"
    assert not store.saved[-1]["submission_started"]
    assert not store.items(provider)
    assert not delivery.receipt_for({"_delivery": store.saved[-1]}, provider)
    second = Session([*prefix, Response(posted)])
    publish(account, store.restart(), session=second)
    assert store.saved[-1]["provider_receipt"]["status"] == "delivered"
    assert any(state.get("submission_started") for state in store.saved)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("failure", ["transport", "server", "missing_id"])
def test_lost_create_response_never_resends_on_restart(provider, failure):
    publish, account, original, prefix, _ = case(provider)
    failed = {"transport": TimeoutError("synthetic lost response"),
              "server": Response(status_code=503), "missing_id": Response({})}[failure]
    store = Checkpoints(original)
    with pytest.raises(delivery.DeliveryUnknown):
        publish(account, store.restart(), session=Session([*prefix, failed]))
    assert store.saved[-1]["submission_started"]
    resumed = Session([])
    with pytest.raises(delivery.DeliveryUnknown):
        publish(account, store.restart(), session=resumed)
    assert resumed.calls == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_receipt_checkpoint_never_contains_credentials_or_raw_response(provider):
    publish, account, original, prefix, posted = case(provider)
    store = Checkpoints(original)
    posted = {**posted, "access_token": "RAW_RESPONSE_SECRET",
              "upload_url": "https://upload.invalid/?signature=RAW_SIGNED_SECRET"}
    publish(account, store.restart(), session=Session([*prefix, Response(posted)]))
    encoded = json.dumps(store.saved)
    for secret in ("RAW_RESPONSE_SECRET", "RAW_SIGNED_SECRET", "synthetic-oauth-token", "synthetic-page-token",
                   "synthetic-github-token", "synthetic-jwt", "synthetic-app-password", "synthetic-webhook-secret"):
        assert secret not in encoded
    assert store.saved[-1]["provider_receipt"]["status"] == "delivered"
    assert all(state.get("provider_receipt", {}).get("status") != "delivered" for state in store.saved[:-1])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_lease_loss_before_create_fences_the_write(provider):
    publish, account, original, prefix, _ = case(provider)
    store = Checkpoints(original)
    store.reject = lambda state: state.get("submission_started")
    session = Session(prefix)
    with pytest.raises(delivery.LeaseLost):
        publish(account, store.restart(), session=session)
    assert len(session.calls) == len(prefix)  # auth/read-only probes only


@pytest.mark.parametrize("provider", ["twitter", "facebook", "discord", "bluesky"])
def test_lease_loss_after_create_keeps_durable_ambiguity_and_never_resends(provider):
    publish, account, original, prefix, posted = case(provider)
    store = Checkpoints(original)
    store.reject = lambda state: bool(state.get("provider_receipt"))
    with pytest.raises(delivery.LeaseLost):
        publish(account, store.restart(), session=Session([*prefix, Response(posted)]))
    assert any(item["status"] == "started" for item in store.items(provider).values())
    resumed = Session([])
    with pytest.raises(delivery.DeliveryUnknown):
        publish(account, store.restart(), session=resumed)
    assert resumed.calls == []


def photos():
    return {"message": "Synthetic album", "artifacts": [
        {"public_url": f"https://cdn.invalid/{index}.jpg", "artifact_kind": "image"} for index in range(2)
    ]}


@pytest.mark.parametrize("failure", [429, 503, "transport"])
def test_facebook_partial_album_preserves_first_photo(failure):
    account = SimpleNamespace(config={"pageId": "page", "accessToken": "synthetic-page-token"})
    store = Checkpoints(photos())
    failed = TimeoutError("lost photo") if failure == "transport" else Response(status_code=failure)
    error = publishers.PreparedPublishError if failure == 429 else delivery.DeliveryUnknown
    with pytest.raises(error):
        publishers.publish_facebook_sync(account, store.restart(), session=Session([Response({"id": "photo-1"}), failed]))
    assert store.items("facebook")["photo:0"]["id"] == "photo-1"
    assert store.saved[-1]["provider_receipt"]["status"] != "delivered"
    if failure == 429:
        assert "photo:1" not in store.items("facebook")
        resumed = Session([Response({"id": "photo-2"}), Response({"id": "post-1"})])
        result = publishers.publish_facebook_sync(account, store.restart(), session=resumed)
        assert [item["id"] for item in result["results"]] == ["photo-1", "photo-2", "post-1"]
        assert len(resumed.calls) == 2
        attached = json.loads(resumed.calls[-1][2]["data"]["attached_media"])
        assert attached == [{"media_fbid": "photo-1"}, {"media_fbid": "photo-2"}]
        assert store.saved[-1]["provider_receipt"]["status"] == "delivered"
    else:
        resumed = Session([])
        with pytest.raises(delivery.DeliveryUnknown):
            publishers.publish_facebook_sync(account, store.restart(), session=resumed)
        assert resumed.calls == []


def test_facebook_comment_rejection_retries_comment_not_post():
    account = SimpleNamespace(config={"pageId": "page", "accessToken": "synthetic-token"})
    store = Checkpoints({"message": "Synthetic post", "firstComment": "Synthetic comment"})
    with pytest.raises(publishers.PreparedPublishError):
        publishers.publish_facebook_sync(account, store.restart(), session=Session([
            Response({"id": "post-1"}), Response(status_code=429)]))
    assert store.items("facebook")["post"]["id"] == "post-1"
    assert store.saved[-1]["provider_receipt"]["status"] != "delivered"
    resumed = Session([Response({"id": "comment-1"})])
    publishers.publish_facebook_sync(account, store.restart(), session=resumed)
    assert len(resumed.calls) == 1 and resumed.calls[0][1].endswith("/post-1/comments")


def test_facebook_processing_receipt_resumes_without_local_media():
    account = SimpleNamespace(config={"pageId": "page", "accessToken": "synthetic-token"})
    store = Checkpoints({"message": "Video", "artifacts": [
        {"public_url": "https://cdn.invalid/video.mp4", "artifact_kind": "video"}]})
    with pytest.raises(delivery.DeliveryPending):
        publishers.publish_facebook_sync(account, store.restart(), session=Session([
            Response({"id": "video-1"}), Response({"id": "video-1", "status": {"video_status": "processing"}})]))
    assert store.saved[-1]["provider_receipt"]["id"] == "video-1"
    assert store.items("facebook")["video"]["status"] == "processing"
    resumed = Session([Response({"id": "video-1", "status": {"video_status": "ready"}, "published": True})])
    result = publishers.publish_facebook_sync(account, store.restart(artifacts=[{"artifact_kind": "missing"}]), session=resumed)
    assert result["results"] == [{"id": "video-1"}]
    assert [call[0] for call in resumed.calls] == ["GET"]
    assert store.saved[-1]["provider_receipt"]["status"] == "delivered"


def youtube_case(tmp_path, *, playlist=False):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic video")
    config = {"channelId": "channel", "clientId": "synthetic-client", "clientSecret": "synthetic-secret",
              "refreshToken": "synthetic-refresh", "privacyStatus": "public"}
    if playlist:
        config["playlistId"] = "playlist-1"
    return SimpleNamespace(config=config), {"message": "Video", "artifacts": [{"local_path": str(path), "artifact_kind": "video"}]}


def youtube_init():
    return [Response({"access_token": "synthetic-google-token"}),
            Response(headers={"Location": "https://upload.invalid/resumable?signature=SIGNED_UPLOAD_SECRET"})]


def youtube_status(uploaded="processed", privacy="public"):
    return Response({"items": [{"id": "video-1", "status": {"uploadStatus": uploaded, "privacyStatus": privacy}}]})


def test_youtube_processing_restart_uses_video_id_and_no_upload_url(tmp_path):
    account, original = youtube_case(tmp_path)
    store = Checkpoints(original)
    with pytest.raises(delivery.DeliveryPending):
        publishers.publish_youtube_sync(account, store.restart(), session=Session([
            *youtube_init(), Response({"id": "video-1"}), youtube_status("uploaded")]))
    assert store.items("youtube")["video"]["status"] == "processing"
    checkpoint = json.dumps(store.saved)
    assert "SIGNED_UPLOAD_SECRET" not in checkpoint and "synthetic-google-token" not in checkpoint
    resumed = Session([Response({"access_token": "new-synthetic-token"}), youtube_status()])
    result = publishers.publish_youtube_sync(account, store.restart(artifacts=[]), session=resumed)
    assert result["id"] == "video-1"
    assert [call[0] for call in resumed.calls] == ["POST", "GET"]
    assert resumed.calls[0][1] == publishers.GOOGLE_TOKEN_URL
    assert store.saved[-1]["provider_receipt"]["status"] == "delivered"


@pytest.mark.parametrize("failure", [429, 503, "transport"])
def test_youtube_playlist_failure_never_reuploads_video(tmp_path, failure):
    account, original = youtube_case(tmp_path, playlist=True)
    store = Checkpoints(original)
    failed = TimeoutError("lost playlist create") if failure == "transport" else Response(status_code=failure)
    error = publishers.PreparedPublishError if failure == 429 else delivery.DeliveryUnknown
    with pytest.raises(error):
        publishers.publish_youtube_sync(account, store.restart(), session=Session([
            *youtube_init(), Response({"id": "video-1"}), youtube_status(), failed]))
    assert store.items("youtube")["video"]["status"] == "complete"
    assert store.saved[-1]["provider_receipt"]["status"] != "delivered"
    if failure == 429:
        resumed = Session([Response({"access_token": "synthetic-token"}), Response({"id": "playlist-item-1"})])
        publishers.publish_youtube_sync(account, store.restart(artifacts=[]), session=resumed)
        assert [call[1] for call in resumed.calls] == [publishers.GOOGLE_TOKEN_URL, publishers.YOUTUBE_PLAYLIST_INSERT_URL]
        assert store.saved[-1]["provider_receipt"]["id"] == "video-1"
        assert store.saved[-1]["provider_receipt"]["status"] == "delivered"
    else:
        resumed = Session([])
        with pytest.raises(delivery.DeliveryUnknown):
            publishers.publish_youtube_sync(account, store.restart(), session=resumed)
        assert resumed.calls == []


@pytest.mark.parametrize("stage", ["initialize", "transfer"])
def test_youtube_lost_upload_response_never_creates_again(tmp_path, stage):
    account, original = youtube_case(tmp_path)
    store = Checkpoints(original)
    prefix = youtube_init() if stage == "transfer" else youtube_init()[:1]
    with pytest.raises(delivery.DeliveryUnknown):
        publishers.publish_youtube_sync(account, store.restart(), session=Session([*prefix, TimeoutError("lost upload")]))
    resumed = Session([])
    with pytest.raises(delivery.DeliveryUnknown):
        publishers.publish_youtube_sync(account, store.restart(), session=resumed)
    assert resumed.calls == []


@pytest.mark.parametrize("stage", ["initialize", "transfer"])
def test_youtube_explicit_upload_rejection_is_retry_safe(tmp_path, stage):
    account, original = youtube_case(tmp_path)
    store = Checkpoints(original)
    prefix = youtube_init() if stage == "transfer" else youtube_init()[:1]
    with pytest.raises(publishers.PreparedPublishError):
        publishers.publish_youtube_sync(account, store.restart(), session=Session([*prefix, Response(status_code=429)]))
    assert not store.saved[-1]["submission_started"]
    publishers.publish_youtube_sync(account, store.restart(), session=Session([
        *youtube_init(), Response({"id": "video-1"}), youtube_status()]))
    assert store.saved[-1]["provider_receipt"]["status"] == "delivered"


@pytest.mark.parametrize("provider", ["facebook", "youtube"])
def test_processing_deadline_is_bounded_and_not_reset(tmp_path, provider):
    if provider == "facebook":
        account = SimpleNamespace(config={"pageId": "page", "accessToken": "synthetic"})
        original = {"artifacts": [{"public_url": "https://cdn.invalid/video.mp4"}]}
        initial = [Response({"id": "video-1"}), Response({"id": "video-1", "status": {"video_status": "processing"}})]
        polling = [Response({"id": "video-1", "status": {"video_status": "processing"}})]
    else:
        account, original = youtube_case(tmp_path)
        initial = [*youtube_init(), Response({"id": "video-1"}), youtube_status("uploaded")]
        polling = [Response({"access_token": "synthetic"}), youtube_status("uploaded")]
    publish = getattr(publishers, f"publish_{provider}_sync")
    store = Checkpoints(original)
    with pytest.raises(delivery.DeliveryPending):
        publish(account, store.restart(), session=Session(initial))
    store.saved[-1]["provider_receipt"]["submitted_at"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(delivery.DeliveryUnknown, match="deadline"):
        publish(account, store.restart(), session=Session(polling))
    assert store.saved[-1]["provider_receipt"]["submitted_at"] == "2000-01-01T00:00:00+00:00"


@pytest.mark.parametrize("provider", ["instagram", "threads"])
def test_meta_lost_create_blocks_recreation(provider):
    account = SimpleNamespace(config={"igUserId": "user", "threadUserId": "user", "accessToken": "synthetic",
                                      "accessTokenExpiresAt": "2099-01-01T00:00:00"})
    store = Checkpoints({"message": "Post", "artifacts": [{"public_url": "https://cdn.invalid/image.jpg"}]})
    publish = getattr(publishers, f"publish_{provider}_sync")
    with pytest.raises(delivery.DeliveryUnknown):
        publish(account, store.restart(), session=Session([TimeoutError("lost create")]))
    resumed = Session([])
    with pytest.raises(delivery.DeliveryUnknown):
        publish(account, store.restart(), session=resumed)
    assert resumed.calls == []


@pytest.mark.parametrize("provider", ["instagram", "threads"])
@pytest.mark.parametrize("failure", ["transport", "server"])
def test_meta_lost_publish_only_polls_and_bypasses_missing_media(provider, failure):
    account = SimpleNamespace(config={"igUserId": "user", "threadUserId": "user", "accessToken": "synthetic",
                                      "accessTokenExpiresAt": "2099-01-01T00:00:00"})
    store = Checkpoints({"message": "Post", "artifacts": [{"public_url": "https://cdn.invalid/image.jpg"}]})
    publish = getattr(publishers, f"publish_{provider}_sync")
    field = "status_code" if provider == "instagram" else "status"
    failed = TimeoutError("lost publish") if failure == "transport" else Response(status_code=503)
    with pytest.raises(delivery.DeliveryPending):
        publish(account, store.restart(), session=Session([Response({"id": "container-1"}), Response({field: "FINISHED"}), failed]))
    assert store.saved[-1]["provider_receipt"]["stage"] == "publishing"
    resumed = Session([Response({field: "FINISHED"})])
    with pytest.raises(delivery.DeliveryPending):
        publish(account, store.restart(artifacts=[{"artifact_kind": "missing"}]), session=resumed)
    assert [call[0] for call in resumed.calls] == ["GET"]
    final = Session([Response({field: "PUBLISHED"})])
    publish(account, store.restart(artifacts=[]), session=final)
    assert [call[0] for call in final.calls] == ["GET"]
    assert store.saved[-1]["provider_receipt"]["status"] == "delivered"


@pytest.mark.parametrize("provider", ["instagram", "threads"])
def test_meta_carousel_child_receipts_survive_second_child_429(provider):
    account = SimpleNamespace(config={"igUserId": "user", "threadUserId": "user", "accessToken": "synthetic",
                                      "accessTokenExpiresAt": "2099-01-01T00:00:00"})
    store = Checkpoints(photos())
    publish = getattr(publishers, f"publish_{provider}_sync")
    with pytest.raises(publishers.PreparedPublishError):
        publish(account, store.restart(), session=Session([Response({"id": "child-1"}), Response(status_code=429)]))
    assert store.items(provider)["child:0"]["id"] == "child-1"
    assert "child:1" not in store.items(provider)
    assert not store.saved[-1].get("provider_receipt")  # a child is not the final container
    field = "status_code" if provider == "instagram" else "status"
    resumed = Session([Response({"id": "child-2"}), Response({"id": "container-1"}),
                       Response({field: "FINISHED"}), Response({"id": "post-1"})])
    publish(account, store.restart(), session=resumed)
    assert len(resumed.calls) == 4
    assert resumed.calls[1][2]["data"]["children"] == "child-1,child-2"
    assert store.saved[-1]["provider_receipt"]["status"] == "delivered"


@pytest.mark.parametrize("provider", ["instagram", "threads"])
def test_meta_publish_429_keeps_container_and_retries_only_publish(provider):
    account = SimpleNamespace(config={"igUserId": "user", "threadUserId": "user", "accessToken": "synthetic",
                                      "accessTokenExpiresAt": "2099-01-01T00:00:00"})
    store = Checkpoints({"message": "Post", "artifacts": [{"public_url": "https://cdn.invalid/image.jpg"}]})
    publish = getattr(publishers, f"publish_{provider}_sync")
    field = "status_code" if provider == "instagram" else "status"
    with pytest.raises(publishers.PreparedPublishError):
        publish(account, store.restart(), session=Session([
            Response({"id": "container-1"}), Response({field: "FINISHED"}), Response(status_code=429)]))
    assert store.saved[-1]["provider_receipt"]["stage"] == "container"
    resumed = Session([Response({field: "FINISHED"}), Response({"id": "post-1"})])
    publish(account, store.restart(artifacts=[]), session=resumed)
    assert len(resumed.calls) == 2
    assert resumed.calls[1][1].endswith("_publish")


def test_instagram_comment_loss_is_not_swallowed_or_marked_delivered():
    account = SimpleNamespace(config={"igUserId": "user", "accessToken": "synthetic"})
    store = Checkpoints({"firstComment": "Comment", "artifacts": [{"public_url": "https://cdn.invalid/image.jpg"}]})
    with pytest.raises(delivery.DeliveryUnknown):
        publishers.publish_instagram_sync(account, store.restart(), session=Session([
            Response({"id": "container-1"}), Response({"status_code": "FINISHED"}),
            Response({"id": "post-1"}), TimeoutError("lost comment")]))
    assert store.saved[-1]["provider_receipt"]["status"] != "delivered"
    assert store.saved[-1]["provider_receipt"]["media_id"] == "post-1"
    resumed = Session([])
    with pytest.raises(delivery.DeliveryUnknown):
        publishers.publish_instagram_sync(account, store.restart(), session=resumed)
    assert resumed.calls == []


def test_nw_sw_blog_second_locale_rejection_never_rewrites_first_locale():
    publish, account, original, prefix, _ = case("nw_sw_blog")
    account.config["locales"] = "en,zh"
    store = Checkpoints(original)
    with pytest.raises(publishers.PreparedPublishError):
        publish(account, store.restart(), session=Session([
            *prefix, Response({"content": {"sha": "en-sha"}}), Response(status_code=429)]))
    assert store.items("nw_sw_blog")["file:en"]["id"] == "en-sha"
    assert store.saved[-1]["provider_receipt"]["status"] != "delivered"
    resumed = Session([*prefix, Response({"content": {"sha": "zh-sha"}})])
    result = publish(account, store.restart(), session=resumed)
    writes = [call for call in resumed.calls if call[0] == "PUT"]
    assert len(writes) == 1 and "/zh/" in writes[0][1]
    assert [item["sha"] for item in result] == ["en-sha", "zh-sha"]


def test_twitter_media_progress_survives_second_item_rejection(tmp_path):
    artifacts = []
    for index in range(2):
        path = tmp_path / f"photo-{index}.jpg"
        path.write_bytes(b"synthetic image")
        artifacts.append({"local_path": str(path), "artifact_kind": "image"})
    account = SimpleNamespace(config={"accessToken": "synthetic", "scope": "tweet.write media.write"})
    store = Checkpoints({"message": "Photos", "artifacts": artifacts})
    with pytest.raises(publishers.PreparedPublishError):
        publishers.publish_twitter_sync(account, store.restart(), session=Session([
            Response({"data": {"id": "media-1"}}), Response({}), Response({}), Response(status_code=429)]))
    assert store.items("twitter")["media:0"]["id"] == "media-1"
    (tmp_path / "photo-0.jpg").unlink()  # completed uploads do not need the old cache
    resumed = Session([Response({"data": {"id": "media-2"}}), Response({}), Response({}),
                       Response({"data": {"id": "tweet-1"}})])
    publishers.publish_twitter_sync(account, store.restart(), session=resumed)
    assert len(resumed.calls) == 4
    assert json.loads(resumed.calls[-1][2]["data"])["media"]["media_ids"] == ["media-1", "media-2"]


def test_facebook_pending_retains_rotated_config_only_in_transient_memory(monkeypatch):
    account = SimpleNamespace(config={"pageId": "page", "accessToken": "old-token"})
    refreshed = {**account.config, "accessToken": "ROTATED_SECRET"}
    monkeypatch.setattr(publishers, "_maybe_refresh_facebook_token", lambda *a, **kw: refreshed)
    store = Checkpoints({"artifacts": [{"public_url": "https://cdn.invalid/video.mp4"}]})
    payload = store.restart()
    with pytest.raises(delivery.DeliveryPending):
        publishers.publish_facebook_sync(account, payload, session=Session([
            Response({"id": "video-1"}), Response({"id": "video-1", "status": {"video_status": "processing"}})]))
    assert payload["_updated_config"]["accessToken"] == "ROTATED_SECRET"
    assert "ROTATED_SECRET" not in json.dumps(store.saved)


def test_facebook_feed_retry_uses_all_saved_photos_without_artifacts():
    account = SimpleNamespace(config={"pageId": "page", "accessToken": "synthetic"})
    store = Checkpoints(photos())
    with pytest.raises(publishers.PreparedPublishError):
        publishers.publish_facebook_sync(account, store.restart(), session=Session([
            Response({"id": "photo-1"}), Response({"id": "photo-2"}), Response(status_code=429)]))
    resumed = Session([Response({"id": "post-1"})])
    publishers.publish_facebook_sync(account, store.restart(artifacts=[]), session=resumed)
    assert len(resumed.calls) == 1
    assert json.loads(resumed.calls[0][2]["data"]["attached_media"]) == [
        {"media_fbid": "photo-1"}, {"media_fbid": "photo-2"}]


def test_bluesky_completed_blob_is_reused_after_second_blob_rejection(tmp_path):
    publish, account, _, auth, posted = case("bluesky")
    artifacts = []
    for index in range(2):
        path = tmp_path / f"blob-{index}.jpg"
        path.write_bytes(b"synthetic image")
        artifacts.append({"local_path": str(path), "artifact_kind": "image"})
    store = Checkpoints({"message": "Synthetic photos", "artifacts": artifacts})
    blob = lambda cid: Response({"blob": {"$type": "blob", "ref": {"$link": cid},
                                         "mimeType": "image/jpeg", "size": 15}})
    with pytest.raises(publishers.PreparedPublishError):
        publish(account, store.restart(), session=Session([*auth, blob("cid-1"), Response(status_code=429)]))
    assert store.items("bluesky")["blob:0"]["id"] == "cid-1"
    assert not store.saved[-1].get("provider_receipt")
    (tmp_path / "blob-0.jpg").unlink()
    resumed = Session([*auth, blob("cid-2"), Response(posted)])
    publish(account, store.restart(), session=resumed)
    assert len(resumed.calls) == 3  # authenticate, upload only second blob, create one post
    images = resumed.calls[-1][2]["json"]["record"]["embed"]["images"]
    assert [item["image"]["ref"]["$link"] for item in images] == ["cid-1", "cid-2"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_explicit_400_does_not_leave_ambiguous_marker(provider):
    publish, account, original, prefix, _ = case(provider)
    store = Checkpoints(original)
    with pytest.raises(publishers.PreparedPublishError) as error:
        publish(account, store.restart(), session=Session([
            *prefix, Response({"error": {"message": "synthetic bad request"}}, status_code=400)]))
    assert error.value.retryable is False
    assert not store.saved[-1]["submission_started"]
    assert not store.items(provider)


@pytest.mark.parametrize("scheduled", [False, True])
def test_youtube_processed_but_wrong_or_scheduled_visibility_is_not_delivery(tmp_path, scheduled):
    account, original = youtube_case(tmp_path)
    if scheduled:
        account.config["publishAt"] = "2099-01-01T00:00:00Z"
    store = Checkpoints(original)
    error = delivery.DeliveryPending if scheduled else publishers.PreparedPublishError
    with pytest.raises(error):
        publishers.publish_youtube_sync(account, store.restart(), session=Session([
            *youtube_init(), Response({"id": "video-1"}), youtube_status(privacy="private")]))
    assert store.saved[-1]["provider_receipt"]["status"] == "processing"


def test_operations_cannot_finish_a_partially_completed_plan():
    payload = {}
    operations = generic_delivery.Operations(payload, "synthetic")
    operations.plan(["first", "second"])
    operations.record("first", "id-1")
    with pytest.raises(delivery.DeliveryUnknown):
        operations.finish()
    assert payload["_delivery"]["provider_receipt"]["status"] == "processing"
