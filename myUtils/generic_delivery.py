"""Small, credential-free operation journal for non-idempotent API publishers.

Only explicitly selected IDs/status/progress belong here, never provider response
bodies or upload URLs. The worker supplies delivery.checkpoint's fenced writer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from uuid import uuid4

from myUtils import delivery


class Operations:
    def __init__(self, payload: dict, provider: str, *, allow_receipt: bool = False):
        self.payload = payload
        self.provider = provider
        self.progress = copy.deepcopy(
            (delivery.state(payload).get("provider_operations") or {}).get(provider) or {}
        )
        self.items = self.progress.setdefault("items", {})
        if any(item.get("status") == "started" for item in self.items.values()):
            raise delivery.DeliveryUnknown(
                f"{provider} create response was lost; reconcile before sending again"
            )
        if (delivery.state(payload).get("submission_started") and not self.items
                and not (allow_receipt and delivery.receipt_for(payload, provider))):
            raise delivery.DeliveryUnknown(f"{provider} has an unreconciled prior submission")

    def _save(self, **changes):
        providers = dict(delivery.state(self.payload).get("provider_operations") or {})
        providers[self.provider] = self.progress
        return delivery.checkpoint(self.payload, provider_operations=providers, **changes)

    def plan(self, names: list[str]) -> None:
        previous = self.progress.get("plan")
        if previous is not None and previous != names:
            raise delivery.DeliveryUnknown(f"{self.provider} operation plan changed after checkpoint")
        if previous is None:
            self.progress["plan"] = names
            self._save()

    def get(self, name: str) -> dict | None:
        return self.items.get(name)

    def begin(self, name: str) -> None:
        if any(item.get("status") == "started" for item in self.items.values()):
            raise delivery.DeliveryUnknown(f"{self.provider} has an unresolved create request")
        if name in self.items:
            raise delivery.DeliveryUnknown(f"{self.provider} operation already has a receipt; do not recreate")
        now = delivery.utc_now()
        generation = uuid4().hex
        self.progress.pop("last_rejection", None)
        self.items[name] = {"status": "started", "submitted_at": now}
        self._save(submission_started=True, operation=f"{self.provider}:{name}",
                   operation_started_at=now, operation_generation=generation)

    def reject(self, name: str, *, status: int | None = None) -> None:
        """A definite rejection clears only this request, not earlier effects."""
        if (self.items.get(name) or {}).get("status") == "started":
            del self.items[name]
        if status in {401, 402, 429}:
            self.progress["last_rejection"] = {
                "name": name, "httpStatus": status, "no_publication": True,
                "operation_generation": delivery.state(self.payload).get("operation_generation"),
            }
        else:
            self.progress.pop("last_rejection", None)
        self._save(submission_started=bool(self.items) or bool(delivery.receipt_for(self.payload, self.provider)),
                   operation="")

    def request(self, name: str, send, *, allow_statuses=(), continuing: bool = False):
        """Fence immediately before sending; ambiguous writes are never retried."""
        from myUtils import prepared_publishers as publishers

        # Resumable YouTube initialization and transfer are one operation; the
        # session URL is deliberately never checkpointed. Fence both requests.
        if continuing:
            if (self.get(name) or {}).get("status") != "started":
                raise delivery.DeliveryUnknown(f"{self.provider} has no active upload to continue")
            self._save(submission_started=True)
        else:
            self.begin(name)
        try:
            response = send()
        except (delivery.LeaseLost, delivery.DeliveryPending, delivery.DeliveryUnknown):
            raise
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if not (isinstance(status, int) and 400 <= status < 500 and status != 408):
                raise delivery.DeliveryUnknown(
                    f"{self.provider} {name} response was lost; acceptance is unknown"
                ) from None
        status = getattr(response, "status_code", 0)
        if isinstance(status, int) and 400 <= status < 500 and status != 408:
            self.reject(name, status=status)
            if status in allow_statuses:
                return response
            try:
                publishers._raise_for_status(response)
            except publishers.PreparedPublishError as exc:
                exc.details["httpStatus"] = status
                if status in {401, 402, 429}:
                    exc.details["definitiveNonDelivery"] = True
                    exc.details["operationGeneration"] = self.progress["last_rejection"]["operation_generation"]
                retry_after = (getattr(response, "headers", None) or {}).get("Retry-After")
                if retry_after:
                    exc.details["retryAfter"] = str(retry_after)
                raise
            raise publishers.PreparedPublishError(
                f"HTTP {status}: {self.provider} rejected {name}", retryable=status == 429,
                details={"httpStatus": status, "definitiveNonDelivery": status in {401, 402, 429},
                         "operationGeneration": delivery.state(self.payload).get("operation_generation")})
        if not isinstance(status, int) or not 200 <= status < 300:
            raise delivery.DeliveryUnknown(f"{self.provider} {name} acceptance is unknown (HTTP {status})")
        try:
            publishers._raise_for_status(response)
        except (delivery.LeaseLost, delivery.DeliveryPending, delivery.DeliveryUnknown):
            raise
        except Exception:
            raise delivery.DeliveryUnknown(f"{self.provider} {name} returned an ambiguous response") from None
        return response

    def record(self, name: str, receipt_id, *, status: str = "complete",
               provider_receipt: bool = True, **metadata) -> dict:
        if not isinstance(receipt_id, (str, int)) or not str(receipt_id).strip():
            raise delivery.DeliveryUnknown(f"{self.provider} {name} returned no receipt ID")
        previous = self.items.get(name) or {}
        item = {"id": str(receipt_id), "status": status,
                "submitted_at": previous.get("submitted_at") or delivery.utc_now(), **metadata,
                "effect": "publication" if provider_receipt else "media_only"}
        self.items[name] = item
        changes = {"submission_started": True, "operation": ""}
        if provider_receipt:
            # Even a completed sub-operation is not a completed target.
            changes["provider_receipt"] = {
                **item, "provider": self.provider, "operation": name, "status": "processing"
            }
        self._save(**changes)
        return item

    def finish(self, *, receipt_id: str = "") -> None:
        plan = self.progress.get("plan") or []
        if not plan or any((self.get(name) or {}).get("status") != "complete" for name in plan):
            raise delivery.DeliveryUnknown(f"{self.provider} has incomplete operations")
        delivery.confirmed(self.payload, provider=self.provider,
                           receipt_id=receipt_id or self.items[plan[-1]]["id"])


def _media_only_evidence(current: dict) -> dict | None:
    """Only explicit new X journals can prove that no tweet was created."""
    if (not isinstance(current, dict) or current.get("checkpoint_invalid")
            or current.get("provider_receipt") or current.get("native_provider_receipt")
            or current.get("completed_operations") or current.get("telegramCompletedByDelivery")):
        return None
    journals = current.get("provider_operations")
    if not isinstance(journals, dict) or set(journals) != {"twitter"}:
        return None
    journal = journals["twitter"]
    if not isinstance(journal, dict):
        return None
    plan, items, rejection = journal.get("plan"), journal.get("items"), journal.get("last_rejection")
    if (not isinstance(plan, list) or not 1 <= len(plan) <= 5 or plan[-1] != "tweet"
            or any(not isinstance(name, str) or not re.fullmatch(r"media:[0-3]", name) for name in plan[:-1])
            or len(set(plan)) != len(plan) or not isinstance(items, dict)
            or not isinstance(rejection, dict) or rejection.get("no_publication") is not True
            or type(rejection.get("httpStatus")) is not int or rejection["httpStatus"] not in {401, 402, 429}):
        return None
    # The retained native rejection owns this fence. Backup requests may safely
    # change the shared fence without changing any native journaled effect.
    generation = rejection.get("operation_generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        return None
    media_names = set(plan[:-1])
    def media_operation(name):
        return (isinstance(name, str) and re.fullmatch(r"media:[0-3](?::initialize|:segment:\d+|:finalize)?", name)
                and ":".join(name.split(":")[:2]) in media_names)
    if rejection.get("name") != "tweet" and not media_operation(rejection.get("name")):
        return None
    for name, item in items.items():
        if (not media_operation(name) or not isinstance(item, dict)
                or item.get("status") != "complete" or item.get("effect") != "media_only"
                or isinstance(item.get("id"), bool) or not isinstance(item.get("id"), (str, int))
                or not str(item["id"]).strip()):
            return None
    try:
        digest = hashlib.sha256(json.dumps(journal, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return None
    return {"provider": "twitter", "httpStatus": rejection["httpStatus"],
            "operation_generation": generation, "journal_sha256": digest,
            "operation_receipts": {"twitter": {name: {"id": item["id"], "no_publication": True,
                                                       "definitive_rejection": True} for name, item in items.items()}}}


def media_only_reconciled(current: dict, *, target_id: int | None = None) -> bool:
    """A saved authorization must still match every retained native effect."""
    evidence = _media_only_evidence(current)
    proof = current.get("media_only_rejection") if isinstance(current, dict) else None
    authorization = current.get("backup_authorization") if isinstance(current, dict) else None
    if (evidence is None or not isinstance(proof, dict) or not isinstance(authorization, dict)
            or type(proof.get("target_id")) is not int or proof["target_id"] <= 0
            or target_id is not None and proof["target_id"] != target_id):
        return False
    return (all(proof.get(key) == evidence[key] for key in
                ("provider", "httpStatus", "operation_generation", "journal_sha256"))
            and authorization.get("target_id") == proof["target_id"]
            and authorization.get("provider") == "twitter"
            and authorization.get("no_publication") is True
            and authorization.get("definitive_rejection") is True
            and authorization.get("operation_receipts") == evidence["operation_receipts"])


def authorize_media_only_handoff(payload: dict, exc: Exception, *, target_id: int) -> bool:
    """Persist an exact-target proof only for a matching real HTTP rejection."""
    current = delivery.state(payload)
    evidence = _media_only_evidence(current)
    details = getattr(exc, "details", None)
    if (evidence is None or type(target_id) is not int or target_id <= 0
            or str(payload.get("_telegramDeliveryKey")) != str(target_id)
            or not isinstance(details, dict) or details.get("definitiveNonDelivery") is not True
            or details.get("httpStatus") != evidence["httpStatus"]
            or details.get("operationGeneration") != evidence["operation_generation"]
            or type(current.get("submission_started")) is not bool or current.get("operation") != ""
            or current.get("operation_generation") != evidence["operation_generation"]):
        return False
    proof = {key: evidence[key] for key in
             ("provider", "httpStatus", "operation_generation", "journal_sha256")}
    proof.update(target_id=target_id, confirmed_at=delivery.utc_now())
    authorization = {"target_id": target_id, "provider": "twitter", "no_publication": True,
                     "definitive_rejection": True, "operation_receipts": evidence["operation_receipts"]}
    delivery.checkpoint(payload, media_only_rejection=proof, backup_authorization=authorization)
    return True


def _poll_json(http, url, payload, *, provider: str, **kwargs) -> dict:
    """Polling errors retain the original receipt and its bounded deadline."""
    from myUtils import prepared_publishers as publishers

    try:
        response = http.get(url, timeout=30, **kwargs)
    except delivery.LeaseLost:
        raise
    except Exception:
        delivery.pending_or_unknown(payload, f"{provider} receipt could not be polled")
    if response.status_code >= 500 or response.status_code in {408, 429}:
        delivery.pending_or_unknown(payload, f"{provider} status endpoint is unavailable",
                                    retry_after=(getattr(response, "headers", None) or {}).get("Retry-After"))
    publishers._raise_for_status(response)
    result = publishers._response_payload(response)
    return result if isinstance(result, dict) else {}


def poll_facebook_video(http, payload, access_token: str, video_id: str) -> None:
    from myUtils import prepared_publishers as publishers

    body = _poll_json(http, f"{publishers.FACEBOOK_GRAPH_ROOT}/{video_id}", payload,
                      provider="facebook", params={"fields": "id,status,published", "access_token": access_token})
    status = body.get("status") or {}
    status = status if isinstance(status, dict) else {}
    phase = str(status.get("video_status") or "").lower()
    publishing = str((status.get("publishing_phase") or {}).get("status") or "").lower()
    if phase in {"error", "failed"} or publishing in {"error", "failed"}:
        raise publishers.PreparedPublishError("Facebook video processing/publishing failed", retryable=False)
    if (str(body.get("id") or "") == video_id and phase == "ready"
            and body.get("published") is not False and publishing in {"", "complete"}):
        return
    delivery.pending_or_unknown(payload, "Facebook video is not confirmed ready/published")


def poll_youtube_video(http, payload, access_token: str, video_id: str, *, privacy: str, scheduled: bool) -> None:
    from myUtils import prepared_publishers as publishers

    body = _poll_json(http, "https://www.googleapis.com/youtube/v3/videos", payload,
                      provider="youtube", headers={"Authorization": f"Bearer {access_token}"},
                      params={"id": video_id, "part": "status,processingDetails"})
    items = body.get("items") or []
    video = next((item for item in items if isinstance(item, dict) and str(item.get("id")) == video_id), {})
    status = video.get("status") or {}
    processing = str((video.get("processingDetails") or {}).get("processingStatus") or "").lower()
    uploaded = str(status.get("uploadStatus") or "").lower()
    if uploaded in {"failed", "rejected", "deleted"} or processing in {"failed", "terminated"}:
        raise publishers.PreparedPublishError("YouTube video processing failed or was rejected", retryable=False)
    if uploaded != "processed" and processing != "succeeded":
        delivery.pending_or_unknown(payload, "YouTube video is still processing")
    actual = str(status.get("privacyStatus") or "").lower()
    if scheduled and (actual == "private" or status.get("publishAt")):
        delivery.pending_or_unknown(payload, "YouTube video is still scheduled, not published")
    requested = "public" if scheduled else privacy.lower()
    if not actual:
        delivery.pending_or_unknown(payload, "YouTube visibility is not confirmed")
    if actual != requested:
        raise publishers.PreparedPublishError(
            f"YouTube uploaded {video_id} as '{actual}' but '{requested}' was requested "
            "(check API project visibility restrictions)", retryable=False)


def _x_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def x_browser_has_effects(current: dict) -> bool:
    """Fail closed on new browser effects, independently of legacy API journals.

    Preparation alone is not a publishing fence. Only a generation-bound real
    rejection can resolve retained, fully identified media-only effects.
    """
    journal = current.get("x_browser_operations")
    if journal is None:
        return False
    if (not isinstance(journal, dict) or type(journal.get("version")) is not int or journal["version"] != 1
            or type(journal.get("target_id")) is not int or journal["target_id"] <= 0
            or not isinstance(journal.get("account_key"), str) or not journal["account_key"]
            or not isinstance(journal.get("plan"), list) or not journal["plan"]
            or not isinstance(journal.get("budgets"), dict)):
        return True
    items = journal.get("items")
    if not isinstance(items, dict):
        return True
    if not items:
        return False
    proof = current.get("x_browser_non_delivery")
    try:
        return not (isinstance(proof, dict)
            and proof.get("journal_sha256") == _x_digest(journal)
            and proof.get("target_id") == journal.get("target_id")
            and proof.get("account_key") == journal.get("account_key")
            and proof.get("operation_generation") == current.get("operation_generation")
            and proof.get("operation") == current.get("operation")
            and proof.get("httpStatus") in {401, 402, 429}
            and isinstance(proof.get("operation_generation"), str)
            and re.fullmatch(r"[0-9a-f]{32}", proof["operation_generation"])
            and isinstance(proof.get("operation"), str) and proof["operation"].startswith("browser-publish:twitter:")
            and all(isinstance(item, dict) and (
                item.get("effect") == "media_only" and item.get("status") == "complete" and item.get("id")
                or item.get("effect") == "publication" and item.get("status") == "rejected"
                   and item.get("httpStatus") in {401, 402, 429}) for item in items.values()))
    except (TypeError, ValueError):
        return True


class XBrowserObserver:
    """Synchronous event callback; every save uses the worker's lease/CAS writer.

    Kept separate from legacy ``provider_operations.twitter``: neither previews
    nor this prospective format can authorize historical media-only proofs.
    """
    def __init__(self, payload: dict, *, target_id: int, account_key: str,
                 max_create_attempts: int = 3, max_media_requests: int = 256):
        if (type(target_id) is not int or target_id <= 0 or not isinstance(account_key, str) or not account_key
                or str(payload.get("_telegramDeliveryKey")) != str(target_id)
                or not callable(payload.get("_save_checkpoint"))):
            raise ValueError("X browser evidence requires an exact target/account and fenced checkpoint writer")
        if any(type(n) is not int or n < 1 for n in (max_create_attempts, max_media_requests)):
            raise ValueError("X browser budgets must be positive finite integers")
        if delivery.has_unresolved_submission(payload):
            raise delivery.DeliveryUnknown("Retained delivery evidence forbids a new X browser attempt")
        self.payload = payload
        self.journal = copy.deepcopy(delivery.state(payload).get("x_browser_operations") or {
            "version": 1, "target_id": target_id, "account_key": account_key,
            "items": {}, "budgets": {}, "plan": None})
        if self.journal.get("target_id") != target_id or self.journal.get("account_key") != account_key:
            raise delivery.DeliveryUnknown("X browser evidence belongs to another target/account")
        self.max_create_attempts = max_create_attempts
        self.max_media_requests = max_media_requests
        self.active_create = None

    def _save(self, **changes):
        if str(self.payload.get("_telegramDeliveryKey")) != str(self.journal["target_id"]):
            raise delivery.LeaseLost("X browser callback target binding changed")
        return delivery.checkpoint(self.payload, x_browser_operations=self.journal, **changes)

    def _budget(self, part: str, kind: str, maximum: int):
        key = f"{part}:{kind}"
        previous = self.journal["budgets"].get(key, {"attempts": 0, "max_attempts": maximum})
        count, limit = previous.get("attempts"), previous.get("max_attempts")
        if type(count) is not int or count < 0 or type(limit) is not int or limit < 1:
            raise delivery.DeliveryUnknown("Invalid X browser request budget")
        limit = min(limit, maximum)
        if count >= limit:
            error = delivery.RouteExhausted("native")
            if delivery.has_unresolved_submission(self.payload):
                error.details["retrySafe"] = False
            raise error
        self.journal["budgets"][key] = {"attempts": count + 1, "max_attempts": limit}
        return count + 1

    def __call__(self, event: dict):
        kind = event["event"]
        if kind == "plan":
            plan = event["parts"]
            if (not isinstance(plan, list) or not plan
                    or any(not isinstance(part, dict) or part.get("index") != i
                           or not isinstance(part.get("identity"), str)
                           or not re.fullmatch(r"[0-9a-f]{64}", part["identity"])
                           for i, part in enumerate(plan))
                    or len({part["identity"] for part in plan}) != len(plan)):
                raise delivery.DeliveryUnknown("Invalid ordered X browser plan")
            if self.journal["plan"] is not None and self.journal["plan"] != plan:
                raise delivery.DeliveryUnknown("X browser ordered media/text identity changed")
            self.journal["plan"] = copy.deepcopy(plan)
            self._save(stage="browser_preparing")
            return
        if kind == "complete":
            plan = self.journal["plan"] or []
            receipts = [item for item in self.journal["items"].values()
                        if item.get("effect") == "publication" and item.get("status") == "complete"]
            if (not plan or len(receipts) != len(plan)
                    or {item["part"] for item in receipts} != {part["identity"] for part in plan}
                    or any(item.get("status") in {"started", "unknown"} for item in self.journal["items"].values())):
                raise delivery.DeliveryUnknown("X browser thread lacks all exact-part receipts")
            receipts.sort(key=lambda item: item["part_index"])
            self._save(stage="browser_delivered")
            delivery.confirmed(self.payload, provider="twitter", receipt_id=receipts[-1]["id"])
            return
        part = event["part"]
        plan = self.journal["plan"] or []
        index = next((i for i, entry in enumerate(plan) if entry["identity"] == part), None)
        if index is None:
            raise delivery.DeliveryUnknown("X browser operation is not in the approved ordered plan")
        if kind == "preparing":
            self._save(stage="browser_preparing", browser_part_index=index)
            return
        if kind in {"create_action", "media_request"}:
            create = kind == "create_action"
            if create and self.active_create:
                raise delivery.DeliveryUnknown("X browser already owns a post action")
            if create:
                if any(item.get("status") in {"started", "unknown"}
                       for item in self.journal["items"].values()):
                    raise delivery.DeliveryUnknown("X browser has unresolved media/create effects before Post")
                receipts = [item for item in self.journal["items"].values()
                            if item.get("effect") == "publication" and item.get("status") == "complete"]
                if (len(receipts) != index or {item["part_index"] for item in receipts} != set(range(index))
                        or (receipts and event.get("reply_to") != next(
                            item["id"] for item in receipts if item["part_index"] == index - 1))):
                    raise delivery.DeliveryUnknown("X browser must not replay or skip a retained thread prefix")
            elif self.active_create:
                raise delivery.DeliveryUnknown("X media write arrived after the post action fence")
            sequence = self._budget(part, "create" if create else "media",
                                    self.max_create_attempts if create else self.max_media_requests)
            name = f"{'create' if create else 'media'}:{index}:{sequence}"
            generation = uuid4().hex
            self.journal["items"][name] = {"status": "started", "part": part, "part_index": index,
                "effect": "publication" if create else "media_only", "operation_generation": generation,
                "target_id": self.journal["target_id"], "account_key": self.journal["account_key"],
                "submitted_at": delivery.utc_now()}
            if create:
                self.active_create = name
                self._save(submission_started=True, operation=f"browser-publish:twitter:{name}",
                           operation_generation=generation, operation_started_at=delivery.utc_now(),
                           stage="browser_creating")
            else:
                self._save(stage="browser_media_upload")
            return name
        name = event.get("name") or self.active_create
        item = self.journal["items"].get(name)
        statuses = {"started", "unknown"} if kind in {"receipt", "rejection"} else {"started"}
        if not isinstance(item, dict) or item["part"] != part or item["status"] not in statuses:
            raise delivery.DeliveryUnknown("X browser response does not match an owned operation")
        if (item["effect"] == "publication" and
                item["operation_generation"] != delivery.state(self.payload).get("operation_generation")):
            raise delivery.DeliveryUnknown("X browser response belongs to a stale operation generation")
        if kind == "create_request":
            if item["effect"] != "publication":
                raise delivery.DeliveryUnknown("Invalid X browser create fence")
            self._save(submission_started=True)  # Revalidate lease immediately before upstream POST.
            return
        status = event.get("httpStatus")
        if type(status) is int:
            item["httpStatus"] = status
        if kind == "unknown":
            item["status"] = "unknown"
            self._save()
            error = delivery.DeliveryUnknown(f"X browser {item['effect']} response is unknown")
            error.details = {"retrySafe": False, "effect": item["effect"],
                             "operationGeneration": item["operation_generation"]}
            raise error
        if kind == "receipt":
            receipt_id = event.get("id")
            if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9]+", receipt_id):
                raise delivery.DeliveryUnknown("X browser returned no usable provider receipt")
            item.update(status="complete", id=receipt_id,
                        evidence_source=event.get("evidence_source", "provider_response"))
            if item["effect"] == "publication":
                self.active_create = None
                self._save(submission_started=True, stage="browser_part_confirmed")
                delivery.accepted(self.payload, provider="twitter", receipt_id=receipt_id,
                                  operation_generation=item["operation_generation"],
                                  target_id=item["target_id"], account_key=item["account_key"],
                                  part=part, part_index=index, part_count=len(plan))
            else:
                self._save()
            return
        if kind == "rejection":
            # Native HTTP guard's documented safe subset; a 4xx label alone is
            # not permission to resolve every earlier effect (408/5xx unknown).
            safe = status in {401, 402, 429}
            item["status"] = "rejected"
            item["providerErrorCodes"] = [code for code in event.get("provider_codes", [])
                                          if type(code) is int][:8]
            self._save()
            all_safe = (safe and item["effect"] == "publication" and all(
                prior["effect"] == "media_only" and prior["status"] == "complete" and prior.get("id")
                or prior["effect"] == "publication" and prior["status"] == "rejected"
                   and prior.get("httpStatus") in {401, 402, 429}
                for prior in self.journal["items"].values()))
            details = {"httpStatus": status, "definitiveNonDelivery": safe,
                       "operationGeneration": item["operation_generation"], "retrySafe": bool(all_safe)}
            details["providerErrorCodes"] = item["providerErrorCodes"]
            if all_safe:
                self._save(x_browser_non_delivery={"target_id": item["target_id"],
                    "account_key": item["account_key"], "httpStatus": status,
                    "operation": delivery.state(self.payload).get("operation"),
                    "operation_generation": item["operation_generation"],
                    "journal_sha256": _x_digest(self.journal)})
                delivery.record_non_delivery(self.payload, provider="native", reason=f"X CreateTweet HTTP {status}")
            if not all_safe:
                error = delivery.DeliveryUnknown("X rejection retained; other effects need reconciliation")
                error.details = details
                raise error
            from myUtils.prepared_publishers import PreparedPublishError
            raise PreparedPublishError(f"HTTP {status}: X CreateTweet rejected", retryable=status == 429,
                                       details=details)
        raise delivery.DeliveryUnknown("Unsupported X browser evidence event")


def make_x_browser_observer(payload: dict, *, target_id: int, account_key: str,
                            max_create_attempts: int = 3, max_media_requests: int = 256):
    """Parent must pass canonical target.account_ref, not a cookie filename.

    Remove ONLY the X outer starting() fence when this callback is passed all
    the way into TwitterThreadVideo. No callback => retain the worker guard.
    Budgets here are per exact part and do not replace/refund route budgets.
    """
    return XBrowserObserver(payload, target_id=target_id, account_key=account_key,
                            max_create_attempts=max_create_attempts, max_media_requests=max_media_requests)
