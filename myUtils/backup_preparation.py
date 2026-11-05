"""Read-only bridge from verified backup grants to pre-queue media constraints.

No publisher is called, no upload grant is requested, and no credentials enter the
returned context. Network verification and media validation belong outside queue
write transactions. A later publish must recheck all grants, quota and strict
provider validation; this context is not an authorization token.
"""
from __future__ import annotations

from collections.abc import Mapping

from myUtils import image_compliance, media_compliance, platform_limits
from myUtils import sociamonials_fallback as adapter, sociamonials_policy as policy


def resolve_constraints(*, platform: str, account, payload: dict | None = None,
                        settings=None) -> dict:
    """Fresh exact-destination verification, returning tightening-only policy."""
    verified = adapter.verify_destination(platform=platform, account=account,
                                          settings=settings, payload=payload)
    return constraints_from_verification(verified, platform=platform,
                                         account_config=getattr(account, "config", None))


def constraints_from_verification(verified: Mapping, *, platform: str,
                                  account_config: dict | None = None) -> dict:
    if not isinstance(verified, Mapping) or verified.get("requires_approval") is not False:
        raise media_compliance.MediaPreparationError("Backup requires approval or has unknown approval policy")
    route = verified.get("mapping")
    caps = verified.get("limits")
    if (not isinstance(route, Mapping) or not isinstance(caps, Mapping)
            or route.get("network") != adapter._platform_network(platform)
            or not isinstance(route.get("profile_refs"), list) or len(route["profile_refs"]) != 1
            or not isinstance(route["profile_refs"][0], str) or not route["profile_refs"][0].strip()
            or not str(route.get("workspace_id") or "").strip()):
        raise media_compliance.MediaPreparationError("Backup preparation needs one verified exact destination")
    hosted = caps.get("hosted_video_max_bytes")
    external = caps.get("external_video_max_bytes")
    image_bytes = caps.get("image_max_bytes")
    count = caps.get("max_images")
    if (hosted is not None and (type(hosted) is not int or hosted <= 0)
            or type(external) is not int or external <= 0
            or type(image_bytes) is not int or image_bytes <= 0
            or type(count) is not int or count < 0):
        raise media_compliance.MediaPreparationError("Backup preparation has invalid transport constraints")
    # No hosted entitlement means external/reused media ONLY, not unlimited
    # hosting. Its separately documented finite ceiling still bounds preparation.
    byte_cap = min(platform_limits.BACKUP_MEDIA_MAX_BYTES, external,
                   hosted if hosted is not None else external)
    video = media_compliance.effective_limits(platform, account_config=account_config,
        backup_compatible=True, limits={"max_bytes": byte_cap,
            "max_seconds": caps.get("video_max_seconds"), "max_items": 1})
    images = None
    if count:
        images = image_compliance.effective_limits(platform, account_config=account_config,
            backup_compatible=True, limits={"max_bytes": image_bytes, "max_items": count})
    return {"network": route["network"], "profile_refs": list(route["profile_refs"]),
            "workspace_id": str(route["workspace_id"]), "policy_version": caps.get("policy_version"),
            "hosted_video_entitled": hosted is not None,
            "video_limits": video, "image_limits": images}


def validate_intent(payload: dict, *, context: dict, account_config: dict | None = None,
                    prepared: bool = True) -> None:
    """Prove that original text/settings/accessibility can be represented.

    Counts are checked on FINAL parts. Before splitting, the caller may validate
    text and settings with prepared=False; no approved field is modified.
    """
    network = context["network"]
    if prepared:
        media = adapter.collect_media(payload)
    else:
        # Source containers/codecs may require normalization. They are not yet
        # backup transport files, so inspect intent roles without rejecting MKV
        # (or another intact source) before the final-output gate can encode it.
        from myUtils.publish_media_guard import is_context_artifact, is_support_artifact, media_kind
        media = []
        for artifact in payload.get("artifacts") or []:
            if is_context_artifact(artifact):
                continue
            kind = "thumbnail" if is_support_artifact(artifact) else media_kind(artifact)
            if kind is None:
                raise media_compliance.MediaPreparationError("Original media has no supported preparation kind")
            media.append({"kind": kind, "metadata": artifact.get("metadata") or {}, "artifact": artifact})
    main = [item for item in media if item["kind"] != "thumbnail"]
    covers = [item for item in media if item["kind"] == "thumbnail"]
    kinds = {item["kind"] for item in main}
    if prepared and (len(kinds) > 1 or sum(item["kind"] == "video" for item in main) > 1):
        raise media_compliance.MediaPreparationError("Backup requires complete separately scheduled media intents")
    kind = next(iter(kinds)) if len(kinds) == 1 else None
    if covers and (network not in policy.THUMBNAIL_NETWORKS or (prepared and (len(covers) != 1 or kind != "video"))):
        raise media_compliance.MediaPreparationError("Original cover cannot be preserved by the backup")
    if prepared and kind == "image":
        limits = context["image_limits"]
        if not limits or len(main) > limits["max_items"]:
            raise media_compliance.MediaPreparationError("Backup image count requires all ordered parts before queueing")
    mapped = policy.map_intent(payload, network=network,
        message=adapter.compose_message(payload, network=network), account_config=account_config,
        media_kind=kind, media_count=len(main))
    alts = [item["metadata"].get("alt_text", item["artifact"].get("alt_text")) for item in main]
    if any(value is not None for value in alts):
        if (kind != "image" or network in {"in", "gmb", "yt", "tiktok"}
                or not all(isinstance(value, str) for value in alts)
                or any(value != alts[0] for value in alts)):
            raise media_compliance.MediaPreparationError("Original per-image alt text cannot be preserved by the backup")
        if mapped["top_fields"].get("alt_text") is not None and mapped["top_fields"]["alt_text"] != alts[0]:
            raise media_compliance.MediaPreparationError("Original global and per-image alt text conflict")
    if mapped["top_fields"].get("video_thumbnail_url") and not covers:
        raise media_compliance.MediaPreparationError("Original cover URL needs an exact checksum-backed cover artifact")
    if prepared and not context["hosted_video_entitled"] and kind == "video":
        # An immutable prepared derivative cannot inherit an oversized original
        # URL. Reused assets or finite, byte-verified stable URLs are allowed.
        # Here only the stable-URL path is proved; the adapter also independently
        # supports exact ready assets at publish time, without quota deletion.
        for item in main:
            if not item.get("public_url"):
                raise media_compliance.MediaPreparationError("No hosted video entitlement: verify an exact ready asset or stable public URL")
            checked = adapter._checked_media([item], network)[0]
            adapter._external_reference(checked, adapter._http(None, ""), timeout=45)
