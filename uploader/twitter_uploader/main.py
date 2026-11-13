from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import json
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Sequence
from urllib.parse import parse_qs, urlsplit

from myUtils import delivery

from patchright.async_api import BrowserContext
from patchright.async_api import Page
from patchright.async_api import Route
from patchright.async_api import async_playwright

from utils.conf_defaults import DEBUG_MODE, LOCAL_CHROME_HEADLESS
from utils.log import twitter_logger
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.browser_hook import get_browser_options

TWITTER_COMPOSE_URL = "https://x.com/compose/post"
TWITTER_STATUS_URL_TEMPLATE = "https://x.com/i/status/{post_id}"
TWITTER_MAX_VIDEO_SECONDS = 140.0
TWITTER_SPLIT_SEGMENT_SECONDS = 139.0
TWITTER_MAX_IMAGES_PER_TWEET = 4
TWITTER_MAX_IMAGE_BYTES = 5 * 1024 * 1024
TWITTER_IMAGE_UPLOAD_QUALITY = 88
TWITTER_POST_BUTTON_READY_TIMEOUT_MS = 45000
TWITTER_POST_BUTTON_POLL_SECONDS = 0.25
TWITTER_POST_BUTTON_TOPMOST_BUDGET_MS = 6000
TWITTER_POST_CLICK_TIMEOUT_MS = 8000
TWITTER_ATTACHMENT_READY_TIMEOUT_MS = 240000
TWITTER_TEXTBOX_SELECTOR = "[data-testid='tweetTextarea_0']"
TWITTER_FILE_INPUT_SELECTOR = "input[data-testid='fileInput']"
TWITTER_ATTACHMENT_SELECTOR = "[data-testid='attachments']"
TWITTER_POST_BUTTON_SELECTORS = (
    "[data-testid='tweetButton']",
    "[data-testid='tweetButtonInline']",
)
TWITTER_REPLY_BUTTON_SELECTOR = "[data-testid='reply']"
TWITTER_CREATE_TWEET_RESPONSE_TOKEN = "CreateTweet"
FFPROBE_COMMAND = "ffprobe"
FFMPEG_COMMAND = "ffmpeg"


@dataclass(frozen=True, slots=True)
class PlannedTwitterSegment:
    source_path: Path
    source_index: int
    segment_index: int
    start_seconds: float
    duration_seconds: float
    requires_split: bool
    media_paths: tuple[Path, ...] = ()

    @property
    def upload_paths(self) -> tuple[Path, ...]:
        """Every file this post attaches, in upload order."""
        return self.media_paths or (self.source_path,)


@dataclass(frozen=True, slots=True)
class PreparedTwitterSegment:
    source_path: Path
    upload_path: Path
    source_index: int
    segment_index: int
    start_seconds: float
    duration_seconds: float
    requires_split: bool
    media_paths: tuple[Path, ...] = ()
    post_index: int = 0

    @property
    def upload_paths(self) -> tuple[Path, ...]:
        """Every file this post attaches, in upload order."""
        return self.media_paths or (self.upload_path,)

    @property
    def is_image_post(self) -> bool:
        return bool(self.media_paths) and not self.requires_split


SubprocessRunner = Callable[..., subprocess.CompletedProcess]
DurationReader = Callable[[Path], float]
PublishStep = Callable[[PreparedTwitterSegment, str | None], Awaitable[str]]
ImageNormalizer = Callable[[Path, Path, int], Path]


def is_image_path(file_path: str | Path) -> bool:
    return Path(file_path).suffix.lower() in BaseVideoUploader.SUPPORTED_IMAGE_EXTENSIONS


def is_video_path(file_path: str | Path) -> bool:
    return Path(file_path).suffix.lower() in BaseVideoUploader.SUPPORTED_VIDEO_EXTENSIONS


def prepare_image_for_upload(
    file_path: str | Path,
    temp_root: str | Path,
    index: int,
    *,
    max_bytes: int = TWITTER_MAX_IMAGE_BYTES,
) -> Path:
    """Return an X-uploadable image, shrinking anything over the size cap.

    X rejects stills larger than 5 MB, which is easy to hit with the camera
    originals in the Nakedwill / Sexualwill libraries. Anything already within
    the cap is passed through untouched; the rest is re-encoded as JPEG into
    ``temp_root`` (the caller's temp dir, cleaned up after publishing).
    """
    source = Path(file_path).expanduser().resolve()
    try:
        if source.stat().st_size <= max_bytes:
            return source
    except OSError:
        return source

    from PIL import Image

    destination = Path(temp_root) / f"image-{index:03d}.jpg"
    with Image.open(source) as opened:
        image = opened.convert("RGB")

    quality = TWITTER_IMAGE_UPLOAD_QUALITY
    scale = 1.0
    for _ in range(12):
        candidate = image
        if scale < 1.0:
            candidate = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.LANCZOS,
            )
        candidate.save(destination, format="JPEG", quality=quality, optimize=True)
        if destination.stat().st_size <= max_bytes:
            break
        # Shave quality first (cheap, keeps dimensions), then fall back to
        # progressively smaller frames for pathologically large sources.
        if quality > 45:
            quality -= 10
        else:
            scale *= 0.85
            quality = TWITTER_IMAGE_UPLOAD_QUALITY
    return destination


def run_subprocess(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def probe_video_duration(
    file_path: str | Path,
    *,
    runner: SubprocessRunner = run_subprocess,
) -> float:
    resolved = Path(file_path).expanduser().resolve()
    completed = runner(
        [
            FFPROBE_COMMAND,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(resolved),
        ],
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout or "{}")
    duration = payload.get("format", {}).get("duration")
    if duration is None:
        raise ValueError(f"ffprobe did not return a duration for {resolved}")
    return float(duration)


def plan_video_segments(
    file_path: str | Path,
    *,
    duration_seconds: float,
    source_index: int,
    max_unsplit_seconds: float = TWITTER_MAX_VIDEO_SECONDS,
    split_segment_seconds: float = TWITTER_SPLIT_SEGMENT_SECONDS,
) -> list[PlannedTwitterSegment]:
    resolved = Path(file_path).expanduser().resolve()
    if duration_seconds <= max_unsplit_seconds:
        return [
            PlannedTwitterSegment(
                source_path=resolved,
                source_index=source_index,
                segment_index=0,
                start_seconds=0.0,
                duration_seconds=duration_seconds,
                requires_split=False,
            )
        ]

    segments: list[PlannedTwitterSegment] = []
    segment_index = 0
    start_seconds = 0.0
    while start_seconds < duration_seconds:
        remaining = duration_seconds - start_seconds
        segment_duration = min(split_segment_seconds, remaining)
        segments.append(
            PlannedTwitterSegment(
                source_path=resolved,
                source_index=source_index,
                segment_index=segment_index,
                start_seconds=start_seconds,
                duration_seconds=segment_duration,
                requires_split=True,
            )
        )
        start_seconds += segment_duration
        segment_index += 1
    return segments


def plan_image_segments(
    file_paths: Sequence[str | Path],
    *,
    first_source_index: int = 0,
    max_images_per_tweet: int = TWITTER_MAX_IMAGES_PER_TWEET,
) -> list[PlannedTwitterSegment]:
    """Group stills into posts of at most ``max_images_per_tweet`` images.

    X accepts four images per tweet, so a larger batch becomes a reply chain
    instead of one oversized post.
    """
    resolved = [Path(file_path).expanduser().resolve() for file_path in file_paths]
    segments: list[PlannedTwitterSegment] = []
    for offset in range(0, len(resolved), max_images_per_tweet):
        batch = resolved[offset : offset + max_images_per_tweet]
        segments.append(
            PlannedTwitterSegment(
                source_path=batch[0],
                source_index=first_source_index + offset,
                segment_index=0,
                start_seconds=0.0,
                duration_seconds=0.0,
                requires_split=False,
                media_paths=tuple(batch),
            )
        )
    return segments


def plan_thread_segments(
    file_paths: Sequence[str | Path],
    *,
    duration_reader: DurationReader = probe_video_duration,
    max_unsplit_seconds: float = TWITTER_MAX_VIDEO_SECONDS,
    split_segment_seconds: float = TWITTER_SPLIT_SEGMENT_SECONDS,
    max_images_per_tweet: int = TWITTER_MAX_IMAGES_PER_TWEET,
) -> list[PlannedTwitterSegment]:
    """Plan the reply chain for a batch of videos, stills, or both.

    Consecutive images are batched four to a tweet; every video is probed and
    split when it runs past the platform limit. Upload order is preserved so
    the thread reads the same way it was handed to us.
    """
    planned: list[PlannedTwitterSegment] = []
    pending_images: list[Path] = []
    pending_start_index = 0

    def flush_images() -> None:
        if not pending_images:
            return
        planned.extend(
            plan_image_segments(
                pending_images,
                first_source_index=pending_start_index,
                max_images_per_tweet=max_images_per_tweet,
            )
        )
        pending_images.clear()

    for source_index, file_path in enumerate(file_paths):
        resolved = Path(file_path).expanduser().resolve()
        if is_image_path(resolved):
            if not pending_images:
                pending_start_index = source_index
            pending_images.append(resolved)
            if len(pending_images) >= max_images_per_tweet:
                flush_images()
            continue

        flush_images()
        duration_seconds = float(duration_reader(resolved))
        planned.extend(
            plan_video_segments(
                resolved,
                duration_seconds=duration_seconds,
                source_index=source_index,
                max_unsplit_seconds=max_unsplit_seconds,
                split_segment_seconds=split_segment_seconds,
            )
        )

    flush_images()
    return planned


def split_video_segment(
    segment: PlannedTwitterSegment,
    output_path: str | Path,
    *,
    runner: SubprocessRunner = run_subprocess,
) -> Path:
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    runner(
        [
            FFMPEG_COMMAND,
            "-y",
            "-ss",
            f"{segment.start_seconds:.3f}",
            "-i",
            str(segment.source_path),
            "-t",
            f"{segment.duration_seconds:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-reset_timestamps",
            "1",
            str(destination),
        ]
    )
    return destination


@contextmanager
def materialize_thread_segments(
    planned_segments: Sequence[PlannedTwitterSegment],
    *,
    splitter: Callable[[PlannedTwitterSegment, str | Path], Path] = split_video_segment,
    image_normalizer: ImageNormalizer = prepare_image_for_upload,
) -> Iterator[list[PreparedTwitterSegment]]:
    with tempfile.TemporaryDirectory(prefix="sau-twitter-thread-") as temp_dir:
        temp_root = Path(temp_dir)
        prepared: list[PreparedTwitterSegment] = []
        for post_index, segment in enumerate(planned_segments):
            media_paths: tuple[Path, ...] = ()
            if segment.requires_split:
                output_path = temp_root / (
                    f"{segment.source_path.stem}-"
                    f"{segment.source_index:02d}-{segment.segment_index:02d}.mp4"
                )
                upload_path = splitter(segment, output_path)
            elif segment.media_paths:
                media_paths = tuple(
                    image_normalizer(source, temp_root, post_index * 10 + offset)
                    for offset, source in enumerate(segment.media_paths)
                )
                upload_path = media_paths[0]
            else:
                upload_path = segment.source_path

            prepared.append(
                PreparedTwitterSegment(
                    source_path=segment.source_path,
                    upload_path=Path(upload_path).expanduser().resolve(),
                    source_index=segment.source_index,
                    segment_index=segment.segment_index,
                    start_seconds=segment.start_seconds,
                    duration_seconds=segment.duration_seconds,
                    requires_split=segment.requires_split,
                    media_paths=tuple(
                        Path(path).expanduser().resolve() for path in media_paths
                    ),
                    post_index=post_index,
                )
            )
        yield prepared


async def publish_thread_segments(
    prepared_segments: Sequence[PreparedTwitterSegment],
    publish_step: PublishStep,
) -> list[str]:
    post_ids: list[str] = []
    previous_post_id: str | None = None
    for segment in prepared_segments:
        post_id = await publish_step(segment, previous_post_id)
        post_ids.append(post_id)
        previous_post_id = post_id
    return post_ids


def extract_created_post_id(payload) -> str | None:
    create_tweet_payload = _find_create_tweet_payload(payload)
    if create_tweet_payload is not None:
        return _find_post_id_in_payload(create_tweet_payload)
    return _find_post_id_in_payload(payload)


def _find_create_tweet_payload(payload):
    if isinstance(payload, dict):
        for key in ("create_tweet", "createTweet", "CreateTweet"):
            if key in payload:
                return payload[key]
        for value in payload.values():
            found = _find_create_tweet_payload(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_create_tweet_payload(item)
            if found is not None:
                return found
    return None


def _find_post_id_in_payload(payload) -> str | None:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("id"), (str, int)):
            return str(data["id"])
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("id"), (str, int)):
            return str(data["id"])
        tweet_results = payload.get("tweet_results")
        if tweet_results is not None:
            found = _find_post_id_in_payload(tweet_results)
            if found:
                return found

        result = payload.get("result")
        if result is not None:
            found = _find_post_id_in_payload(result)
            if found:
                return found

        for field_name in ("rest_id", "id_str"):
            candidate = payload.get(field_name)
            if isinstance(candidate, str) and candidate:
                return candidate

        for value in payload.values():
            found = _find_post_id_in_payload(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_post_id_in_payload(item)
            if found:
                return found
    return None


async def is_topmost_at_center(locator) -> bool:
    """True when the element would actually receive a click at its centre.

    X mounts its composer inside a modal whose mask animates in over the
    dock. ``is_visible``/``is_enabled`` both go true while that mask is
    still on top, so a click at that moment lands on the mask instead —
    which closes the composer and silently discards the post.
    """
    try:
        return bool(
            await locator.evaluate(
                "(el) => {"
                "  const r = el.getBoundingClientRect();"
                "  if (!r.width || !r.height) return false;"
                "  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
                "  return !!hit && el.contains(hit);"
                "}"
            )
        )
    except Exception:  # noqa: BLE001
        return False
# Media the compose page never needs. x.com serves its home timeline alongside
# the composer, and that timeline autoplays multi-megabyte 4K clips from
# video.twimg.com. Those downloads saturate the session so the composer never
# becomes interactive in time; the observed failures were
# "Page.goto: Timeout 30000ms exceeded" and "TargetClosedError" mid-typing, on
# every X account. Aborting the heavy media leaves the page's own markup and
# API calls intact while removing the bandwidth and decoder pressure.
BLOCKED_MEDIA_RESOURCE_TYPES = frozenset({"media", "font"})
BLOCKED_MEDIA_URL_PATTERNS = (
    "video.twimg.com",
    "pbs.twimg.com/amplify_video_thumb",
)


async def _block_heavy_media(context: BrowserContext) -> BrowserContext:
    """Abort bulky video/font responses for the publish session.

    Best effort: a routing failure must never fail the publish, so it is
    swallowed and the unmodified context is returned.
    """
    async def _route(route: Route) -> None:
        try:
            request = route.request
            if request.resource_type in BLOCKED_MEDIA_RESOURCE_TYPES:
                await route.abort()
                return
            url = request.url
            if any(pattern in url for pattern in BLOCKED_MEDIA_URL_PATTERNS):
                await route.abort()
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            await route.continue_()
        except Exception:  # noqa: BLE001
            pass

    try:
        await context.route("**/*", _route)
    except Exception as exc:  # noqa: BLE001
        twitter_logger.warning(f"X 影音資源封鎖失敗，改用原設定: {exc}")
    return context




async def wait_for_ready_post_button(
    page: Page,
    *,
    timeout_ms: int = TWITTER_POST_BUTTON_READY_TIMEOUT_MS,
    poll_interval_seconds: float = TWITTER_POST_BUTTON_POLL_SECONDS,
    topmost_budget_ms: int = TWITTER_POST_BUTTON_TOPMOST_BUDGET_MS,
):
    started = asyncio.get_running_loop().time()
    deadline = started + (timeout_ms / 1000)
    topmost_deadline = started + (topmost_budget_ms / 1000)
    last_error: Exception | None = None
    fallback = None

    while asyncio.get_running_loop().time() < deadline:
        for selector in TWITTER_POST_BUTTON_SELECTORS:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            # Never take .first: the composer's button and the one behind it
            # on the timeline both match, and only one of them is clickable.
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible() or not await candidate.is_enabled():
                        continue
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
                if fallback is None:
                    fallback = candidate
                if await is_topmost_at_center(candidate):
                    return candidate
        if fallback is not None and asyncio.get_running_loop().time() >= topmost_deadline:
            # A long draft makes X grow the composer until part of it sits
            # under a fixed layer, and the overlay may never lift. Waiting the
            # full budget just lets the browser session expire, so hand the
            # button back and let the caller click it the overlay-proof way.
            return fallback
        await asyncio.sleep(poll_interval_seconds)

    if fallback is not None:
        return fallback
    if last_error is not None:
        raise RuntimeError("Twitter publish button did not become ready in time") from last_error
    raise RuntimeError("Twitter publish button did not become ready in time")


async def click_post_button(page: Page, button, *, before_action=None) -> None:
    """Choose one action up front; a failed action is never a retry permit.

    Dispatch is overlay-proof, but falling back *after* a click timed out can
    duplicate an accepted post. No keyboard fallback after an ambiguous action.
    """
    topmost = await is_topmost_at_center(button)
    if before_action is not None:
        before_action()  # No intervening browser await before the selected action.
    if topmost:
        await button.click(timeout=TWITTER_POST_CLICK_TIMEOUT_MS)
    else:
        await button.dispatch_event("click")


def _browser_post_id(body) -> str | None:
    # Do not mistake a viewer/account ID or an error's ID for a created tweet.
    branch = _find_create_tweet_payload(body)
    if not isinstance(body, dict) or body.get("errors") or not isinstance(branch, dict):
        return None
    results = branch.get("tweet_results")
    result = results.get("result") if isinstance(results, dict) else None
    if not isinstance(result, dict):
        return None
    candidate = result.get("rest_id") or (result.get("legacy") or {}).get("id_str")
    return candidate if isinstance(candidate, str) and re.fullmatch(r"[0-9]+", candidate) else None


def browser_segment_identity(segment: PreparedTwitterSegment, text: str) -> dict:
    media = []
    for path in segment.upload_paths:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        media.append({"sha256": digest.hexdigest(), "bytes": path.stat().st_size})
    entry = {"index": segment.post_index, "source_index": segment.source_index,
             "segment_index": segment.segment_index, "start_seconds": segment.start_seconds,
             "duration_seconds": segment.duration_seconds, "media": media,
             "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    entry["identity"] = hashlib.sha256(json.dumps(entry, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    return entry


class _XBrowserSession:
    """Intercept writes before the network, including alternate UI shortcuts.

    route.fetch makes exactly one upstream request (no retries/redirects), then
    saves selected receipt/rejection evidence before exposing it to the page.
    Unknown mutation endpoints are blocked, not optimistically uninstrumented.
    Service workers MUST be blocked by the context that installs this route.
    """
    def __init__(self, observer):
        self.observer = observer
        self.part = None
        self.action = None
        self.create_seen = False
        self.error = None
        self.post_id = None
        self.media_ids = set()
        self.approved = None
        self.reply_to = None

    def notify(self, event, **values):
        result = self.observer({"event": event, "part": self.part, **values})
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("X delivery_observer must be synchronous and fenced")
        return result

    def check(self):
        if self.error is not None:
            raise self.error

    async def route(self, route):
        request = route.request
        part = self.part  # Capture before awaiting: later UI parts cannot steal this response.
        url = urlsplit(request.url)
        host = (url.hostname or "").lower()
        owned_host = host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com")
        if request.method in {"GET", "HEAD", "OPTIONS"} or not owned_host:
            await route.fallback()
            return
        create = request.method == "POST" and bool(re.fullmatch(r"/i/api/graphql/[^/]+/CreateTweet", url.path))
        media = request.method == "POST" and url.path in {"/i/media/upload.json", "/1.1/media/upload.json"}
        if not create and not media:
            # Deny unknown writes (scheduled posts, legacy statuses/update,
            # drafts, etc.). Read-only browser resources keep existing routing.
            await route.abort()
            return
        name = None
        try:
            self.check()
            if self.part is None or (create and (self.action is None or self.create_seen)):
                raise delivery.DeliveryUnknown("Blocked an unowned/duplicate X post request")
            if create:
                self.create_seen = True
                name = self.action
                request_body = request.post_data_json
                variables = request_body.get("variables") if isinstance(request_body, dict) else None
                if not isinstance(variables, dict) or self.approved is None:
                    raise delivery.DeliveryUnknown("Blocked X create with no exact request identity")
                text = variables.get("tweet_text")
                entities = (variables.get("media") or {}).get("media_entities")
                ids = [entity.get("media_id") for entity in entities] if isinstance(entities, list) else []
                reply = (variables.get("reply") or {}).get("in_reply_to_tweet_id")
                if (not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != self.approved["text_sha256"]
                        or len(ids) != len(self.approved["media"]) or len(set(ids)) != len(ids)
                        or any(media_id not in self.media_ids for media_id in ids)
                        or (reply or None) != self.reply_to):
                    raise delivery.DeliveryUnknown("Blocked X create whose text/media/reply differs from owned part")
                self.notify("create_request", part=part, name=name)
            else:
                if self.action is not None:
                    raise delivery.DeliveryUnknown("Blocked media write after the owned Post action")
                name = self.notify("media_request", part=part)
            try:
                response = await route.fetch(timeout=TWITTER_ATTACHMENT_READY_TIMEOUT_MS if media
                                             else TWITTER_POST_BUTTON_READY_TIMEOUT_MS,
                                             max_redirects=0, max_retries=0)
            except delivery.LeaseLost:
                raise
            except Exception:
                self.notify("unknown", part=part, name=name)
                raise delivery.DeliveryUnknown("X write transport response was lost") from None
            status = response.status
            if type(status) is int and 400 <= status < 500 and status != 408:
                try:
                    rejected_body = await response.json()
                except Exception:
                    rejected_body = {}
                errors = rejected_body.get("errors", []) if isinstance(rejected_body, dict) else []
                codes = [error["code"] for error in errors if isinstance(error, dict)
                         and type(error.get("code")) is int][:8] if isinstance(errors, list) else []
                try:
                    self.notify("rejection", part=part, name=name, httpStatus=status, provider_codes=codes)
                except Exception as exc:
                    self.error = exc
                await route.fulfill(response=response)
                return
            elif type(status) is not int or not 200 <= status < 300:
                self.notify("unknown", part=part, name=name, httpStatus=status)
            else:
                try:
                    body = await response.json()
                except Exception:
                    body = None
                receipt_id = _browser_post_id(body) if create else (
                    body.get("media_id_string") or str(body.get("media_id") or "")
                    if isinstance(body, dict) else None)
                if isinstance(body, dict) and (body.get("errors") or body.get("error")):
                    receipt_id = None
                # APPEND's documented 204 acknowledges bytes for a media ID
                # already supplied by an earlier INIT response, not a tweet.
                linked = None
                if media and status == 204:
                    values = parse_qs(url.query)
                    try:
                        values.update(parse_qs(request.post_data or ""))
                    except (UnicodeError, ValueError):
                        pass
                    # Multipart APPEND carries the same selected fields. Never
                    # persist its binary body, filenames, headers, or URL.
                    raw = getattr(request, "post_data_buffer", None) or b""
                    if isinstance(raw, bytes):
                        for field, value in re.findall(
                            rb'name="(command|media_id)"\r\n\r\n([A-Za-z0-9_]+)\r\n', raw):
                            values.setdefault(field.decode("ascii"), [value.decode("ascii")])
                    if (values.get("command") or [""])[0] == "APPEND":
                        linked = (values.get("media_id") or [""])[0]
                    if linked in self.media_ids:
                        receipt_id = linked
                if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9]+", receipt_id):
                    self.notify("unknown", part=part, name=name, httpStatus=status)
                self.notify("receipt", part=part, name=name, httpStatus=status, id=receipt_id,
                            evidence_source="retained_media_ack" if linked else "provider_response")
                if create:
                    self.post_id = receipt_id
                else:
                    self.media_ids.add(receipt_id)
            await route.fulfill(response=response)
        except Exception as exc:
            # Propagate to the owning coroutine, never log-and-continue a lease
            # failure. Aborting here cannot prove an already fetched write absent.
            if self.error is None:
                self.error = exc
            await route.abort()


class TwitterThreadVideo(BaseVideoUploader):
    def __init__(
        self,
        title: str,
        file_paths: Sequence[str | Path],
        tags: Sequence[str] | None,
        account_file: str | Path,
        publish_date=0,
        *,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        delivery_observer: Callable[[dict], object] | None = None,
    ) -> None:
        if not file_paths:
            raise ValueError("Twitter thread upload requires at least one file")
        self.title = (title or "").strip()
        self.tags = [str(tag).strip().lstrip("#") for tag in (tags or []) if str(tag).strip()]
        self.file_paths = [self._validate_media_file(path) for path in file_paths]
        account_path = Path(account_file).expanduser().resolve()
        if not account_path.exists():
            raise FileNotFoundError(f"Twitter storage_state file not found: {account_path}")
        self.account_file = str(account_path)
        self.publish_date = self.validate_publish_date(publish_date)
        self.debug = debug
        self.headless = headless
        self.delivery_observer = delivery_observer
        self._browser_evidence = None
        self._browser_parts = {}

    @classmethod
    def _validate_media_file(cls, file_path: str | Path) -> Path:
        """Accept stills as well as videos; X posts both from the composer."""
        if is_image_path(file_path):
            return cls.validate_image_file(file_path)
        return cls.validate_video_file(file_path)

    def build_thread_plan(self) -> list[PlannedTwitterSegment]:
        return plan_thread_segments(self.file_paths)

    def _build_post_text(
        self,
        segment: PreparedTwitterSegment,
        *,
        total_segments: int,
        is_root: bool,
    ) -> str:
        base = self.title
        if total_segments > 1:
            base = f"{base} ({segment.post_index + 1}/{total_segments})".strip()
        if is_root and self.tags:
            hashtags = " ".join(f"#{tag}" for tag in self.tags)
            return "\n\n".join(part for part in (base, hashtags) if part)
        return base

    async def _open_reply_composer(self, page: Page, previous_post_id: str) -> None:
        await page.goto(TWITTER_STATUS_URL_TEMPLATE.format(post_id=previous_post_id))
        reply_button = page.locator(TWITTER_REPLY_BUTTON_SELECTOR).first
        try:
            await reply_button.wait_for(state="visible", timeout=15000)
            await reply_button.click()
        except Exception:
            await page.goto(f"{TWITTER_COMPOSE_URL}?in_reply_to={previous_post_id}")

    async def _wait_for_attachments(self, page: Page, *, expected: int) -> None:
        """Wait until X visibly renders every requested media attachment.

        The post button can become enabled before X finishes attaching one
        video/image. Submitting at that point creates a successful text-only
        post, so timeout is a publish failure rather than a best-effort warning.
        """
        if expected <= 0:
            return

        deadline = (
            asyncio.get_running_loop().time() + TWITTER_ATTACHMENT_READY_TIMEOUT_MS / 1000
        )
        previews = page.locator(
            f"{TWITTER_ATTACHMENT_SELECTOR} img, {TWITTER_ATTACHMENT_SELECTOR} video"
        )
        attachment_inputs = page.locator(TWITTER_FILE_INPUT_SELECTOR)
        last_input_count = 0
        last_preview_count = 0
        while asyncio.get_running_loop().time() < deadline:
            try:
                last_input_count = await attachment_inputs.count()
                last_preview_count = await previews.count()
                if last_preview_count >= expected:
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(TWITTER_POST_BUTTON_POLL_SECONDS)
        raise TimeoutError(
            f"X did not render all requested media attachments "
            f"(expected {expected}, previews {last_preview_count}, file inputs {last_input_count}) before the timeout"
        )

    async def _fill_composer(
        self,
        page: Page,
        *,
        segment: PreparedTwitterSegment,
        text: str,
    ) -> None:
        textbox = page.locator(TWITTER_TEXTBOX_SELECTOR).first
        await textbox.wait_for(state="visible", timeout=15000)
        await textbox.click()
        if text:
            await page.keyboard.type(text)

        file_input = page.locator(TWITTER_FILE_INPUT_SELECTOR).first
        media_paths = [str(path) for path in segment.upload_paths]
        await file_input.set_input_files(media_paths)
        await self._wait_for_attachments(page, expected=len(media_paths))

    async def _submit_composer(self, page: Page, button) -> None:
        evidence = getattr(self, "_browser_evidence", None)
        def before_action():
            evidence.check()
            evidence.action = evidence.notify("create_action", reply_to=evidence.reply_to)
        await click_post_button(page, button, before_action=before_action if evidence else None)

    async def _publish_segment(
        self,
        page: Page,
        segment: PreparedTwitterSegment,
        previous_post_id: str | None,
        *,
        total_segments: int,
    ) -> str:
        evidence = getattr(self, "_browser_evidence", None)
        if getattr(self, "delivery_observer", None) is not None and evidence is None:
            raise delivery.DeliveryUnknown("X observer supplied without fail-closed network instrumentation")
        if evidence:
            evidence.check()
            evidence.approved = self._browser_parts[segment.post_index]
            evidence.reply_to = previous_post_id
            evidence.media_ids = set()
            evidence.part = self._browser_parts[segment.post_index]["identity"]
            evidence.action = None
            evidence.create_seen = False
            evidence.post_id = None
            evidence.notify("preparing")
        try:
            if previous_post_id is None:
                await page.goto(TWITTER_COMPOSE_URL)
            else:
                await self._open_reply_composer(page, previous_post_id)
            await self._fill_composer(
                page, segment=segment, text=self._build_post_text(
                    segment, total_segments=total_segments, is_root=previous_post_id is None))
            button = await wait_for_ready_post_button(page)
            if evidence:
                evidence.check()
            async with page.expect_response(
                lambda response: response.request.method == "POST"
                and TWITTER_CREATE_TWEET_RESPONSE_TOKEN in response.url,
                timeout=TWITTER_POST_BUTTON_READY_TIMEOUT_MS,
            ) as response_info:
                await self._submit_composer(page, button)
            response = await response_info.value
            if evidence:
                evidence.check()
                if not evidence.post_id:
                    evidence.notify("unknown", name=evidence.action, httpStatus=response.status)
                return evidence.post_id
            # Default callers still require the worker's conservative outer
            # fence. Capture all statuses; absence of a receipt is never success.
            if not response.ok:
                raise delivery.DeliveryUnknown(f"X browser returned HTTP {response.status}; no owned evidence callback")
            payload = await response.json()
            post_id = _browser_post_id(payload)
            if not post_id:
                raise delivery.DeliveryUnknown("X CreateTweet returned no verified tweet receipt")
            return post_id
        except Exception:
            if evidence:
                evidence.check()
                if evidence.action is not None and evidence.post_id is None:
                    evidence.notify("unknown", name=evidence.action)
            raise

    async def _publish_prepared_segments(
        self,
        prepared_segments: Sequence[PreparedTwitterSegment],
    ) -> list[str]:
        observer = getattr(self, "delivery_observer", None)
        if observer is not None:
            self._browser_evidence = _XBrowserSession(observer)
            parts = [browser_segment_identity(segment, self._build_post_text(
                segment, total_segments=len(prepared_segments), is_root=i == 0))
                for i, segment in enumerate(prepared_segments)]
            self._browser_parts = {part["index"]: part for part in parts}
            self._browser_evidence.notify("plan", parts=parts)
        browser_options = get_browser_options()
        browser_options["headless"] = self.headless

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**browser_options)
            context = None
            try:
                context = await browser.new_context(storage_state=self.account_file,
                    **({"service_workers": "block"} if observer is not None else {}))
                context = await set_init_script(context)
                context = await _block_heavy_media(context)
                if getattr(self, "_browser_evidence", None) is not None:
                    await context.route("**/*", self._browser_evidence.route)
                page = await context.new_page()

                async def publish_step(
                    segment: PreparedTwitterSegment,
                    previous_post_id: str | None,
                ) -> str:
                    return await self._publish_segment(
                        page,
                        segment,
                        previous_post_id,
                        total_segments=len(prepared_segments),
                    )

                result = await publish_thread_segments(prepared_segments, publish_step)
                if getattr(self, "_browser_evidence", None) is not None:
                    self._browser_evidence.check()
                    self._browser_evidence.notify("complete")
                return result
            finally:
                if context is not None:
                    # Refreshing the stored session is a cache update, not part
                    # of the publish. Letting it raise here would replace the
                    # real failure with a confusing "target closed" error.
                    try:
                        await context.storage_state(path=self.account_file)
                    except Exception as exc:  # noqa: BLE001
                        twitter_logger.warning(f"⚠️ 回寫 X cookie 失敗，沿用舊的: {exc}")
                    try:
                        await context.close()
                    except Exception:  # noqa: BLE001
                        pass
                await browser.close()

    async def main(self) -> list[str]:
        planned_segments = self.build_thread_plan()
        with materialize_thread_segments(planned_segments) as prepared_segments:
            return await self._publish_prepared_segments(prepared_segments)
