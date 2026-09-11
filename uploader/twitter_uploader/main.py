from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Sequence

from patchright.async_api import Page
from patchright.async_api import async_playwright

from utils.conf_defaults import DEBUG_MODE, LOCAL_CHROME_HEADLESS
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
TWITTER_ATTACHMENT_READY_TIMEOUT_MS = 120000
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


async def wait_for_ready_post_button(
    page: Page,
    *,
    timeout_ms: int = TWITTER_POST_BUTTON_READY_TIMEOUT_MS,
    poll_interval_seconds: float = TWITTER_POST_BUTTON_POLL_SECONDS,
):
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    last_error: Exception | None = None

    while asyncio.get_running_loop().time() < deadline:
        for selector in TWITTER_POST_BUTTON_SELECTORS:
            locator = page.locator(selector).first
            try:
                if (
                    await locator.count()
                    and await locator.is_visible()
                    and await locator.is_enabled()
                ):
                    return locator
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        await asyncio.sleep(poll_interval_seconds)

    if last_error is not None:
        raise RuntimeError("Twitter publish button did not become ready in time") from last_error
    raise RuntimeError("Twitter publish button did not become ready in time")


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
        """Best-effort wait for X to render every media preview.

        The post button stays disabled while media uploads, but the preview
        list can still lag behind it. Polling the preview count keeps a
        multi-image post from being submitted half-attached; a timeout just
        falls back to the post-button gate rather than failing the publish.
        """
        if expected <= 1:
            return

        deadline = (
            asyncio.get_running_loop().time() + TWITTER_ATTACHMENT_READY_TIMEOUT_MS / 1000
        )
        previews = page.locator(
            f"{TWITTER_ATTACHMENT_SELECTOR} img, {TWITTER_ATTACHMENT_SELECTOR} video"
        )
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await previews.count() >= expected:
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(TWITTER_POST_BUTTON_POLL_SECONDS)

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

    async def _publish_segment(
        self,
        page: Page,
        segment: PreparedTwitterSegment,
        previous_post_id: str | None,
        *,
        total_segments: int,
    ) -> str:
        if previous_post_id is None:
            await page.goto(TWITTER_COMPOSE_URL)
        else:
            await self._open_reply_composer(page, previous_post_id)

        await self._fill_composer(
            page,
            segment=segment,
            text=self._build_post_text(
                segment,
                total_segments=total_segments,
                is_root=previous_post_id is None,
            ),
        )

        button = await wait_for_ready_post_button(page)
        async with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and TWITTER_CREATE_TWEET_RESPONSE_TOKEN in response.url
                and response.ok
            ),
            timeout=TWITTER_POST_BUTTON_READY_TIMEOUT_MS,
        ) as response_info:
            await button.click()

        response = await response_info.value
        payload = await response.json()
        post_id = extract_created_post_id(payload)
        if not post_id:
            raise RuntimeError("Twitter publish succeeded but no post id was found")
        return post_id

    async def _publish_prepared_segments(
        self,
        prepared_segments: Sequence[PreparedTwitterSegment],
    ) -> list[str]:
        browser_options = get_browser_options()
        browser_options["headless"] = self.headless

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**browser_options)
            context = None
            try:
                context = await browser.new_context(storage_state=self.account_file)
                context = await set_init_script(context)
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

                return await publish_thread_segments(prepared_segments, publish_step)
            finally:
                if context is not None:
                    await context.storage_state(path=self.account_file)
                    await context.close()
                await browser.close()

    async def main(self) -> list[str]:
        planned_segments = self.build_thread_plan()
        with materialize_thread_segments(planned_segments) as prepared_segments:
            return await self._publish_prepared_segments(prepared_segments)
