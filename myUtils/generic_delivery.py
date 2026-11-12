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
