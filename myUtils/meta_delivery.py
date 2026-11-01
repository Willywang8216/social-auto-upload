"""Resume Meta/Threads containers without recreating or repeating a publish."""
from __future__ import annotations

from myUtils import delivery, generic_delivery


def create_container(http, *, platform: str, user_id: str, access_token: str,
                     payload: dict, data: dict, operation: str = "container") -> str:
    from myUtils import prepared_publishers as publishers

    operations = generic_delivery.Operations(payload, platform, allow_receipt=True)
    prior = operations.get(operation)
    if prior:
        return prior["id"]
    root = publishers.FACEBOOK_GRAPH_ROOT if platform == "instagram" else publishers.THREADS_GRAPH_ROOT
    endpoint = "media" if platform == "instagram" else "threads"
    response = operations.request(operation, lambda: http.post(
        f"{root}/{user_id}/{endpoint}", data={**data, "access_token": access_token}, timeout=120))
    body = publishers._response_payload(response) or {}
    container_id = body.get("id") if isinstance(body, dict) else None
    operations.record(operation, container_id, provider_receipt=operation == "container", stage="container")
    return str(container_id)


def finish_container(http, *, platform: str, user_id: str, access_token: str,
                     payload: dict, container_id: str, ready: bool = False,
                     finalize: bool = True) -> dict:
    from myUtils import prepared_publishers as publishers

    prior = delivery.receipt_for(payload, platform)
    new_container = prior is None
    if prior is None:
        delivery.accepted(payload, provider=platform, receipt_id=container_id, stage="container")
        prior = delivery.receipt_for(payload, platform)
    container_id = str(prior["id"])

    def complete(media_id):
        delivery.accepted(payload, provider=platform, receipt_id=container_id,
                          status="delivered" if finalize else "processing",
                          stage="complete", media_id=str(media_id or ""))
        return {"container_id": container_id,
                "publish": {"id": media_id or (container_id if finalize else "")}}

    if prior.get("stage") == "complete" or prior.get("status") == "delivered":
        return complete(prior.get("media_id"))
    try:
        status = "FINISHED" if ready and (new_container or prior.get("stage") == "container") else publishers._wait_for_container_status(
            http, container_id, access_token, platform=platform, timeout=30)
    except delivery.LeaseLost:
        raise
    except publishers.PreparedPublishError as exc:
        if exc.retryable is False:
            raise
        delivery.pending_or_unknown(payload, f"{platform} container is still processing",
                                    retry_after=exc.details.get("retryAfter"))
    except Exception:
        delivery.pending_or_unknown(payload, f"{platform} container status is unavailable")
    if status == "PUBLISHED":
        return complete(prior.get("media_id"))
    if prior.get("stage") == "publishing":
        # FINISHED is not evidence that the previous media_publish was rejected.
        delivery.pending_or_unknown(payload, f"{platform} publish response was lost; awaiting reconciliation")
    delivery.accepted(payload, provider=platform, receipt_id=container_id, stage="publishing")
    root = publishers.FACEBOOK_GRAPH_ROOT if platform == "instagram" else publishers.THREADS_GRAPH_ROOT
    endpoint = "media_publish" if platform == "instagram" else "threads_publish"
    try:
        response = http.post(f"{root}/{user_id}/{endpoint}",
                             data={"creation_id": container_id, "access_token": access_token}, timeout=120)
    except delivery.LeaseLost:
        raise
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if not (isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 408):
            delivery.pending_or_unknown(payload, f"{platform} publish response was lost; poll the saved container")
    if 400 <= response.status_code < 500 and response.status_code != 408:
        delivery.accepted(payload, provider=platform, receipt_id=container_id, stage="container")
        try:
            publishers._raise_for_status(response)
        except publishers.PreparedPublishError as exc:
            retry_after = (getattr(response, "headers", None) or {}).get("Retry-After")
            if retry_after:
                exc.details["retryAfter"] = str(retry_after)
            raise
        raise publishers.PreparedPublishError(f"HTTP {response.status_code}: {platform} publish rejected")
    if not 200 <= response.status_code < 300:
        delivery.pending_or_unknown(payload, f"{platform} publish acceptance is unknown; poll the saved container")
    body = publishers._response_payload(response) or {}
    if not isinstance(body, dict) or not body.get("id"):
        delivery.pending_or_unknown(payload, f"{platform} publish returned no media ID")
    complete(str(body["id"]))
    return {"container_id": container_id, "publish": body}
