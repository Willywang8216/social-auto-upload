"""Shared publishing callbacks, independent of the web application.

The draft prompts, language guards and artifact selectors are the existing
backend implementations. Database access is explicit; media preparation uses
only the configured public origin, never an HTTP request. ``submit_publish``
is the durable, always-asynchronous entry point shared by every producer.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path

from myUtils import campaign_media_prep
from myUtils import content_generator, content_rules, humanize, llm_client
from myUtils import jobs as job_runtime
from myUtils import media_groups as media_group_store
from myUtils import ops_alerts, platform_limits
from myUtils import profiles as profile_registry
from myUtils.campaign_media_prep import _resolve_ai_config
from myUtils.submissions import ensure_file_record_for_path, submit_publish as submit_publish
from myUtils.sqlite_utils import connect as _db_connection


def _artifact_payloads_for_platform(artifacts: list[dict], platform: str) -> list[dict]:
    raw_kinds = {"raw_remote_upload", "raw_local"}
    # Context artifacts created by the prep pipeline are NOT publishable media.
    # The backend auto-transcribes whenever the submit carried no transcript, so
    # ``audio`` (the extracted WAV) and ``transcript`` (the .txt) are present on
    # most campaigns. They have no ``metadata.role == "video"``, so they used to
    # land in ``passthrough`` *before* the video part, and the worker's
    # ``_prepared_artifact_local_paths()`` hands the uploader every path in
    # order — which published the WAV instead of the clip on bluesky/telegram/
    # nw_sw_blog (7 targets succeeded that way on 2026-10-18/19 and 357 more
    # were queued). Twitter raises "Unsupported prepared Twitter media type"
    # instead, which is how it surfaced. Never publish them.
    context_kinds = {"audio", "transcript"}
    if platform != profile_registry.PLATFORM_TIKTOK:
        # Drop the un-prepared raw artifacts and, for every video source, pick
        # the duration-safe cut(s) for this platform: the trimmed/split parts
        # when they exist, otherwise the full _pub.mp4.
        cleaned = [
            a for a in artifacts
            if a.get("artifact_kind") not in raw_kinds | context_kinds
        ]
        videos: dict[object, list[dict]] = {}
        passthrough: list[dict] = []
        for artifact in cleaned:
            role = (artifact.get("metadata") or {}).get("role")
            source_id = artifact.get("source_file_record_id")
            if role == "video" and source_id is not None:
                videos.setdefault(source_id, []).append(artifact)
            else:
                passthrough.append(artifact)
        selected: list[dict] = []
        for items in videos.values():
            selected.extend(_select_videos_for_platform(items, platform))
        return [*passthrough, *selected]

    grouped: dict[tuple[object, object], list[dict]] = {}
    passthrough: list[dict] = []
    for artifact in artifacts:
        kind = artifact.get("artifact_kind") or ""
        if kind in context_kinds:
            continue
        source_id = artifact.get("source_file_record_id")
        if kind in {"remote_upload", "raw_remote_upload", "local", "raw_local"} and source_id is not None:
            role = ((artifact.get("metadata") or {}).get("role"))
            grouped.setdefault((source_id, role), []).append(artifact)
        elif kind not in {"watermarked_image", "watermarked_video"}:
            passthrough.append(artifact)

    selected = []
    for items in grouped.values():
        raw = next((item for item in items if item.get("artifact_kind") in {"raw_remote_upload", "raw_local"}), None)
        selected.append(raw or items[0])
    return [*passthrough, *selected]


def _artifact_part_groups_for_platform(artifacts: list[dict], platform: str) -> list[list[dict]]:
    """Group a platform's artifacts into one list per post.

    A video too long for the platform is re-encoded into parts; each part is
    its own post. Everything already inside the cap yields a single group, so a
    caller that ignores this helper still publishes exactly as before.
    """
    selected = _artifact_payloads_for_platform(artifacts, platform)
    video_items = [a for a in selected if (a.get("metadata") or {}).get("role") == "video"]
    source_ids = {a.get("source_file_record_id") for a in video_items}
    if len(video_items) <= 1 or len(source_ids) > 1:
        return [selected]
    parts = sorted(
        (a for a in video_items if (a.get("metadata") or {}).get("part_index") is not None),
        key=lambda a: int((a.get("metadata") or {}).get("part_index") or 0),
    )
    if len(parts) <= 1:
        return [selected]
    passthrough = [a for a in selected if (a.get("metadata") or {}).get("role") != "video"]
    return [passthrough + [part] for part in parts]


def _select_videos_for_platform(items: list[dict], platform: str) -> list[dict]:
    """Pick the video artifact(s) whose duration fits ``platform``.

    Returns every split part when the source was divided (one post each),
    otherwise the largest trimmed variant under the cap, else the full artifact.
    """
    if not items:
        return []
    max_seconds = platform_limits.video_max_seconds(platform)
    if max_seconds is None:
        return [items[0]]
    max_mb = platform_limits.media_max_mb(platform)

    def _meta(artifact: dict) -> dict:
        return artifact.get("metadata") or {}

    def _fits(artifact: dict) -> bool:
        meta = _meta(artifact)
        seconds = meta.get("max_duration_seconds")
        if seconds is not None and float(seconds) > float(max_seconds):
            return False
        mb = meta.get("max_media_mb")
        if max_mb is not None and mb is not None and float(mb) > float(max_mb):
            return False
        return True

    def _planned_for(artifact: dict) -> bool:
        # A platform only picks up a split it asked for by default: a large-cap
        # platform such as YouTube must not grab a small-cap platform's finer
        # split. The untagged fallback below is the deliberate exception.
        planned = _meta(artifact).get("split_for")
        return not planned or platform in planned

    def _largest_fitting_parts(candidates: list[dict]) -> list[dict]:
        # Several split plans may exist (X 140s, Threads 300s, a size-only
        # split); use the largest parts that still fit, never a finer split.
        #
        # Group by part_count rather than by an exact duration. ffmpeg gives the
        # last part of an equal split a slightly different length (here 200.048s
        # against 200.083s - 35ms, from frame-boundary rounding). Comparing
        # durations for equality silently dropped that last part, so a third of
        # the video never published: a 3-part Threads split selected only parts 1
        # and 2. part_count identifies the same plan without depending on float
        # equality, and the median picks the plan whose parts are the largest.
        by_count: dict[int, list[dict]] = {}
        for artifact in candidates:
            count = int(_meta(artifact).get("part_count") or 0)
            by_count.setdefault(count, []).append(artifact)
        if not by_count:
            return []

        def _median_seconds(group: list[dict]) -> float:
            values = sorted(
                float(_meta(a).get("max_duration_seconds") or 0) for a in group
            )
            return values[len(values) // 2] if values else 0.0

        best_count = max(by_count, key=lambda c: (_median_seconds(by_count[c]), c))
        # A re-prep appends a fresh copy of every split part instead of
        # replacing the previous one (campaign_artifacts keeps growing), so the
        # same (part_count, part_index) can appear several times. Returning
        # every copy made _artifact_part_groups_for_platform emit one post per
        # copy: campaign 2602 queued each of its two X parts twice (4 posts per
        # account) and 2603 queued each of five parts twice. Collapse to one
        # artifact per index, preferring a copy that carries a public_url so a
        # URL-fetching publisher (and the Sociamonials fallback) still sees the
        # served copy.
        deduped: dict[int, dict] = {}
        for artifact in by_count[best_count]:
            index = int(_meta(artifact).get("part_index") or 0)
            current = deduped.get(index)
            if current is None or (
                not str(current.get("public_url") or "").strip()
                and str(artifact.get("public_url") or "").strip()
            ):
                deduped[index] = artifact
        return sorted(
            deduped.values(),
            key=lambda a: int(_meta(a).get("part_index") or 0),
        )

    part_artifacts = [a for a in items if _meta(a).get("part_index") is not None]
    parts = [a for a in part_artifacts if _fits(a) and _planned_for(a)]
    if parts:
        return _largest_fitting_parts(parts)

    # Untagged fallback. A platform can be missing from ``split_for`` (it was
    # added after prep, or the prep-time probe read the source at/below its cap)
    # while the full artifact later turns out to be over it. The full artifact
    # carries no duration in its metadata, so ``_fits`` cannot reject it and the
    # publisher's own guard is left to fail on the oversized file. Recover the
    # source duration/size the split was built from (part value x part count)
    # and, when it exceeds this platform's cap, use the fitting untagged parts
    # instead of the full artifact. Gated on the inferred source so a large-cap
    # platform still keeps the full video.
    untagged = [a for a in part_artifacts if _fits(a) and not _planned_for(a)]
    if untagged:
        inferred_seconds = max(
            float(_meta(a).get("max_duration_seconds") or 0)
            * int(_meta(a).get("part_count") or 0)
            for a in untagged
        )
        inferred_mb = max(
            float(_meta(a).get("max_media_mb") or 0)
            * int(_meta(a).get("part_count") or 0)
            for a in untagged
        )
        over_time = bool(max_seconds and inferred_seconds and inferred_seconds > float(max_seconds))
        over_size = bool(max_mb and inferred_mb and inferred_mb > float(max_mb))
        if over_time or over_size:
            return _largest_fitting_parts(untagged)

    variants = [
        a for a in items
        if (a.get("metadata") or {}).get("max_duration_seconds")
        and (a.get("metadata") or {}).get("part_index") is None
    ]
    fitting = [
        a for a in variants
        if float((a.get("metadata") or {}).get("max_duration_seconds")) <= float(max_seconds)
    ]
    if fitting:
        return [max(
            fitting,
            key=lambda a: float((a.get("metadata") or {}).get("max_duration_seconds")),
        )]
    full = [a for a in items if not (a.get("metadata") or {}).get("max_duration_seconds")]
    if not full:
        return [items[0]]
    # Several artifacts can describe the same full file: the watermarked_video
    # row plus the ``local`` row that carries the served /getFile URL. They
    # share a local_path, so _extract_media would merge them - but only one
    # survives this selection. Prefer the one with a public_url so a
    # link-capable platform (Reddit) posts a link instead of silently
    # degrading to a text-only self post when a URL was available.
    return [next((a for a in full if a.get("public_url")), full[0])]


def _ensure_file_record_for_path(file_path: str, *, db_path: Path) -> int:
    """Reuse registered (including remote) files; only register safe local files."""
    return ensure_file_record_for_path(file_path, db_path=db_path)


def _prepare_campaign_media_artifacts(
    campaign_id: int,
    profile: profile_registry.Profile,
    media_files: list[dict],
    request_data: dict,
    *,
    selected_platforms: set[str] | None = None,
    db_path: Path,
) -> dict:
    """Prepare media with a configured public origin, without request context."""
    return campaign_media_prep.prepare_campaign_media_artifacts(
        campaign_id,
        profile,
        media_files,
        request_data,
        selected_platforms=selected_platforms,
        db_path=db_path,
        public_base_url=ops_alerts.public_app_origin() or None,
    )


def _fallback_generated_draft(
    platform: str,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
) -> dict:
    if request_data.get("_accountLanguage"):
        raise RuntimeError(
            "Account-language content generation requires an available LLM; "
            "refusing to publish source-language fallback copy"
        )
    # Order by how likely the source is to be real copy. The media-group name
    # and the batch importer's brief are deliberately NOT candidates: they are
    # labels, not captions, and publishing them is a visible defect. The brief
    # (``request_data['notes']``) is the operator's *instruction* to the LLM
    # ("SFW invitation to ...", "adult, honest, 18+ only"), so echoing it as
    # the caption produces exactly the generic copy the pre-publish guard
    # rejects. Drop it from the candidate list entirely: when the LLM is down
    # and there is no transcript, surface a real failure instead of queueing a
    # placeholder that can never pass the guard.
    transcript = str(media_context.get("transcriptText", "") or "").strip()
    title = str(request_data.get("title", "") or "").strip()
    message = ""
    for candidate in (transcript, title):
        if content_rules.is_usable_copy(candidate):
            message = candidate
            break
    if not message:
        raise RuntimeError(
            "Cannot generate copy: the LLM is unavailable and there is no "
            "usable transcript or explicit title to fall back to. The operator "
            "brief is an instruction, not a caption, and is never published."
        )
    return {
        "message": message[:1000],
        "hashtags": request_data.get("hashtags") or [],
        "firstComment": str(request_data.get("firstComment", "") or "").strip(),
    }


def _build_generation_prompt(
    platform: str,
    profile: profile_registry.Profile,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
) -> tuple[str, str]:
    rule = content_rules.get_platform_rule(platform)
    system_prompt = str((profile.settings or {}).get("systemPrompt", "") or "").strip()
    if not system_prompt:
        system_prompt = "You write concise, platform-native social media copy."
    user_lines = [
        f"Platform: {platform}",
        f"Media group: {media_group.name}",
        f"Max chars: {rule.max_chars if rule.max_chars is not None else 'none'}",
        f"Required hashtag count: {rule.hashtag_count}",
        f"Require emoji: {rule.require_emoji}",
        f"Require contact details: {rule.require_contact_details}",
        f"Require CTA: {rule.require_cta}",
        f"Title: {request_data.get('title', '')}",
        f"Notes: {request_data.get('notes', '')}",
        f"Transcript: {media_context.get('transcriptText', '')}",
        f"Contact details: {request_data.get('contactDetails', '')}",
        f"CTA: {request_data.get('cta', '')}",
    ]
    if media_context.get("imageLocalPaths"):
        user_lines.append(
            "Attached image(s): describe what is actually visible (pose, setting, "
            "mood) in the copy; do not invent details that are not in the frame."
        )
    # Humanizer rules apply to every language. They exist because generated
    # copy reads as AI-written and the operator publishes it as their own voice.
    user_lines.extend((
        "",
        "HUMANIZER RULES (apply in every language): write like a real person, not "
        "a marketing bot. Use concrete, specific details from the media; vary the "
        "sentence length; contractions are fine. NEVER use these AI tells: 'delve', "
        "'in today's fast-paced world', 'unleash', 'elevate', 'embark', 'testament "
        "to', 'tapestry', 'navigate the landscape', 'game-changer', 'seamlessly', "
        "'leverage', 'utilize', 'it's not just X, it's Y', 'whether you're a X or a "
        "Y', 'in conclusion', 'furthermore', 'moreover'. Do not force a neat "
        "tricolon or a tidy moral. No emoji spam; one or two only if the platform "
        "rule asks for emoji.",
    ))
    account_context = str(request_data.get("_accountContext") or "").strip()
    if account_context:
        user_lines.append("")
        user_lines.append("Account context (apply any account-specific rules from the system prompt):")
        user_lines.append(account_context)
    language = str(request_data.get("_accountLanguage") or "").strip()
    if language:
        labels = content_generator._parse_languages(language)
        if any("Chinese" in label for label in labels) or "zh" in language.lower():
            user_lines.extend((
                "",
                "TAIWAN MANDARIN RULES: write Traditional Chinese as used in Taiwan. "
                "Never output Simplified characters. Use Taiwan vocabulary and "
                "punctuation: 影片(not 视频), 網路(not 网络), 資訊(not 信息), "
                "軟體(not 软件), 品質(not 质量), 專案(not 项目), 透過(not 通过), "
                "鏡頭, 拍照, 感受, 身體. Use full-width punctuation ，。！？、. Do not "
                "write English sentences inside the Chinese copy.",
            ))
        if len(labels) == 1:
            user_lines.extend((
                "",
                "LANGUAGE REQUIREMENT (highest priority): Write the entire output, "
                f"including the message, in {labels[0]}. Do not use another language, "
                "provide a translation, or mix languages.",
            ))
        elif len(labels) > 1:
            primary, *secondary = labels
            user_lines.extend((
                "",
                "LANGUAGE REQUIREMENT (highest priority): This account explicitly "
                f"requests bilingual copy in {' and '.join(labels)}. Write the complete "
                f"{primary} version first, then a standalone --- line, then the complete "
                f"{' then '.join(secondary)} version. Do not omit either language.",
            ))
    if platform in ("nw_sw_blog", "teaching_blog"):
        user_lines.extend((
            "",
            "BLOG ARTICLE REQUIREMENT: this platform publishes a long-form blog "
            "article. The 'message' field MUST be a complete 700-1500 word Markdown "
            "article: one # H1 title, several H2/H3 sections, short paragraphs, and "
            "a conclusion. Never output a short social caption or just a title. "
            "If the transcript/notes are thin, expand with original, on-topic "
            "reasoning and concrete detail from the persona's life; do not repeat "
            "the title as the body.",
        ))
    user_lines.append(
        "Return JSON with keys: message, title, summary, description, altText, "
        "hashtags, firstComment, contactDetails, cta. Every text field must follow "
        "the language rules above and the writing rules that follow."
    )
    return system_prompt, "\n".join(user_lines)


def _generate_platform_draft(
    platform: str,
    profile: profile_registry.Profile,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
) -> dict:
    ai_config = _resolve_ai_config(profile)
    has_ai = bool(
        ai_config["api_base_url"] and ai_config["api_key"]
    ) or bool(
        os.environ.get("SAU_LLM_API_KEY") and os.environ.get("SAU_LLM_API_BASE_URL")
    )
    should_use_llm = bool(request_data.get("useLlm", True)) and has_ai
    account_language = str(request_data.get("_accountLanguage") or "").strip()
    if platform == "twitter" and len(content_generator._parse_languages(account_language)) > 1:
        raise ValueError("X posts support one audience language per account; configure separate accounts for separate languages")
    if account_language and not should_use_llm:
        raise RuntimeError("An LLM is required for language-specific account copy; refusing source-language fallback")

    raw_draft = None
    if should_use_llm:
        try:
            system_prompt, user_prompt = _build_generation_prompt(
                platform,
                profile,
                media_group,
                request_data,
                media_context,
            )
            kwargs = {}
            if ai_config["api_base_url"]:
                kwargs["api_base_url"] = ai_config["api_base_url"]
            if ai_config["api_key"]:
                kwargs["api_key"] = ai_config["api_key"]
            if ai_config["model"]:
                kwargs["model"] = ai_config["model"]
            # Send the actual image(s) so the copy describes what is shown
            # rather than inventing from the filename. Text-only models that
            # reject the image parts fall through to the except below and the
            # copy still generates from the transcript/notes.
            vision_paths = [
                str(path)
                for path in (media_context.get("imageLocalPaths") or [])[:4]
                if path and Path(str(path)).is_file()
            ]
            if vision_paths:
                kwargs["images"] = vision_paths
            result = llm_client.generate_chat_completion(system_prompt, user_prompt, **kwargs)
            raw_draft = (
                result.parsed_json
                or llm_client.coerce_json_object(result.content)
                or {"message": result.content}
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "LLM generation failed for %s: %s",
                platform,
                exc,
            )

    if not isinstance(raw_draft, dict):
        raw_draft = _fallback_generated_draft(platform, media_group, request_data, media_context)

    prepared = content_rules.prepare_platform_draft(
        platform,
        raw_draft,
        contact_details=str(request_data.get("contactDetails", "") or "").strip(),
        cta=str(request_data.get("cta", "") or "").strip(),
        default_hashtags=request_data.get("hashtags") or [],
    )
    message = str(prepared.get("message") or "")
    if not content_rules.is_usable_copy(message):
        raise RuntimeError(
            "Generated copy looks like a placeholder (media-group name, filename "
            "or generic brief); refusing to queue it"
        )
    if platform in ("nw_sw_blog", "teaching_blog") and len(message.strip()) < 600:
        raise RuntimeError(
            "Generated blog article is too short "
            f"({len(message.strip())} chars); refusing to queue an empty article"
        )
    if account_language and not content_rules.message_matches_language(
        message, account_language
    ):
        raise RuntimeError(
            f"Generated copy does not match the account language "
            f"'{account_language}'; refusing to queue it"
        )
    return prepared


def _account_audience_language(account, profile) -> str:
    config = getattr(account, "config", None) or {}
    language = config.get("audience_language") or config.get("audienceLanguage")
    settings = getattr(profile, "settings", None) or {}
    if isinstance(settings, dict):
        language = language or settings.get("default_language") or settings.get("defaultLanguage")
    else:
        language = language or getattr(profile, "default_language", "")
    return str(language or "").strip()


def _generate_account_draft(
    account: profile_registry.Account,
    profile: profile_registry.Profile,
    media_group: media_group_store.MediaGroup,
    request_data: dict,
    media_context: dict,
    *,
    regenerate: bool = False,
) -> dict:
    """Generate a draft for a specific account, honouring per-account
    rules the user may have written in the profile's system prompt.

    The user's system prompt may include a *Platform specification* (or
    *Account specification*) section that lists per-platform or per-account
    rules. We surface the account context to the LLM so it can apply those
    rules. When ``regenerate`` is true a small randomised nonce is appended
    to the user prompt so the LLM produces a fresh variation.

    The account context is passed via a side channel
    (``_accountContext``) that only the LLM prompt builder reads — never
    via ``notes``, since the fallback path echoes ``notes`` directly into
    the visible message body.
    """
    extended_data = dict(request_data or {})
    nonce_suffix = ""
    if regenerate:
        nonce_suffix = f"\nRegeneration nonce: {uuid.uuid4().hex[:8]}"

    account_context_lines = [
        f"Platform: {account.platform}",
        f"Account name: {account.account_name}",
        "If the system prompt contains a 'Platform specification' or "
        "'Account specification' section with rules for this account, follow them.",
    ]
    extended_data["_accountContext"] = "\n".join(account_context_lines) + nonce_suffix
    extended_data["_accountLanguage"] = _account_audience_language(account, profile)
    draft = _generate_platform_draft(
        account.platform,
        profile,
        media_group,
        extended_data,
        media_context,
    )
    # Pass 2: the humanizer skills' deterministic check plus one bounded rewrite.
    # A draft that fails to improve is returned untouched — this must never block
    # a publish.
    try:
        humanize.humanize_fields(
            draft,
            language=_account_audience_language(account, profile),
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "humanize pass failed for account %s", getattr(account, "id", "?"), exc_info=True
        )
    return draft


def _job_to_payload(job: job_runtime.Job) -> dict:
    payload = job.payload
    return {
        "id": job.id,
        "idempotencyKey": job.idempotency_key,
        "platform": job.platform,
        "profileId": job.profile_id,
        "status": job.status,
        "totalTargets": job.total_targets,
        "completedTargets": job.completed_targets,
        "failedTargets": job.failed_targets,
        "createdAt": job.created_at,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "payload": payload,
        "title": job_runtime._action_title(payload),
    }


def _account_display_name(account_ref: str, *, db_path: Path) -> str:
    """Resolve against the caller's DB, without a cross-database name cache."""
    if not account_ref or not isinstance(account_ref, str):
        return account_ref or ""
    if not account_ref.startswith("account:"):
        return account_ref
    try:
        account_id = int(account_ref.split(":", 1)[1])
        with _db_connection(db_path) as conn:
            row = conn.execute(
                "SELECT nickname, account_name FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return str((row[0] or row[1] or "") if row else "").strip() or account_ref
    except Exception:  # noqa: BLE001 — any miss falls back to the raw ref
        return account_ref


def _tg_review_cards(*, jobs: list[dict], db_path: Path) -> list[dict]:
    """Build one Telegram review card per queued campaign post.

    Read back from the DB rather than from the submit result: the job row
    carries the draft that will actually be sent plus the artifacts that were
    prepared, and its targets carry the schedule the worker will honour.
    """
    cards: list[dict] = []
    for job in jobs or []:
        job_id = job.get("id")
        if job_id is None:
            continue
        try:
            with _db_connection(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, profile_id, platform, payload_json FROM publish_jobs WHERE id = ?",
                    (int(job_id),),
                ).fetchone()
                if row is None:
                    continue
                targets = conn.execute(
                    "SELECT account_ref, schedule_at FROM publish_job_targets "
                    "WHERE job_id = ? ORDER BY id",
                    (int(job_id),),
                ).fetchall()
        except Exception:  # noqa: BLE001 — a missing card must not break submit
            logging.getLogger(__name__).debug("tg review: job read failed", exc_info=True)
            continue

        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
        copy_text = str(draft.get("message") or payload.get("message") or "").strip()
        artifacts = payload.get("artifacts") or []
        first = artifacts[0] if artifacts else {}
        media_path = str(first.get("local_path") or "")
        public_url = str(first.get("public_url") or "")

        labels: list[str] = []
        schedules: list[str] = []
        for target in targets:
            ref = str(target["account_ref"] or "")
            name = _account_display_name(ref, db_path=db_path)
            labels.append(f"{row['platform']} — {name}" if name and name != ref else str(row["platform"]))
            if target["schedule_at"]:
                schedules.append(str(target["schedule_at"]))
        try:
            profile = profile_registry.get_profile(int(row["profile_id"]), db_path=db_path)
            profile_label = f"{profile.name} (#{profile.id})"
        except Exception:  # noqa: BLE001
            profile_label = f"profile {row['profile_id']}"

        # Deep link into the app so the operator can open this post, edit its
        # copy, or pause it before it publishes.
        app_url = None
        try:
            from myUtils import ops_alerts as _ops_alerts
            origin = _ops_alerts.public_app_origin()
            if origin:
                mgid = None
                cid = payload.get("campaignId")
                if cid is not None:
                    with _db_connection(db_path) as _c:
                        _r = _c.execute(
                            "SELECT media_group_id FROM campaigns WHERE id = ?",
                            (int(cid),),
                        ).fetchone()
                        if _r and _r[0] is not None:
                            mgid = int(_r[0])
                app_url = (
                    f"{origin}/#/publish/queue?entity=mg-{mgid}"
                    if mgid is not None
                    else f"{origin}/#/publish/queue?job={int(row['id'])}"
                )
        except Exception:  # noqa: BLE001 — a missing link must not break submit
            app_url = None

        cards.append({
            "job_id": int(row["id"]),
            "campaign_id": payload.get("campaignId"),
            "post_id": payload.get("campaignPostId"),
            "profile_id": int(row["profile_id"]),
            "profile_label": profile_label,
            "schedule_label": (min(schedules) + " UTC") if schedules else "immediate",
            "account_labels": sorted(set(labels)),
            "copy_text": copy_text,
            "media_path": media_path or None,
            "media_label": Path(media_path).name if media_path else "",
            "public_url": public_url,
            "app_url": app_url,
        })
    return cards


def _notify_tg_review(*, jobs: list[dict], db_path: Path) -> None:
    """Queue the review cards off the request path (Telegram must not block)."""
    try:
        from myUtils import tg_review
        cards = _tg_review_cards(jobs=jobs, db_path=db_path)
        if cards:
            tg_review.notify_async(cards=cards, db_path=db_path)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("tg review: notify skipped", exc_info=True)
