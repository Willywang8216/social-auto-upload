from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from utils.conf_defaults import BASE_DIR
from myUtils.env_loader import load_repo_env

load_repo_env()
from myUtils import profiles as profile_registry
from uploader.bilibili_uploader.runtime import run_biliup_command
from uploader.douyin_uploader.main import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    DouYinNote,
    DouYinVideo,
    cookie_auth as douyin_cookie_auth,
    douyin_setup,
)
from uploader.ks_uploader.main import (
    KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
    KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
    KSNote,
    KSVideo,
    cookie_auth as kuaishou_cookie_auth,
    ks_setup,
)
from uploader.medium_uploader.main import (
    MEDIUM_PUBLISH_STRATEGY_DRAFT,
    MEDIUM_PUBLISH_STRATEGY_IMMEDIATE,
    MediumPost,
    cookie_auth as medium_cookie_auth,
    medium_setup,
)
from uploader.substack_uploader.main import (
    SUBSTACK_PUBLISH_STRATEGY_DRAFT,
    SUBSTACK_PUBLISH_STRATEGY_IMMEDIATE,
    SUBSTACK_PUBLISH_STRATEGY_SCHEDULED,
    SubstackPost,
    cookie_auth as substack_cookie_auth,
    substack_setup,
)
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuNote,
    XiaoHongShuVideo,
    cookie_auth as xiaohongshu_cookie_auth,
    xiaohongshu_setup,
)
from uploader.tencent_uploader.main import (
    TencentVideo,
    cookie_auth as tencent_cookie_auth,
    weixin_setup,
)

DEFAULT_PROFILE_SLUG = "default"

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(slots=True)
class DouyinVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    thumbnail_landscape_file: Path | None = None
    thumbnail_portrait_file: Path | None = None
    product_link: str = ""
    product_title: str = ""
    publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class TencentVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_landscape_file: Path | None = None
    thumbnail_portrait_file: Path | None = None
    short_title: str | None = None
    category: str | None = None
    is_draft: bool = False
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class DouyinNoteUploadRequest:
    account_name: str
    image_files: list[Path]
    title: str
    note: str
    tags: list[str]
    publish_date: datetime | int
    publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class KuaishouVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    publish_strategy: str = KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class KuaishouNoteUploadRequest:
    account_name: str
    image_files: list[Path]
    title: str
    note: str
    tags: list[str]
    publish_date: datetime | int
    publish_strategy: str = KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class XiaohongshuVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class XiaohongshuNoteUploadRequest:
    account_name: str
    image_files: list[Path]
    title: str
    note: str
    tags: list[str]
    publish_date: datetime | int
    publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class BilibiliVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tid: int
    tags: list[str]
    publish_date: datetime | int


@dataclass(slots=True)
class MediumPostUploadRequest:
    account_name: str
    body_file: Path
    title: str
    subtitle: str
    tags: list[str]
    publish_date: datetime | int
    cover_image: Path | None = None
    publish_strategy: str = MEDIUM_PUBLISH_STRATEGY_IMMEDIATE
    profile: str | None = None
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class SubstackPostUploadRequest:
    account_name: str
    body_file: Path
    title: str
    publication: str
    subtitle: str
    tags: list[str]
    publish_date: datetime | int
    publish_strategy: str = SUBSTACK_PUBLISH_STRATEGY_IMMEDIATE
    profile: str | None = None
    debug: bool = True
    headless: bool = True


def has_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def resolve_runtime_home() -> Path:
    return Path(BASE_DIR)


def resolve_account_file(
    platform: str, account_name: str, profile: str | None = None
) -> Path:
    """Resolve the storage_state file for a (platform, account, profile).

    Backwards-compatible behaviour:
    - When ``profile`` is None, falls back to the legacy flat layout
      ``cookies/{platform}_{account_name}.json`` so existing callers keep
      working without a registry entry.
    - When ``profile`` is given, the file lives at
      ``cookies/{platform}/{profile_slug}/{account_name}.json`` and the
      Profile/Account row is created if it does not yet exist.
    """

    if profile is None:
        account_file = resolve_runtime_home() / "cookies" / f"{platform}_{account_name}.json"
        account_file.parent.mkdir(parents=True, exist_ok=True)
        return account_file

    slug = profile_registry.slugify(profile)
    try:
        prof = profile_registry.get_profile_by_slug(slug)
    except LookupError:
        prof = profile_registry.create_profile(profile)
    account = profile_registry.ensure_account(prof.id, platform, account_name)
    return Path(account.cookie_path)


def parse_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []

    tags: list[str] = []
    for item in raw_tags.split(","):
        cleaned = item.strip().lstrip("#")
        if cleaned:
            tags.append(cleaned)
    return tags


def parse_image_files(raw_files: Iterable[Path]) -> list[Path]:
    return [Path(file) for file in raw_files]


def parse_schedule(raw_schedule: str | None) -> datetime | int:
    if not raw_schedule:
        return 0
    return datetime.strptime(raw_schedule, SCHEDULE_FORMAT)


async def login_douyin_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("douyin", account_name)
    return await douyin_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_douyin_account(account_name: str) -> bool:
    account_file = resolve_account_file("douyin", account_name)
    if not account_file.exists():
        return False
    return await douyin_cookie_auth(str(account_file))


async def login_kuaishou_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("kuaishou", account_name)
    return await ks_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_kuaishou_account(account_name: str) -> bool:
    account_file = resolve_account_file("kuaishou", account_name)
    if not account_file.exists():
        return False
    return await kuaishou_cookie_auth(str(account_file))


async def login_xiaohongshu_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("xiaohongshu", account_name)
    return await xiaohongshu_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_xiaohongshu_account(account_name: str) -> bool:
    account_file = resolve_account_file("xiaohongshu", account_name)
    if not account_file.exists():
        return False
    return await xiaohongshu_cookie_auth(str(account_file))


async def login_tencent_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("tencent", account_name)
    ready = await weixin_setup(str(account_file), handle=True)
    return {
        "success": bool(ready),
        "message": "" if ready else "Tencent login flow did not complete",
        "account_file": str(account_file),
    }


async def check_tencent_account(account_name: str) -> bool:
    account_file = resolve_account_file("tencent", account_name)
    if not account_file.exists():
        return False
    return await tencent_cookie_auth(str(account_file))


async def login_bilibili_account(account_name: str) -> dict:
    account_file = resolve_account_file("bilibili", account_name)
    if not has_interactive_terminal():
        return {
            "success": False,
            "message": (
                "Bilibili login requires a local interactive terminal. "
                f"Please run `sau bilibili login --account {account_name}` yourself in a local terminal. "
                "If the terminal QR code does not render completely, open `./qrcode.png` and scan that image."
            ),
            "account_file": str(account_file),
        }

    result = run_biliup_command(["-u", str(account_file), "login"], interactive=True)
    success = result.returncode == 0
    return {
        "success": success,
        "message": (result.stderr or result.stdout or "").strip() or "Bilibili login completed" if success else (result.stderr or result.stdout or "").strip() or "Bilibili login failed",
        "account_file": str(account_file),
    }


async def check_bilibili_account(account_name: str) -> bool:
    account_file = resolve_account_file("bilibili", account_name)
    if not account_file.exists():
        return False
    result = run_biliup_command(["-u", str(account_file), "renew"])
    return result.returncode == 0


# --------------------------- Medium ---------------------------


async def login_medium_account(account_name: str, *, profile: str | None = None, headless: bool = False) -> dict:
    account_file = resolve_account_file("medium", account_name, profile=profile)
    return await medium_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_medium_account(account_name: str, *, profile: str | None = None) -> bool:
    account_file = resolve_account_file("medium", account_name, profile=profile)
    if not account_file.exists():
        return False
    return await medium_cookie_auth(str(account_file))


async def upload_medium_post(request: MediumPostUploadRequest) -> Path:
    account_file = resolve_account_file("medium", request.account_name, profile=request.profile)
    is_ready = await medium_setup(str(account_file), handle=False)
    if not is_ready:
        scope = f"--profile {request.profile} " if request.profile else ""
        raise RuntimeError(
            f"Medium cookie missing or expired: {account_file}. "
            f"Run `sau medium login {scope}--account {request.account_name}` first."
        )

    app = MediumPost(
        title=request.title,
        body_file=request.body_file,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        subtitle=request.subtitle,
        cover_image=request.cover_image,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.publish()
    return account_file


# --------------------------- Substack ---------------------------


async def login_substack_account(account_name: str, *, profile: str | None = None, headless: bool = False) -> dict:
    account_file = resolve_account_file("substack", account_name, profile=profile)
    return await substack_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_substack_account(account_name: str, *, profile: str | None = None) -> bool:
    account_file = resolve_account_file("substack", account_name, profile=profile)
    if not account_file.exists():
        return False
    return await substack_cookie_auth(str(account_file))


async def upload_substack_post(request: SubstackPostUploadRequest) -> Path:
    account_file = resolve_account_file("substack", request.account_name, profile=request.profile)
    is_ready = await substack_setup(str(account_file), handle=False)
    if not is_ready:
        scope = f"--profile {request.profile} " if request.profile else ""
        raise RuntimeError(
            f"Substack cookie missing or expired: {account_file}. "
            f"Run `sau substack login {scope}--account {request.account_name}` first."
        )

    app = SubstackPost(
        title=request.title,
        body_file=request.body_file,
        publication=request.publication,
        publish_date=request.publish_date,
        account_file=str(account_file),
        subtitle=request.subtitle,
        tags=request.tags,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.publish()
    return account_file


async def upload_video(request: DouyinVideoUploadRequest) -> Path:
    account_file = resolve_account_file("douyin", request.account_name)
    is_ready = await douyin_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Douyin cookie is missing or expired: {account_file}. Run `sau douyin login --account {request.account_name}` first."
        )

    app = DouYinVideo(
        request.title,
        str(request.video_file),
        request.tags,
        request.publish_date,
        str(account_file),
        desc=request.description,
        thumbnail_landscape_path=str(request.thumbnail_landscape_file)
        if request.thumbnail_landscape_file
        else None,
        thumbnail_portrait_path=str(request.thumbnail_portrait_file or request.thumbnail_file)
        if (request.thumbnail_portrait_file or request.thumbnail_file)
        else None,
        productLink=request.product_link,
        productTitle=request.product_title,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.douyin_upload_video()
    return account_file


async def upload_note(request: DouyinNoteUploadRequest) -> Path:
    account_file = resolve_account_file("douyin", request.account_name)
    is_ready = await douyin_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Douyin cookie is missing or expired: {account_file}. Run `sau douyin login --account {request.account_name}` first."
        )

    app = DouYinNote(
        image_paths=[str(path) for path in request.image_files],
        title=request.title,
        note=request.note,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.douyin_upload_note()
    return account_file


async def upload_kuaishou_video(request: KuaishouVideoUploadRequest) -> Path:
    account_file = resolve_account_file("kuaishou", request.account_name)
    is_ready = await ks_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Kuaishou cookie is missing or expired: {account_file}. Run `sau kuaishou login --account {request.account_name}` first."
        )

    app = KSVideo(
        title=request.title,
        file_path=str(request.video_file),
        desc=request.description,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def upload_kuaishou_note(request: KuaishouNoteUploadRequest) -> Path:
    account_file = resolve_account_file("kuaishou", request.account_name)
    is_ready = await ks_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Kuaishou cookie is missing or expired: {account_file}. Run `sau kuaishou login --account {request.account_name}` first."
        )

    app = KSNote(
        image_paths=[str(path) for path in request.image_files],
        title=request.title,
        note=request.note,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def upload_xiaohongshu_video(request: XiaohongshuVideoUploadRequest) -> Path:
    account_file = resolve_account_file("xiaohongshu", request.account_name)
    is_ready = await xiaohongshu_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Xiaohongshu cookie is missing or expired: {account_file}. Run `sau xiaohongshu login --account {request.account_name}` first."
        )

    app = XiaoHongShuVideo(
        title=request.title,
        file_path=str(request.video_file),
        desc=request.description,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def upload_tencent_video(request: TencentVideoUploadRequest) -> Path:
    account_file = resolve_account_file("tencent", request.account_name)
    is_ready = await weixin_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Tencent cookie is missing or expired: {account_file}. Run `sau tencent login --account {request.account_name}` first."
        )

    # NOTE: the current fork's TencentVideo uploader does not yet support custom
    # cover images. The --thumbnail-landscape/--thumbnail-portrait flags are
    # accepted (and carried on the request) for parity with the other platforms
    # and for forward-compatibility; warn rather than silently ignore them.
    if request.thumbnail_landscape_file or request.thumbnail_portrait_file:
        print(
            "warning: tencent uploader does not support custom cover images yet; "
            "ignoring --thumbnail-landscape/--thumbnail-portrait",
            file=sys.stderr,
        )

    app = TencentVideo(
        request.title,
        str(request.video_file),
        request.tags,
        request.publish_date,
        str(account_file),
        category=request.category,
        is_draft=request.is_draft,
    )
    await app.main()
    return account_file


async def upload_xiaohongshu_note(request: XiaohongshuNoteUploadRequest) -> Path:
    account_file = resolve_account_file("xiaohongshu", request.account_name)
    is_ready = await xiaohongshu_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Xiaohongshu cookie is missing or expired: {account_file}. Run `sau xiaohongshu login --account {request.account_name}` first."
        )

    app = XiaoHongShuNote(
        image_paths=[str(path) for path in request.image_files],
        title=request.title,
        desc=request.note,
        note=request.note,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def upload_bilibili_video(request: BilibiliVideoUploadRequest) -> Path:
    account_file = resolve_account_file("bilibili", request.account_name)
    if not account_file.exists():
        raise RuntimeError(
            f"Bilibili account file is missing: {account_file}. Run `sau bilibili login --account {request.account_name}` first."
        )

    arguments = [
        "-u",
        str(account_file),
        "upload",
        str(request.video_file),
        "--title",
        request.title,
        "--desc",
        request.description,
        "--tid",
        str(request.tid),
    ]
    if request.tags:
        arguments.extend(["--tag", ",".join(request.tags)])
    if isinstance(request.publish_date, datetime):
        arguments.extend(["--dtime", str(int(request.publish_date.timestamp()))])

    result = run_biliup_command(arguments)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "Bilibili upload failed")
    return account_file


def existing_file_path(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"File not found: {value}")
    return path


def existing_post_body(value: str) -> Path:
    path = existing_file_path(value)
    if path.suffix.lower() not in {".md", ".markdown", ".html", ".htm", ".txt"}:
        raise argparse.ArgumentTypeError(
            f"Unsupported post body format: {path.suffix}. Use .md, .markdown, .html, .htm or .txt"
        )
    return path


def schedule_value(value: str):
    try:
        return parse_schedule(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid schedule '{value}'. Expected format: {SCHEDULE_FORMAT}"
        ) from exc


def add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument("--headed", dest="headless", action="store_false", help="Run with browser UI")
    headless_group.add_argument("--headless", dest="headless", action="store_true", help="Run in headless mode")
    parser.set_defaults(headless=True)


def build_parser() -> argparse.ArgumentParser:
    schedule_help = SCHEDULE_FORMAT.replace("%", "%%")
    parser = argparse.ArgumentParser(
        prog="sau",
        description="CLI for social-auto-upload.",
    )
    platform_parsers = parser.add_subparsers(dest="platform", required=True)

    douyin_parser = platform_parsers.add_parser("douyin", help="Douyin operations")
    douyin_actions = douyin_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = douyin_actions.add_parser(action_name, help=f"Douyin {action_name}")
        action_parser.add_argument("--account", required=True, help="Douyin user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    upload_video_parser = douyin_actions.add_parser("upload-video", help="Upload one video to Douyin")
    upload_video_parser.add_argument("--account", required=True, help="Douyin user-defined account_name")
    upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    upload_video_parser.add_argument("--title", required=True, help="Video title")
    upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    upload_video_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional thumbnail path")
    upload_video_parser.add_argument("--thumbnail-landscape", type=existing_file_path, help="Optional 4:3 landscape thumbnail path")
    upload_video_parser.add_argument("--thumbnail-portrait", type=existing_file_path, help="Optional 3:4 portrait thumbnail path")
    upload_video_parser.add_argument("--product-link", default="", help="Optional product link")
    upload_video_parser.add_argument("--product-title", default="", help="Optional product title")
    add_runtime_flags(upload_video_parser)

    upload_note_parser = douyin_actions.add_parser("upload-note", help="Upload one note to Douyin")
    upload_note_parser.add_argument("--account", required=True, help="Douyin user-defined account_name")
    upload_note_parser.add_argument("--images", required=True, nargs="+", type=existing_file_path, help="Image file paths")
    upload_note_parser.add_argument("--title", required=True, help="Note title")
    upload_note_parser.add_argument("--note", default="", help="Optional note content")
    upload_note_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    upload_note_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    add_runtime_flags(upload_note_parser)

    kuaishou_parser = platform_parsers.add_parser("kuaishou", help="Kuaishou operations")
    kuaishou_actions = kuaishou_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = kuaishou_actions.add_parser(action_name, help=f"Kuaishou {action_name}")
        action_parser.add_argument("--account", required=True, help="Kuaishou user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    kuaishou_upload_video_parser = kuaishou_actions.add_parser("upload-video", help="Upload one video to Kuaishou")
    kuaishou_upload_video_parser.add_argument("--account", required=True, help="Kuaishou user-defined account_name")
    kuaishou_upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    kuaishou_upload_video_parser.add_argument("--title", required=True, help="Video title")
    kuaishou_upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    kuaishou_upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    kuaishou_upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    kuaishou_upload_video_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional thumbnail path")
    add_runtime_flags(kuaishou_upload_video_parser)

    kuaishou_upload_note_parser = kuaishou_actions.add_parser("upload-note", help="Upload one note to Kuaishou")
    kuaishou_upload_note_parser.add_argument("--account", required=True, help="Kuaishou user-defined account_name")
    kuaishou_upload_note_parser.add_argument("--images", required=True, nargs="+", type=existing_file_path, help="Image file paths")
    kuaishou_upload_note_parser.add_argument("--title", required=True, help="Note title")
    kuaishou_upload_note_parser.add_argument("--note", default="", help="Optional note content")
    kuaishou_upload_note_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    kuaishou_upload_note_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    add_runtime_flags(kuaishou_upload_note_parser)

    xiaohongshu_parser = platform_parsers.add_parser("xiaohongshu", help="Xiaohongshu operations")
    xiaohongshu_actions = xiaohongshu_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = xiaohongshu_actions.add_parser(action_name, help=f"Xiaohongshu {action_name}")
        action_parser.add_argument("--account", required=True, help="Xiaohongshu user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    xiaohongshu_upload_video_parser = xiaohongshu_actions.add_parser("upload-video", help="Upload one video to Xiaohongshu")
    xiaohongshu_upload_video_parser.add_argument("--account", required=True, help="Xiaohongshu user-defined account_name")
    xiaohongshu_upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    xiaohongshu_upload_video_parser.add_argument("--title", required=True, help="Video title")
    xiaohongshu_upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    xiaohongshu_upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    xiaohongshu_upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    xiaohongshu_upload_video_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional thumbnail path")
    add_runtime_flags(xiaohongshu_upload_video_parser)

    xiaohongshu_upload_note_parser = xiaohongshu_actions.add_parser("upload-note", help="Upload one note to Xiaohongshu")
    xiaohongshu_upload_note_parser.add_argument("--account", required=True, help="Xiaohongshu user-defined account_name")
    xiaohongshu_upload_note_parser.add_argument("--images", required=True, nargs="+", type=existing_file_path, help="Image file paths")
    xiaohongshu_upload_note_parser.add_argument("--title", required=True, help="Note title")
    xiaohongshu_upload_note_parser.add_argument("--note", default="", help="Optional note content")
    xiaohongshu_upload_note_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    xiaohongshu_upload_note_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    add_runtime_flags(xiaohongshu_upload_note_parser)

    tencent_parser = platform_parsers.add_parser("tencent", help="Tencent/WeChat Channels operations")
    tencent_actions = tencent_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = tencent_actions.add_parser(action_name, help=f"Tencent/WeChat Channels {action_name}")
        action_parser.add_argument("--account", required=True, help="Tencent user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    tencent_upload_video_parser = tencent_actions.add_parser("upload-video", help="Upload one video to WeChat Channels")
    tencent_upload_video_parser.add_argument("--account", required=True, help="Tencent user-defined account_name")
    tencent_upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    tencent_upload_video_parser.add_argument("--title", required=True, help="Video title")
    tencent_upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    tencent_upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    tencent_upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    tencent_upload_video_parser.add_argument("--thumbnail-landscape", type=existing_file_path, help="Optional 4:3 landscape thumbnail path")
    tencent_upload_video_parser.add_argument("--thumbnail-portrait", type=existing_file_path, help="Optional 3:4 portrait thumbnail path")
    tencent_upload_video_parser.add_argument("--short-title", default=None, help="Optional short title")
    tencent_upload_video_parser.add_argument("--category", default=None, help="Optional category")
    tencent_upload_video_parser.add_argument("--draft", action="store_true", help="Save as draft instead of publishing")
    add_runtime_flags(tencent_upload_video_parser)

    bilibili_parser = platform_parsers.add_parser("bilibili", help="Bilibili operations")
    bilibili_actions = bilibili_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = bilibili_actions.add_parser(action_name, help=f"Bilibili {action_name}")
        action_parser.add_argument("--account", required=True, help="Bilibili user-defined account_name")

    bilibili_upload_video_parser = bilibili_actions.add_parser("upload-video", help="Upload one video to Bilibili")
    bilibili_upload_video_parser.add_argument("--account", required=True, help="Bilibili user-defined account_name")
    bilibili_upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    bilibili_upload_video_parser.add_argument("--title", required=True, help="Video title")
    bilibili_upload_video_parser.add_argument("--desc", required=True, help="Video description")
    bilibili_upload_video_parser.add_argument("--tid", required=True, type=int, help="Bilibili category id")
    bilibili_upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    bilibili_upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")

    # ----- Medium -----
    medium_parser = platform_parsers.add_parser("medium", help="Medium operations")
    medium_actions = medium_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = medium_actions.add_parser(action_name, help=f"Medium {action_name}")
        action_parser.add_argument("--account", required=True, help="Medium user-defined account_name")
        action_parser.add_argument("--profile", default=None, help="Optional profile slug (groups multiple accounts)")
        if action_name == "login":
            add_runtime_flags(action_parser)

    medium_upload_post_parser = medium_actions.add_parser("upload-post", help="Publish a post to Medium")
    medium_upload_post_parser.add_argument("--account", required=True, help="Medium user-defined account_name")
    medium_upload_post_parser.add_argument("--profile", default=None, help="Optional profile slug (groups multiple accounts)")
    medium_upload_post_parser.add_argument("--file", required=True, type=existing_post_body, help="Post body file (.md/.html/.txt)")
    medium_upload_post_parser.add_argument("--title", required=True, help="Post title")
    medium_upload_post_parser.add_argument("--subtitle", default="", help="Optional subtitle")
    medium_upload_post_parser.add_argument("--tags", default="", help="Comma-separated tags (max 5)")
    medium_upload_post_parser.add_argument("--cover", type=existing_file_path, help="Optional cover image")
    medium_upload_post_parser.add_argument("--draft", action="store_true", help="Save as draft instead of publishing")
    add_runtime_flags(medium_upload_post_parser)

    # ----- Substack -----
    substack_parser = platform_parsers.add_parser("substack", help="Substack operations")
    substack_actions = substack_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = substack_actions.add_parser(action_name, help=f"Substack {action_name}")
        action_parser.add_argument("--account", required=True, help="Substack user-defined account_name")
        action_parser.add_argument("--profile", default=None, help="Optional profile slug (groups multiple accounts)")
        if action_name == "login":
            add_runtime_flags(action_parser)

    substack_upload_post_parser = substack_actions.add_parser("upload-post", help="Publish a post to Substack")
    substack_upload_post_parser.add_argument("--account", required=True, help="Substack user-defined account_name")
    substack_upload_post_parser.add_argument("--profile", default=None, help="Optional profile slug (groups multiple accounts)")
    substack_upload_post_parser.add_argument("--publication", required=True, help="Substack publication subdomain, e.g. 'acme' or 'https://acme.substack.com'")
    substack_upload_post_parser.add_argument("--file", required=True, type=existing_post_body, help="Post body file (.md/.html/.txt)")
    substack_upload_post_parser.add_argument("--title", required=True, help="Post title")
    substack_upload_post_parser.add_argument("--subtitle", default="", help="Optional subtitle")
    substack_upload_post_parser.add_argument("--tags", default="", help="Comma-separated tags (max 5)")
    substack_upload_post_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    substack_upload_post_parser.add_argument("--draft", action="store_true", help="Save as draft instead of publishing")
    add_runtime_flags(substack_upload_post_parser)

    # ----- Profile management -----
    profile_parser = platform_parsers.add_parser("profile", help="Manage profiles (groups of accounts)")
    profile_actions = profile_parser.add_subparsers(dest="action", required=True)

    profile_create = profile_actions.add_parser("create", help="Create a profile")
    profile_create.add_argument("--name", required=True, help="Human-readable profile name")
    profile_create.add_argument("--description", default="", help="Optional description")

    profile_actions.add_parser("list", help="List profiles")

    profile_show = profile_actions.add_parser("show", help="Show a profile and its accounts")
    profile_show.add_argument("--profile", required=True, help="Profile slug")

    profile_delete = profile_actions.add_parser("delete", help="Delete a profile and all its accounts")
    profile_delete.add_argument("--profile", required=True, help="Profile slug")

    # ----- Cookie encryption helpers -----
    cookies_parser = platform_parsers.add_parser(
        "cookies",
        help="Cookie storage helpers (encrypt-at-rest migration etc.)",
    )
    cookies_actions = cookies_parser.add_subparsers(dest="action", required=True)
    cookies_actions.add_parser(
        "status",
        help="Report whether at-rest encryption is enabled and how many files are encrypted",
    )
    encrypt_action = cookies_actions.add_parser(
        "encrypt",
        help=(
            "Encrypt every plaintext cookie file under cookiesFile/ and "
            "cookies/. Idempotent. Requires SAU_COOKIE_ENCRYPTION_KEY to be set."
        ),
    )
    encrypt_action.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which files would be encrypted without touching disk.",
    )

    # ----- Skill (MCP) installer -----
    skill_parser = platform_parsers.add_parser(
        "skill",
        help="Register sau-mcp with AI agent clients (Claude Desktop, Cursor, Claude Code).",
    )
    skill_actions = skill_parser.add_subparsers(dest="action", required=True)
    skill_install = skill_actions.add_parser(
        "install",
        help=(
            "Detect installed MCP-aware clients and patch their config so "
            "they expose the sau-mcp server under the `sau` MCP name."
        ),
    )
    skill_install.add_argument(
        "--client",
        choices=("claude-desktop", "cursor", "claude-code", "all"),
        default="all",
        help="Which client to install into (default: all detected).",
    )
    skill_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without touching any config file.",
    )
    skill_remove = skill_actions.add_parser(
        "remove",
        help="Remove the sau-mcp entry from installed client configs.",
    )
    skill_remove.add_argument(
        "--client",
        choices=("claude-desktop", "cursor", "claude-code", "all"),
        default="all",
        help="Which client to remove from (default: all detected).",
    )
    skill_list = skill_actions.add_parser(
        "list",
        help="Print detected client configs and whether `sau` is registered.",
    )
    skill_list.add_argument(
        "--client",
        choices=("claude-desktop", "cursor", "claude-code", "all"),
        default="all",
    )

    return parser


async def dispatch(args: argparse.Namespace) -> int:
    if args.platform == "douyin":
        if args.action == "login":
            result = await login_douyin_account(args.account, headless=args.headless)
            if not isinstance(result, dict) or not result.get("success", False):
                raise RuntimeError(
                    f"Douyin login failed: {result if not isinstance(result, dict) else result.get('message', 'Unknown error')}"
                )
            print(f"Douyin login flow completed: {result.get('account_file')}")
            return 0

        if args.action == "check":
            is_valid = await check_douyin_account(args.account)
            print("valid" if is_valid else "invalid")
            return 0 if is_valid else 1

        publish_strategy = DOUYIN_PUBLISH_STRATEGY_SCHEDULED if args.schedule else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE

        if args.action == "upload-video":
            request = DouyinVideoUploadRequest(
                account_name=args.account,
                video_file=args.file,
                title=args.title,
                description=args.desc,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                thumbnail_file=args.thumbnail,
                thumbnail_landscape_file=args.thumbnail_landscape,
                thumbnail_portrait_file=args.thumbnail_portrait,
                product_link=args.product_link,
                product_title=args.product_title,
                publish_strategy=publish_strategy,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_video(request)
            print(f"Douyin video upload submitted: {request.video_file}")
            return 0

        if args.action == "upload-note":
            request = DouyinNoteUploadRequest(
                account_name=args.account,
                image_files=parse_image_files(args.images),
                title=args.title,
                note=args.note,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                publish_strategy=publish_strategy,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_note(request)
            print(f"Douyin note upload submitted: {len(request.image_files)} images")
            return 0

        raise RuntimeError(f"Unsupported Douyin action: {args.action}")

    if args.platform == "tencent":
        if args.action == "login":
            result = await login_tencent_account(args.account, headless=args.headless)
            if not isinstance(result, dict) or not result.get("success", False):
                raise RuntimeError(
                    f"Tencent login failed: {result if not isinstance(result, dict) else result.get('message', 'Unknown error')}"
                )
            print(f"Tencent login flow completed: {result.get('account_file')}")
            return 0

        if args.action == "check":
            is_valid = await check_tencent_account(args.account)
            print("valid" if is_valid else "invalid")
            return 0 if is_valid else 1

        if args.action == "upload-video":
            request = TencentVideoUploadRequest(
                account_name=args.account,
                video_file=args.file,
                title=args.title,
                description=args.desc,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                thumbnail_landscape_file=args.thumbnail_landscape,
                thumbnail_portrait_file=args.thumbnail_portrait,
                short_title=args.short_title,
                category=args.category,
                is_draft=args.draft,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_tencent_video(request)
            print(f"Tencent video upload submitted: {request.video_file}")
            return 0

        raise RuntimeError(f"Unsupported Tencent action: {args.action}")

    if args.platform == "kuaishou":
        if args.action == "login":
            result = await login_kuaishou_account(args.account, headless=args.headless)
            if not isinstance(result, dict) or not result.get("success", False):
                raise RuntimeError(
                    f"Kuaishou login failed: {result if not isinstance(result, dict) else result.get('message', 'Unknown error')}"
                )
            print(f"Kuaishou login flow completed: {result.get('account_file')}")
            return 0

        if args.action == "check":
            is_valid = await check_kuaishou_account(args.account)
            print("valid" if is_valid else "invalid")
            return 0 if is_valid else 1

        publish_strategy = KUAISHOU_PUBLISH_STRATEGY_SCHEDULED if args.schedule else KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE

        if args.action == "upload-video":
            request = KuaishouVideoUploadRequest(
                account_name=args.account,
                video_file=args.file,
                title=args.title,
                description=args.desc,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                thumbnail_file=args.thumbnail,
                publish_strategy=publish_strategy,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_kuaishou_video(request)
            print(f"Kuaishou video upload submitted: {request.video_file}")
            return 0

        if args.action == "upload-note":
            request = KuaishouNoteUploadRequest(
                account_name=args.account,
                image_files=parse_image_files(args.images),
                title=args.title,
                note=args.note,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                publish_strategy=publish_strategy,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_kuaishou_note(request)
            print(f"Kuaishou note upload submitted: {len(request.image_files)} images")
            return 0

        raise RuntimeError(f"Unsupported Kuaishou action: {args.action}")

    if args.platform == "xiaohongshu":
        if args.action == "login":
            result = await login_xiaohongshu_account(args.account, headless=args.headless)
            if not isinstance(result, dict) or not result.get("success", False):
                raise RuntimeError(
                    f"Xiaohongshu login failed: {result if not isinstance(result, dict) else result.get('message', 'Unknown error')}"
                )
            print(f"Xiaohongshu login flow completed: {result.get('account_file')}")
            return 0

        if args.action == "check":
            is_valid = await check_xiaohongshu_account(args.account)
            print("valid" if is_valid else "invalid")
            return 0 if is_valid else 1

        publish_strategy = (
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED if args.schedule else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
        )

        if args.action == "upload-video":
            request = XiaohongshuVideoUploadRequest(
                account_name=args.account,
                video_file=args.file,
                title=args.title,
                description=args.desc,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                thumbnail_file=args.thumbnail,
                publish_strategy=publish_strategy,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_xiaohongshu_video(request)
            print(f"Xiaohongshu video upload submitted: {request.video_file}")
            return 0

        if args.action == "upload-note":
            request = XiaohongshuNoteUploadRequest(
                account_name=args.account,
                image_files=parse_image_files(args.images),
                title=args.title,
                note=args.note,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                publish_strategy=publish_strategy,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_xiaohongshu_note(request)
            print(f"Xiaohongshu note upload submitted: {len(request.image_files)} images")
            return 0

        raise RuntimeError(f"Unsupported Xiaohongshu action: {args.action}")

    if args.platform == "bilibili":
        if args.action == "login":
            result = await login_bilibili_account(args.account)
            if not isinstance(result, dict) or not result.get("success", False):
                raise RuntimeError(
                    f"Bilibili login failed: {result if not isinstance(result, dict) else result.get('message', 'Unknown error')}"
                )
            print(f"Bilibili login flow completed: {result.get('account_file')}")
            return 0

        if args.action == "check":
            is_valid = await check_bilibili_account(args.account)
            print("valid" if is_valid else "invalid")
            return 0 if is_valid else 1

        if args.action == "upload-video":
            request = BilibiliVideoUploadRequest(
                account_name=args.account,
                video_file=args.file,
                title=args.title,
                description=args.desc,
                tid=args.tid,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
            )
            await upload_bilibili_video(request)
            print(f"Bilibili video upload submitted: {request.video_file}")
            return 0

        raise RuntimeError(f"Unsupported Bilibili action: {args.action}")

    if args.platform == "medium":
        if args.action == "login":
            result = await login_medium_account(args.account, profile=args.profile, headless=args.headless)
            if not isinstance(result, dict) or not result.get("success", False):
                raise RuntimeError(
                    f"Medium login failed: {result if not isinstance(result, dict) else result.get('message', 'Unknown error')}"
                )
            print(f"Medium login flow completed: {result.get('account_file')}")
            return 0

        if args.action == "check":
            is_valid = await check_medium_account(args.account, profile=args.profile)
            print("valid" if is_valid else "invalid")
            return 0 if is_valid else 1

        if args.action == "upload-post":
            strategy = MEDIUM_PUBLISH_STRATEGY_DRAFT if args.draft else MEDIUM_PUBLISH_STRATEGY_IMMEDIATE
            request = MediumPostUploadRequest(
                account_name=args.account,
                body_file=args.file,
                title=args.title,
                subtitle=args.subtitle,
                tags=parse_tags(args.tags),
                publish_date=0,
                cover_image=args.cover,
                publish_strategy=strategy,
                profile=args.profile,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_medium_post(request)
            print(f"Medium post submitted: {request.body_file}")
            return 0

        raise RuntimeError(f"Unsupported Medium action: {args.action}")

    if args.platform == "substack":
        if args.action == "login":
            result = await login_substack_account(args.account, profile=args.profile, headless=args.headless)
            if not isinstance(result, dict) or not result.get("success", False):
                raise RuntimeError(
                    f"Substack login failed: {result if not isinstance(result, dict) else result.get('message', 'Unknown error')}"
                )
            print(f"Substack login flow completed: {result.get('account_file')}")
            return 0

        if args.action == "check":
            is_valid = await check_substack_account(args.account, profile=args.profile)
            print("valid" if is_valid else "invalid")
            return 0 if is_valid else 1

        if args.action == "upload-post":
            if args.draft:
                strategy = SUBSTACK_PUBLISH_STRATEGY_DRAFT
            elif args.schedule:
                strategy = SUBSTACK_PUBLISH_STRATEGY_SCHEDULED
            else:
                strategy = SUBSTACK_PUBLISH_STRATEGY_IMMEDIATE
            request = SubstackPostUploadRequest(
                account_name=args.account,
                body_file=args.file,
                title=args.title,
                publication=args.publication,
                subtitle=args.subtitle,
                tags=parse_tags(args.tags),
                publish_date=args.schedule or 0,
                publish_strategy=strategy,
                profile=args.profile,
                debug=args.debug,
                headless=args.headless,
            )
            await upload_substack_post(request)
            print(f"Substack post submitted: {request.body_file}")
            return 0

        raise RuntimeError(f"Unsupported Substack action: {args.action}")

    if args.platform == "profile":
        if args.action == "create":
            profile = profile_registry.create_profile(args.name, description=args.description)
            print(f"Created profile {profile.slug} (id={profile.id})")
            return 0
        if args.action == "list":
            for prof in profile_registry.list_profiles():
                print(f"{prof.slug}\t{prof.name}\t{prof.description}")
            return 0
        if args.action == "show":
            prof = profile_registry.get_profile_by_slug(args.profile)
            print(f"Profile: {prof.name} (slug={prof.slug}, id={prof.id})")
            for account in profile_registry.list_accounts(profile_id=prof.id):
                print(
                    f"  {account.platform}\t{account.account_name}\tstatus={account.status}\t"
                    f"path={account.cookie_path}"
                )
            return 0
        if args.action == "delete":
            prof = profile_registry.get_profile_by_slug(args.profile)
            profile_registry.delete_profile(prof.id)
            print(f"Deleted profile {prof.slug}")
            return 0

        raise RuntimeError(f"Unsupported profile action: {args.action}")

    if args.platform == "cookies":
        from myUtils import cookie_storage

        if args.action == "status":
            enabled = cookie_storage.is_encryption_enabled()
            paths = _all_cookie_files()
            encrypted = sum(
                1 for path in paths
                if path.exists()
                and cookie_storage.looks_encrypted(path.read_bytes()[:8])
            )
            mode = "encrypted" if enabled else "open (set SAU_COOKIE_ENCRYPTION_KEY)"
            print(f"Cookie storage: {mode}")
            print(f"  files on disk:    {len(paths)}")
            print(f"  already encrypted: {encrypted}")
            print(f"  plaintext:         {len(paths) - encrypted}")
            return 0

        if args.action == "encrypt":
            if not cookie_storage.is_encryption_enabled():
                print(
                    "SAU_COOKIE_ENCRYPTION_KEY is not set; refusing to run.",
                    file=sys.stderr,
                )
                print(
                    "Generate a key via: "
                    "python -c \"import base64, secrets; "
                    "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"",
                    file=sys.stderr,
                )
                return 2

            paths = _all_cookie_files()
            if args.dry_run:
                for path in paths:
                    if not path.exists():
                        continue
                    head = path.read_bytes()[:8]
                    state = "already-encrypted" if cookie_storage.looks_encrypted(head) else "would-encrypt"
                    print(f"{state}\t{path}")
                return 0

            outcomes = cookie_storage.encrypt_existing_files(paths)
            counts: dict[str, int] = {}
            for outcome in outcomes.values():
                counts[outcome] = counts.get(outcome, 0) + 1
            for outcome, count in sorted(counts.items()):
                print(f"{outcome}: {count}")
            return 0

        raise RuntimeError(f"Unsupported cookies action: {args.action}")

    if args.platform == "skill":
        if args.action == "list":
            return _skill_list(getattr(args, "client", "all"))
        if args.action == "install":
            return _skill_install(
                client=getattr(args, "client", "all"),
                dry_run=args.dry_run,
            )
        if args.action == "remove":
            return _skill_remove(client=getattr(args, "client", "all"))
        raise RuntimeError(f"Unsupported skill action: {args.action}")

    raise RuntimeError(f"Unsupported platform: {args.platform}")


def _all_cookie_files() -> list[Path]:
    """Discover every cookie file the project knows about.

    Walks both the legacy Flask layout (``cookiesFile/*.json``) and the
    profile-aware layout (``cookies/{platform}/{profile}/{name}.json``).
    """

    home = resolve_runtime_home()
    candidates: list[Path] = []
    legacy = home / "cookiesFile"
    if legacy.exists():
        candidates.extend(p for p in legacy.glob("*.json") if p.is_file())
    new_root = home / "cookies"
    if new_root.exists():
        candidates.extend(p for p in new_root.rglob("*.json") if p.is_file())
    return sorted(set(candidates))


# ---------------------------------------------------------------------------
# Skill / MCP installer
# ---------------------------------------------------------------------------

_SAU_MCP_NAME = "sau"


@dataclass(slots=True)
class SkillTarget:
    """One MCP-aware client we know how to register sau-mcp with."""

    client: str
    config_path: Path
    servers_key: str  # key under which `mcpServers` lives (or empty for root)


def _resolve_sau_mcp_binary() -> Path:
    """Return the absolute path to the sau-mcp binary, preferring the venv.

    We prefer the venv copy (``sys.prefix / bin / sau-mcp``) so callers do
    not pick up a stale system-wide install. Falls back to ``shutil.which``
    and finally to a synthesized path under ``BASE_DIR/.venv/bin/sau-mcp``
    which the user may need to install with ``uv sync``.
    """
    exe_name = "sau-mcp.exe" if os.name == "nt" else "sau-mcp"
    venv_bin = Path(sys.prefix) / "Scripts" if os.name == "nt" else Path(sys.prefix) / "bin"
    candidate = venv_bin / exe_name
    if candidate.exists():
        return candidate.resolve()
    found = shutil.which("sau-mcp")
    if found:
        return Path(found).resolve()
    fallback = Path(BASE_DIR) / ".venv" / "bin" / exe_name
    return fallback.resolve()


def _resolve_default_db_path() -> Path:
    """Return the project's default DB path for the ``SAU_MCP_DB_PATH`` env.

    Operators running the backend in-place already have this file; we point
    the MCP entry at it so agents see the same rows as the web UI.
    """
    return (Path(BASE_DIR) / "db" / "database.db").resolve()


def _build_sau_mcp_entry() -> dict:
    """Construct the JSON entry an MCP client expects for sau-mcp."""
    entry: dict = {"command": str(_resolve_sau_mcp_binary())}
    db_path = _resolve_default_db_path()
    entry["env"] = {"SAU_MCP_DB_PATH": str(db_path)}
    return entry


def _detect_skill_targets() -> list[SkillTarget]:
    """Locate every known MCP client config on this machine.

    All paths are returned even when the file does not exist — callers
    distinguish "missing" from "empty" so the installer can decide whether
    to create the file (which is fine for ``~/.cursor/mcp.json``) or skip
    it (e.g. Claude Desktop absent).
    """
    home = Path.home()
    targets: list[SkillTarget] = []

    # Claude Desktop — Mac/Win/Linux
    if sys.platform == "darwin":
        cd_path = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        cd_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        cd_path = home / ".config" / "Claude" / "claude_desktop_config.json"
    targets.append(SkillTarget(client="claude-desktop", config_path=cd_path, servers_key="mcpServers"))

    # Cursor — `~/.cursor/mcp.json` (also recognised as `~/.config/cursor/mcp.json`)
    cursor_path = home / ".cursor" / "mcp.json"
    targets.append(SkillTarget(client="cursor", config_path=cursor_path, servers_key="mcpServers"))

    # Claude Code — global MCP servers live under `~/.claude.json`
    targets.append(SkillTarget(client="claude-code", config_path=home / ".claude.json", servers_key="mcpServers"))

    return targets


def _filter_targets(targets: list[SkillTarget], client: str) -> list[SkillTarget]:
    if client == "all":
        return list(targets)
    return [t for t in targets if t.client == client]


def _read_config(target: SkillTarget) -> dict:
    if not target.config_path.exists():
        return {}
    try:
        with target.config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read {target.config_path}: {exc}. Refusing to overwrite an unparseable config."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{target.config_path} is not a JSON object (got {type(data).__name__}). Skipping."
        )
    return data


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to ``path`` via a sibling tempfile + rename so a crash never
    leaves the client with a truncated config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".sau-tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def _ensure_servers_block(config: dict, target: SkillTarget) -> dict:
    """Return the ``mcpServers`` block, creating it if missing."""
    servers = config.get(target.servers_key)
    if servers is None:
        servers = {}
        config[target.servers_key] = servers
    if not isinstance(servers, dict):
        raise RuntimeError(
            f"{target.config_path} has a non-object `{target.servers_key}` block; refusing to touch it."
        )
    return servers


def _format_target_line(target: SkillTarget, status: str, detail: str = "") -> str:
    base = f"{target.client:<14} {status:<10} {target.config_path}"
    return f"{base} {detail}".rstrip()


def _skill_list(client: str) -> int:
    targets = _filter_targets(_detect_skill_targets(), client)
    if not targets:
        print(f"No MCP clients matched --client={client}", file=sys.stderr)
        return 2
    print(f"{'client':<14} {'status':<10} config path")
    print("-" * 80)
    for target in targets:
        if not target.config_path.exists():
            print(_format_target_line(target, "missing"))
            continue
        try:
            config = _read_config(target)
        except RuntimeError as exc:
            print(_format_target_line(target, "broken", str(exc)))
            continue
        servers = config.get(target.servers_key) or {}
        registered = isinstance(servers, dict) and _SAU_MCP_NAME in servers
        status = "registered" if registered else "present"
        detail = f"({_SAU_MCP_NAME}={'yes' if registered else 'no'})"
        print(_format_target_line(target, status, detail))
    return 0


def _skill_install(client: str, dry_run: bool) -> int:
    targets = _filter_targets(_detect_skill_targets(), client)
    if not targets:
        print(f"No MCP clients matched --client={client}", file=sys.stderr)
        return 2

    entry = _build_sau_mcp_entry()
    changed: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []

    for target in targets:
        if not target.config_path.exists():
            missing.append(target.client)
            continue
        try:
            config = _read_config(target)
            servers = _ensure_servers_block(config, target)
        except RuntimeError as exc:
            skipped.append(f"{target.client}: {exc}")
            continue

        existing = servers.get(_SAU_MCP_NAME)
        if existing == entry:
            continue

        if dry_run:
            changed.append(f"{target.client} (would set {_SAU_MCP_NAME})")
            continue

        servers[_SAU_MCP_NAME] = entry
        try:
            _atomic_write_json(target.config_path, config)
        except OSError as exc:
            skipped.append(f"{target.client}: write failed ({exc})")
            continue
        changed.append(target.client)

    print("sau-mcp installer")
    print(f"  binary:  {entry['command']}")
    print(f"  db path: {entry['env']['SAU_MCP_DB_PATH']}")
    if changed:
        verb = "would update" if dry_run else "updated"
        print(f"{verb}: {', '.join(changed)}")
    if missing:
        print(
            f"skipped (config not found): {', '.join(missing)} — "
            "install the client first or pass --client explicitly."
        )
    if skipped:
        print("skipped (manual fix needed):")
        for line in skipped:
            print(f"  - {line}")
    if not changed and not missing and not skipped:
        print("everything already registered — nothing to do.")
    return 0


def _skill_remove(client: str) -> int:
    targets = _filter_targets(_detect_skill_targets(), client)
    if not targets:
        print(f"No MCP clients matched --client={client}", file=sys.stderr)
        return 2

    removed: list[str] = []
    missing: list[str] = []

    for target in targets:
        if not target.config_path.exists():
            missing.append(target.client)
            continue
        try:
            config = _read_config(target)
        except RuntimeError as exc:
            print(f"skipped {target.client}: {exc}", file=sys.stderr)
            continue
        servers = config.get(target.servers_key)
        if not isinstance(servers, dict) or _SAU_MCP_NAME not in servers:
            continue
        del servers[_SAU_MCP_NAME]
        # Drop the parent key when empty so we don't leave a stale `mcpServers: {}`.
        if not servers:
            config.pop(target.servers_key, None)
        try:
            _atomic_write_json(target.config_path, config)
        except OSError as exc:
            print(f"write failed for {target.client}: {exc}", file=sys.stderr)
            continue
        removed.append(target.client)

    if removed:
        print(f"removed `sau` MCP entry from: {', '.join(removed)}")
    if missing:
        print(f"skipped (config not found): {', '.join(missing)}")
    if not removed and not missing:
        print("`sau` was not registered anywhere — nothing to do.")
    return 0


def _record_crash(exc: BaseException, argv: Sequence[str] | None, context: dict) -> None:
    """Persist a structured crash report so other agents can resume.

    Every unexpected ``Exception`` raised inside ``dispatch`` is written to
    ``logs/fixes/crash-<UTC-timestamp>.json`` alongside the argv the user ran
    and a small context dict (platform/action/account when known). The
    function never raises — recording must not mask the original error.
    """

    try:
        fix_dir = BASE_DIR / "logs" / "fixes"
        fix_dir.mkdir(parents=True, exist_ok=True)
        utc_now = datetime.now(timezone.utc)
        timestamp = utc_now.strftime("%Y%m%dT%H%M%S%fZ")
        payload = {
            "captured_at": utc_now.isoformat().replace("+00:00", "Z"),
            "argv": list(argv) if argv is not None else sys.argv[1:],
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "context": context,
        }
        (fix_dir / f"crash-{timestamp}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        # Recording must never mask the original failure.
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    context: dict = {}
    try:
        # Record only the fields that survived argparse validation; missing
        # attributes mean the user passed something so broken argparse rejected
        # it before reaching here.
        context = {
            "platform": getattr(args, "platform", None),
            "action": getattr(args, "action", None),
            "account": getattr(args, "account", None),
        }
        return asyncio.run(dispatch(args))
    except SystemExit:
        raise
    except Exception as exc:
        _record_crash(exc, argv, context)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            f"(crash recorded under logs/fixes/ — run `python3 -m sau_cli "
            f"--help` to confirm argparse still works, then re-run after fixing)",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
