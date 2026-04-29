"""Shared base for long-form text platforms (Medium, Substack, ...).

These differ from video platforms in that the primary payload is Markdown or
HTML, plus an optional cover image. The base centralises validation so each
platform module only owns the browser-driving code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from uploader.base_video import BaseVideoUploader


class BasePostUploader(BaseVideoUploader):
    SUPPORTED_BODY_EXTENSIONS = {".md", ".markdown", ".html", ".htm", ".txt"}
    SUPPORTED_COVER_EXTENSIONS = BaseVideoUploader.SUPPORTED_IMAGE_EXTENSIONS
    MAX_TITLE_LENGTH = 100
    MAX_TAGS = 5
    MIN_BODY_CHARS = 1
    MIN_SCHEDULE_LEAD_TIME = timedelta(minutes=15)

    @classmethod
    def validate_body_file(cls, file_path: str | Path) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Post body file does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"Post body path is not a file: {path}")
        if path.suffix.lower() not in cls.SUPPORTED_BODY_EXTENSIONS:
            raise ValueError(
                f"Unsupported post body format: {path.suffix}. "
                f"Supported: {', '.join(sorted(cls.SUPPORTED_BODY_EXTENSIONS))}"
            )
        return path

    @classmethod
    def validate_cover_file(cls, file_path: str | Path | None) -> Path | None:
        if file_path in (None, ""):
            return None
        return cls.validate_image_file(file_path)

    @classmethod
    def validate_title(cls, title: str | None) -> str:
        if not title or not str(title).strip():
            raise ValueError("Post title is required")
        title = str(title).strip()
        if len(title) > cls.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Post title is too long ({len(title)} > {cls.MAX_TITLE_LENGTH})"
            )
        return title

    @classmethod
    def validate_tags(cls, tags: list[str] | None) -> list[str]:
        cleaned = [tag.strip().lstrip("#") for tag in (tags or []) if tag and tag.strip()]
        if len(cleaned) > cls.MAX_TAGS:
            raise ValueError(
                f"Too many tags ({len(cleaned)} > {cls.MAX_TAGS}). "
                "Most long-form platforms cap tags to a small number."
            )
        return cleaned

    @classmethod
    def read_body(cls, body_file: Path) -> str:
        text = body_file.read_text(encoding="utf-8")
        if len(text.strip()) < cls.MIN_BODY_CHARS:
            raise ValueError("Post body is empty")
        return text

    @classmethod
    def validate_publish_date(cls, publish_date):  # type: ignore[override]
        # Long-form platforms typically allow scheduling closer to "now" than
        # 2 hours out, so we relax the lead time inherited from BaseVideoUploader.
        if publish_date in (None, 0):
            return 0
        if not isinstance(publish_date, datetime):
            raise TypeError("publish_date must be a datetime or 0")
        now = datetime.now(tz=publish_date.tzinfo) if publish_date.tzinfo else datetime.now()
        if publish_date <= now + cls.MIN_SCHEDULE_LEAD_TIME:
            raise ValueError(
                f"Scheduled publish time must be at least "
                f"{int(cls.MIN_SCHEDULE_LEAD_TIME.total_seconds() // 60)} minutes in the future"
            )
        return publish_date
