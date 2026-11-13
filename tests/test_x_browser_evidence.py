"""Prospective X evidence only: no credentials, browser launch, or provider writes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from myUtils import delivery, generic_delivery, failure_classification
from myUtils.prepared_publishers import PreparedPublishError
from uploader.twitter_uploader import main as x

CREATE_URL = "https://x.com/i/api/graphql/owned/CreateTweet"
MEDIA_URL = "https://upload.twitter.com/1.1/media/upload.json"


def created(receipt="123"):
    return {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": receipt}}}}}


class Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self.ok = 200 <= status < 300
        self.body = created() if body is None else body
        self.url = CREATE_URL
        self.request = SimpleNamespace(method="POST")

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Route:
    def __init__(self, response=None, *, url=CREATE_URL, transport=None, text="approved", media_ids=(), reply=None):
        variables = {"tweet_text": text, "media": {"media_entities": [{"media_id": i} for i in media_ids]}}
        if reply:
            variables["reply"] = {"in_reply_to_tweet_id": reply}
        self.request = SimpleNamespace(url=url, method="POST", post_data="", post_data_json={"variables": variables})
        self.response = response or Response()
        self.transport = transport
        self.fetches = 0
        self.aborts = 0
        self.fulfilled = 0
        self.fallbacks = 0

    async def fetch(self, **kwargs):
        assert kwargs["max_retries"] == kwargs["max_redirects"] == 0
        assert 0 < kwargs["timeout"] <= x.TWITTER_ATTACHMENT_READY_TIMEOUT_MS
        self.fetches += 1
        if self.transport:
            raise self.transport
        return self.response

    async def abort(self):
        self.aborts += 1

    async def fulfill(self, **kwargs):
        self.fulfilled += 1

    async def fallback(self):
        self.fallbacks += 1


@pytest.fixture
def evidence():
    saved = []
    payload = {"_telegramDeliveryKey": "17", "_save_checkpoint": lambda current: saved.append(copy.deepcopy(current))}
    observer = generic_delivery.make_x_browser_observer(payload, target_id=17, account_key="account:4")
    return payload, observer, saved


def plan(observer, count=1, media_count=0):
    parts = [{"identity": hashlib.sha256(str(i).encode()).hexdigest(), "index": i,
              "text_sha256": hashlib.sha256(b"approved").hexdigest(),
              "media": [{"sha256": "a" * 64, "bytes": 12}] * media_count} for i in range(count)]
    observer({"event": "plan", "parts": parts})
    return parts


def session(observer, parts, index=0, reply=None, action=True):
    value = x._XBrowserSession(observer)
    value.part = parts[index]["identity"]
    value.approved = parts[index]
    value.reply_to = reply
    if action:
        value.action = value.notify("create_action", reply_to=reply)
    return value


def test_preparation_has_no_post_fence_or_budget(evidence):
    payload, observer, _ = evidence
    parts = plan(observer)
    observer({"event": "preparing", "part": parts[0]["identity"]})
    assert not delivery.state(payload).get("submission_started")
    assert delivery.state(payload)["x_browser_operations"]["budgets"] == {}
    assert not delivery.has_unresolved_submission(payload)


@pytest.mark.parametrize("phase", ["navigation", "preview"])
def test_pre_create_timeouts_do_not_mark_submission(evidence, monkeypatch, tmp_path, phase):
    payload, observer, _ = evidence
    media = tmp_path / "part.jpg"
    media.write_bytes(b"fixture")
    segment = x.PreparedTwitterSegment(media, media, 0, 0, 0, 0, False, (media,))
    uploader = x.TwitterThreadVideo.__new__(x.TwitterThreadVideo)
    uploader.title, uploader.tags = "approved", []
    uploader.delivery_observer = observer
    parts = [x.browser_segment_identity(segment, "approved")]
    observer({"event": "plan", "parts": parts})
    uploader._browser_parts = {0: parts[0]}
    uploader._browser_evidence = session(observer, parts, action=False)
    uploader._fill_composer = AsyncMock(side_effect=TimeoutError("preview") if phase == "preview" else None)
    uploader._submit_composer = AsyncMock()
    page = SimpleNamespace(goto=AsyncMock(side_effect=TimeoutError("navigation") if phase == "navigation" else None))
    monkeypatch.setattr(x, "wait_for_ready_post_button", AsyncMock())
    with pytest.raises(TimeoutError):
        asyncio.run(uploader._publish_segment(page, segment, None, total_segments=1))
    assert not delivery.state(payload).get("submission_started")
    assert not any(":create" in key for key in delivery.state(payload)["x_browser_operations"]["budgets"])
    uploader._submit_composer.assert_not_awaited()


@pytest.mark.parametrize("status", [401, 402, 429])
def test_real_safe_http_rejection_is_generation_bound(evidence, status):
    payload, observer, saved = evidence
    value = session(observer, plan(observer))
    route = Route(Response(status, {"errors": [{"message": "access_token=never-retain", "code": 32}]}))
    asyncio.run(value.route(route))
    with pytest.raises(PreparedPublishError) as raised:
        value.check()
    details = raised.value.details
    assert details["httpStatus"] == status and details["definitiveNonDelivery"] is True
    assert details["operationGeneration"] == delivery.state(payload)["operation_generation"]
    assert not delivery.has_unresolved_submission(payload)
    assert not delivery.receipt_for(payload, "twitter")
    assert route.fetches == route.fulfilled == 1
    assert "never-retain" not in repr(saved)
    delivery.starting(payload, operation="newer-create")
    assert delivery.has_unresolved_submission(payload)
    assert generic_delivery.x_browser_has_effects(delivery.state(payload))


@pytest.mark.parametrize("status,body", [(400, {}), (403, {}), (408, {}), (500, {}),
    (200, {}), (200, {"data": {"viewer": {"rest_id": "999"}}}),
    (200, ValueError("missing body")), (200, {"errors": [{"code": 1}], **created()})])
def test_missing_or_ambiguous_receipt_never_becomes_delivery(evidence, status, body):
    payload, observer, _ = evidence
    value = session(observer, plan(observer))
    route = Route(Response(status, body))
    asyncio.run(value.route(route))
    with pytest.raises(delivery.DeliveryUnknown):
        value.check()
    assert route.fetches == 1
    assert delivery.has_unresolved_submission(payload)
    assert not delivery.receipt_for(payload, "twitter")


def test_lost_transport_retains_unknown_and_never_retries(evidence):
    payload, observer, _ = evidence
    value = session(observer, plan(observer))
    route = Route(transport=TimeoutError("lost"))
    asyncio.run(value.route(route))
    with pytest.raises(delivery.DeliveryUnknown):
        value.check()
    assert route.fetches == 1
    assert delivery.state(payload)["x_browser_operations"]["items"][value.action]["status"] == "unknown"
    with pytest.raises(delivery.DeliveryUnknown):
        generic_delivery.make_x_browser_observer(payload, target_id=17, account_key="account:4")


def test_request_lease_loss_aborts_before_upstream_write(evidence):
    payload, observer, _ = evidence
    value = session(observer, plan(observer))
    payload["_save_checkpoint"] = lambda current: (_ for _ in ()).throw(delivery.LeaseLost("stale"))
    route = Route()
    asyncio.run(value.route(route))
    with pytest.raises(delivery.LeaseLost):
        value.check()
    assert route.fetches == 0 and route.aborts == 1


def test_callback_lease_loss_prevents_button_action(evidence, tmp_path, monkeypatch):
    payload, observer, _ = evidence
    media = tmp_path / "one.jpg"
    media.write_bytes(b"fixture")
    segment = x.PreparedTwitterSegment(media, media, 0, 0, 0, 0, False, (media,))
    uploader = x.TwitterThreadVideo.__new__(x.TwitterThreadVideo)
    uploader.title, uploader.tags, uploader.delivery_observer = "approved", [], observer
    parts = [x.browser_segment_identity(segment, "approved")]
    observer({"event": "plan", "parts": parts})
    uploader._browser_parts = {0: parts[0]}
    uploader._browser_evidence = session(observer, parts, action=False)
    uploader._fill_composer = AsyncMock()
    button = SimpleNamespace(evaluate=AsyncMock(return_value=True), click=AsyncMock(), dispatch_event=AsyncMock())
    page = SimpleNamespace(goto=AsyncMock())
    class Expect:
        async def __aenter__(self):
            return SimpleNamespace()
        async def __aexit__(self, *args):
            return False
    page.expect_response = lambda *args, **kwargs: Expect()
    monkeypatch.setattr(x, "wait_for_ready_post_button", AsyncMock(return_value=button))
    def save(current):
        if current.get("submission_started"):
            raise delivery.LeaseLost("lost before click")
    payload["_save_checkpoint"] = save
    with pytest.raises(delivery.LeaseLost):
        asyncio.run(uploader._publish_segment(page, segment, None, total_segments=1))
    button.click.assert_not_awaited()
    button.dispatch_event.assert_not_awaited()


def test_partial_thread_receipt_blocks_rejection_handoff_and_replay(evidence):
    payload, observer, _ = evidence
    parts = plan(observer, 2)
    first = session(observer, parts)
    asyncio.run(first.route(Route()))
    first.check()
    assert delivery.receipt_for(payload, "twitter")["status"] == "processing"
    with pytest.raises(delivery.DeliveryUnknown):
        observer({"event": "complete"})
    second = session(observer, parts, 1, reply="123")
    asyncio.run(second.route(Route(Response(402, {}), reply="123")))
    with pytest.raises(delivery.DeliveryUnknown) as raised:
        second.check()
    assert raised.value.details["definitiveNonDelivery"] is True
    assert raised.value.details["retrySafe"] is False
    assert not failure_classification.backup_eligible(raised.value, checkpoint=delivery.state(payload))
    assert delivery.receipt_for(payload, "twitter")["id"] == "123"
    assert not delivery.state(payload).get("x_browser_non_delivery")
    assert delivery.has_unresolved_submission(payload)


def test_only_all_ordered_post_receipts_finalize_delivery(evidence):
    payload, observer, saved = evidence
    parts = plan(observer, 2)
    first = session(observer, parts)
    asyncio.run(first.route(Route()))
    second = session(observer, parts, 1, reply="123")
    asyncio.run(second.route(Route(Response(body=created("124")), reply="123")))
    observer({"event": "complete"})
    items = delivery.state(payload)["x_browser_operations"]["items"]
    assert [item["id"] for item in items.values()] == ["123", "124"]
    assert all(item["target_id"] == 17 and item["account_key"] == "account:4" and item["operation_generation"] for item in items.values())
    assert delivery.receipt_for(payload, "twitter")["status"] == "delivered"
    assert not any(s.get("provider_receipt", {}).get("status") == "delivered" for s in saved[:-1])


def test_media_upload_id_retained_without_post_fence_and_safe_rejection_resolves_only_known_media(evidence):
    payload, observer, _ = evidence
    parts = plan(observer, media_count=1)
    value = session(observer, parts, action=False)
    route = Route(Response(body={"media_id_string": "456"}), url=MEDIA_URL)
    asyncio.run(value.route(route))
    value.check()
    assert not delivery.state(payload).get("submission_started")
    items = delivery.state(payload)["x_browser_operations"]["items"]
    assert next(iter(items.values()))["effect"] == "media_only"
    assert next(iter(items.values()))["id"] == "456"
    value.action = value.notify("create_action", reply_to=None)
    asyncio.run(value.route(Route(Response(401, {}), media_ids=["456"])))
    with pytest.raises(PreparedPublishError):
        value.check()
    assert not delivery.has_unresolved_submission(payload)
    assert len(delivery.state(payload)["x_browser_operations"]["items"]) == 2


def test_unknown_media_does_not_forge_non_delivery(evidence):
    payload, observer, _ = evidence
    value = session(observer, plan(observer), action=False)
    asyncio.run(value.route(Route(Response(body={}), url=MEDIA_URL)))
    with pytest.raises(delivery.DeliveryUnknown) as raised:
        value.check()
    assert not delivery.state(payload).get("submission_started")
    assert delivery.has_unresolved_submission(payload)
    assert not delivery.state(payload).get("definitive_non_delivery")
    assert raised.value.details["effect"] == "media_only"
    assert not failure_classification.backup_eligible(raised.value, checkpoint=delivery.state(payload))


def test_premature_duplicate_or_unrecognized_mutation_is_never_sent(evidence):
    _, observer, _ = evidence
    value = session(observer, plan(observer), action=False)
    early = Route()
    asyncio.run(value.route(early))
    assert early.fetches == 0 and early.aborts == 1
    value = session(observer, observer.journal["plan"])
    first, duplicate = Route(), Route()
    asyncio.run(value.route(first))
    asyncio.run(value.route(duplicate))
    assert first.fetches == 1 and duplicate.fetches == 0
    alternate = Route(url="https://x.com/i/api/graphql/id/CreateScheduledTweet")
    asyncio.run(value.route(alternate))
    assert alternate.fetches == 0 and alternate.aborts == 1


def test_changed_request_identity_is_blocked(evidence):
    _, observer, _ = evidence
    value = session(observer, plan(observer))
    route = Route(text="unapproved")
    asyncio.run(value.route(route))
    assert route.fetches == 0 and route.aborts == 1
    with pytest.raises(delivery.DeliveryUnknown):
        value.check()


def test_finite_create_budget_is_retained_across_safe_rejected_attempts(evidence):
    payload, observer, _ = evidence
    parts = plan(observer)
    for _ in range(3):
        value = session(observer, parts)
        asyncio.run(value.route(Route(Response(429, {}))))
        with pytest.raises(PreparedPublishError):
            value.check()
        observer = generic_delivery.make_x_browser_observer(payload, target_id=17, account_key="account:4")
    with pytest.raises(delivery.RouteExhausted):
        session(observer, parts)
    assert len(delivery.state(payload)["x_browser_operations"]["items"]) == 3


def test_finite_media_budget_does_not_reset(evidence):
    _, observer, _ = evidence
    observer.max_media_requests = 1
    value = session(observer, plan(observer), action=False)
    asyncio.run(value.route(Route(Response(body={"media_id_string": "456"}), url=MEDIA_URL)))
    extra = Route(Response(body={"media_id_string": "457"}), url=MEDIA_URL)
    asyncio.run(value.route(extra))
    assert extra.fetches == 0
    with pytest.raises(delivery.RouteExhausted):
        value.check()


def test_legacy_conservative_outer_guard_is_not_reinterpreted(evidence):
    payload, _, _ = evidence
    assert inspect.signature(x.TwitterThreadVideo).parameters["delivery_observer"].default is None
    delivery.starting(payload, operation="browser-publish:twitter")
    before = copy.deepcopy(delivery.state(payload))
    with pytest.raises(delivery.DeliveryUnknown):
        generic_delivery.make_x_browser_observer(payload, target_id=17, account_key="account:4")
    assert delivery.state(payload) == before


def test_instrumented_path_without_installed_interceptor_fails_closed():
    uploader = x.TwitterThreadVideo.__new__(x.TwitterThreadVideo)
    uploader.delivery_observer = lambda event: None
    with pytest.raises(delivery.DeliveryUnknown, match="without fail-closed"):
        asyncio.run(uploader._publish_segment(None, None, None, total_segments=1))


@pytest.mark.parametrize("status", [200, 401])
def test_mocked_browser_installs_route_before_page_and_observes_each_action(evidence, tmp_path, monkeypatch, status):
    payload, observer, _ = evidence
    media = tmp_path / "part.jpg"
    media.write_bytes(b"isolated fixture")
    segments = [x.PreparedTwitterSegment(media, media, 0, i, i, 1, False, (media,), i) for i in range(2)]
    uploader = x.TwitterThreadVideo.__new__(x.TwitterThreadVideo)
    uploader.title, uploader.tags = "approved", []
    uploader.delivery_observer, uploader._browser_evidence = observer, None
    uploader.account_file, uploader.headless = "mocked-state-not-read", True
    writes = []
    page = SimpleNamespace(goto=AsyncMock())
    context = SimpleNamespace(storage_state=AsyncMock(), close=AsyncMock())

    async def install(pattern, handler):
        context.handler = handler
    context.route = install

    async def new_page():
        assert callable(context.handler)  # No navigation/UI can escape the route.
        return page
    context.new_page = new_page

    class Expect:
        async def __aenter__(self):
            self.value = asyncio.get_running_loop().create_future()
            page.future = self.value
            return self
        async def __aexit__(self, *args):
            return False

    def expect(predicate, **kwargs):
        assert predicate(Response(401, {}))  # Non-2xx must not become a timeout.
        return Expect()
    page.expect_response = expect

    async def fill(*args, segment, text):
        page.text = text
        page.media_id = str(456 + segment.post_index)
        route = Route(Response(body={"media_id_string": page.media_id}), url=MEDIA_URL)
        await context.handler(route)
        assert not delivery.state(payload).get("submission_started") if segment.post_index == 0 else True
    uploader._fill_composer = fill
    uploader._open_reply_composer = AsyncMock()

    class Button:
        async def evaluate(self, _script):
            return True
        async def click(self, **kwargs):
            assert delivery.state(payload)["submission_started"] is True
            response = Response(status, created(str(123 + len(writes))) if status == 200 else {})
            route = Route(response, text=page.text, media_ids=[page.media_id], reply="123" if writes else None)
            writes.append(route)
            await context.handler(route)
            page.future.set_result(response)
        async def dispatch_event(self, _event):
            pytest.fail("unexpected second action")
    monkeypatch.setattr(x, "wait_for_ready_post_button", AsyncMock(return_value=Button()))
    monkeypatch.setattr(x, "set_init_script", AsyncMock(return_value=context))
    monkeypatch.setattr(x, "_block_heavy_media", AsyncMock(return_value=context))
    monkeypatch.setattr(x, "get_browser_options", lambda: {})

    async def new_context(**kwargs):
        assert kwargs["service_workers"] == "block"
        return context
    browser = SimpleNamespace(new_context=new_context, close=AsyncMock())
    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        async def __aexit__(self, *args):
            return False
    monkeypatch.setattr(x, "async_playwright", Playwright)
    if status == 200:
        assert asyncio.run(uploader._publish_prepared_segments(segments)) == ["123", "124"]
        assert delivery.receipt_for(payload, "twitter")["status"] == "delivered"
    else:
        with pytest.raises(PreparedPublishError):
            asyncio.run(uploader._publish_prepared_segments(segments))
        assert len(writes) == 1 and not delivery.has_unresolved_submission(payload)
    assert all(route.fetches == 1 for route in writes)


def test_stale_operation_cannot_apply_receipt_or_rejection(evidence):
    payload, observer, _ = evidence
    value = session(observer, plan(observer))
    delivery.starting(payload, operation="newer-provider-write")
    original = copy.deepcopy(delivery.state(payload))
    for event in ("receipt", "rejection"):
        with pytest.raises(delivery.DeliveryUnknown, match="stale operation"):
            value.notify(event, name=value.action, id="123", httpStatus=401 if event == "rejection" else 200)
    assert delivery.state(payload) == original


@pytest.mark.parametrize("mutation", ["target", "account", "media", "new_unknown"])
def test_exact_proof_does_not_resolve_changed_retained_effects(evidence, mutation):
    payload, observer, _ = evidence
    value = session(observer, plan(observer))
    asyncio.run(value.route(Route(Response(402, {}))))
    current = delivery.state(payload)
    assert not delivery.has_unresolved_submission(payload)
    journal = current["x_browser_operations"]
    if mutation == "target":
        journal["target_id"] = 18
    elif mutation == "account":
        journal["account_key"] = "account:5"
    elif mutation == "media":
        journal["items"]["media:0:1"] = {"effect": "media_only", "id": "456", "status": "complete"}
    else:
        journal["items"]["create:0:2"] = {"effect": "publication", "status": "unknown"}
    assert delivery.has_unresolved_submission(payload)


def test_media_append_ack_uses_retained_provider_id_not_a_tweet_receipt(evidence):
    payload, observer, _ = evidence
    value = session(observer, plan(observer), action=False)
    asyncio.run(value.route(Route(Response(body={"media_id_string": "456"}), url=MEDIA_URL)))
    append = Route(Response(204, {}), url=MEDIA_URL + "?command=APPEND&media_id=456")
    asyncio.run(value.route(append))
    value.check()
    items = list(delivery.state(payload)["x_browser_operations"]["items"].values())
    assert len(items) == 2 and items[1]["id"] == "456"
    assert items[1]["evidence_source"] == "retained_media_ack"
    assert not delivery.receipt_for(payload, "twitter")
    assert not delivery.state(payload).get("submission_started")


def test_late_exact_receipt_is_retained_after_action_timeout(evidence):
    payload, observer, _ = evidence
    value = session(observer, plan(observer))
    with pytest.raises(delivery.DeliveryUnknown):
        value.notify("unknown", name=value.action)
    value.notify("receipt", name=value.action, id="123", httpStatus=200)
    assert delivery.receipt_for(payload, "twitter")["id"] == "123"
    assert delivery.receipt_for(payload, "twitter")["status"] == "processing"


def test_media_http_rejection_is_not_a_post_rejection_proof(evidence):
    payload, observer, _ = evidence
    value = session(observer, plan(observer), action=False)
    asyncio.run(value.route(Route(Response(402, {}), url=MEDIA_URL)))
    with pytest.raises(delivery.DeliveryUnknown) as raised:
        value.check()
    assert raised.value.details["httpStatus"] == 402
    assert raised.value.details["retrySafe"] is False
    assert not delivery.state(payload).get("submission_started")
    assert not delivery.state(payload).get("definitive_non_delivery")
    assert not failure_classification.backup_eligible(raised.value, checkpoint=delivery.state(payload))


def test_multipart_append_retains_only_selected_ack_identity(evidence):
    payload, observer, saved = evidence
    value = session(observer, plan(observer), action=False)
    asyncio.run(value.route(Route(Response(body={"media_id_string": "456"}), url=MEDIA_URL)))
    append = Route(Response(204, {}), url=MEDIA_URL)
    append.request.post_data_buffer = (b'--boundary\r\nContent-Disposition: form-data; name="command"\r\n\r\nAPPEND\r\n'
        b'--boundary\r\nContent-Disposition: form-data; name="media_id"\r\n\r\n456\r\n'
        b'--boundary\r\nContent-Disposition: form-data; name="media_data"\r\n\r\nsecret-binary\r\n--boundary--')
    asyncio.run(value.route(append))
    value.check()
    assert "secret-binary" not in repr(saved)
    assert len(delivery.state(payload)["x_browser_operations"]["items"]) == 2


def test_read_only_button_probe_occurs_before_exact_action_fence():
    events = []
    class Button:
        async def evaluate(self, _script):
            events.append("probe")
            return True
        async def click(self, **kwargs):
            events.append("click")
    asyncio.run(x.click_post_button(None, Button(), before_action=lambda: events.append("fence")))
    assert events == ["probe", "fence", "click"]
