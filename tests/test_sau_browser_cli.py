import asyncio
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sau_cli


class BrowserCliParserTests(unittest.TestCase):
    def test_build_parser_accepts_xiaohongshu_login(self):
        parser = sau_cli.build_parser()
        args = parser.parse_args(["xiaohongshu", "login", "--account", "creator"])
        self.assertEqual(args.platform, "xiaohongshu")
        self.assertEqual(args.action, "login")

    def test_douyin_upload_video_accepts_desc(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            video_path.write_bytes(b"video")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "douyin",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "标题",
                    "--desc",
                    "视频简介",
                ]
            )

        self.assertEqual(args.desc, "视频简介")

    def test_douyin_upload_video_accepts_dual_thumbnail_aspects(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            landscape_path = Path(tmp_dir) / "landscape.png"
            portrait_path = Path(tmp_dir) / "portrait.png"
            video_path.write_bytes(b"video")
            landscape_path.write_bytes(b"image")
            portrait_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "douyin",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "标题",
                    "--thumbnail-landscape",
                    str(landscape_path),
                    "--thumbnail-portrait",
                    str(portrait_path),
                ]
            )

        self.assertEqual(args.thumbnail_landscape, landscape_path)
        self.assertEqual(args.thumbnail_portrait, portrait_path)

    def test_tencent_upload_video_accepts_dual_thumbnail_aspects(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            landscape_path = Path(tmp_dir) / "landscape.png"
            portrait_path = Path(tmp_dir) / "portrait.png"
            video_path.write_bytes(b"video")
            landscape_path.write_bytes(b"image")
            portrait_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "tencent",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "标题",
                    "--thumbnail-landscape",
                    str(landscape_path),
                    "--thumbnail-portrait",
                    str(portrait_path),
                ]
            )

        self.assertEqual(args.thumbnail_landscape, landscape_path)
        self.assertEqual(args.thumbnail_portrait, portrait_path)

    def test_kuaishou_upload_note_accepts_title_and_note(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "1.png"
            image_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "kuaishou",
                    "upload-note",
                    "--account",
                    "creator",
                    "--images",
                    str(image_path),
                    "--title",
                    "图文标题",
                    "--note",
                    "图文正文",
                ]
            )

        self.assertEqual(args.title, "图文标题")
        self.assertEqual(args.note, "图文正文")

    def test_xiaohongshu_upload_video_defaults_to_headless(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            video_path.write_bytes(b"video")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "xiaohongshu",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "视频标题",
                ]
            )

        self.assertTrue(args.headless)

    def test_xiaohongshu_upload_note_accepts_headed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "1.png"
            image_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "xiaohongshu",
                    "upload-note",
                    "--account",
                    "creator",
                    "--images",
                    str(image_path),
                    "--title",
                    "图文标题",
                    "--note",
                    "图文正文",
                    "--headed",
                ]
            )

        self.assertFalse(args.headless)


class BrowserCliDispatchTests(unittest.TestCase):
    def test_dispatch_xiaohongshu_check_prints_valid(self):
        args = Namespace(platform="xiaohongshu", action="check", account="creator")
        with patch("sau_cli.check_xiaohongshu_account", new=AsyncMock(return_value=True)):
            code = asyncio.run(sau_cli.dispatch(args))
        self.assertEqual(code, 0)

    def test_dispatch_douyin_upload_note_uses_new_request_fields(self):
        args = Namespace(
            platform="douyin",
            action="upload-note",
            account="creator",
            images=[Path("1.png")],
            title="图文标题",
            note="图文正文",
            tags="测试,图文",
            schedule=0,
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_note", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.title, "图文标题")
        self.assertEqual(request.note, "图文正文")

    def test_dispatch_douyin_upload_video_uses_dual_thumbnail_request_fields(self):
        args = Namespace(
            platform="douyin",
            action="upload-video",
            account="creator",
            file=Path("demo.mp4"),
            title="视频标题",
            desc="视频简介",
            tags="测试,视频",
            schedule=0,
            thumbnail=None,
            thumbnail_landscape=Path("landscape.png"),
            thumbnail_portrait=Path("portrait.png"),
            product_link="",
            product_title="",
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_video", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.thumbnail_landscape_file, Path("landscape.png"))
        self.assertEqual(request.thumbnail_portrait_file, Path("portrait.png"))

    def test_dispatch_tencent_upload_video_uses_dual_thumbnail_request_fields(self):
        args = Namespace(
            platform="tencent",
            action="upload-video",
            account="creator",
            file=Path("demo.mp4"),
            title="视频标题",
            desc="视频简介",
            tags="测试,视频",
            schedule=0,
            thumbnail_landscape=Path("landscape.png"),
            thumbnail_portrait=Path("portrait.png"),
            short_title=None,
            category=None,
            draft=False,
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_tencent_video", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.thumbnail_landscape_file, Path("landscape.png"))
        self.assertEqual(request.thumbnail_portrait_file, Path("portrait.png"))

    def test_dispatch_xiaohongshu_upload_video_uses_headed_request(self):
        args = Namespace(
            platform="xiaohongshu",
            action="upload-video",
            account="creator",
            file=Path("demo.mp4"),
            title="视频标题",
            desc="视频简介",
            tags="测试,视频",
            schedule=0,
            thumbnail=None,
            debug=False,
            headless=False,
        )
        with patch("sau_cli.upload_xiaohongshu_video", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.title, "视频标题")
        self.assertEqual(request.description, "视频简介")
        self.assertFalse(request.headless)

    def test_dispatch_xiaohongshu_upload_note_uses_headless_request(self):
        args = Namespace(
            platform="xiaohongshu",
            action="upload-note",
            account="creator",
            images=[Path("1.png"), Path("2.png")],
            title="图文标题",
            note="图文正文",
            tags="测试,图文",
            schedule=0,
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_xiaohongshu_note", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.title, "图文标题")
        self.assertEqual(request.note, "图文正文")
        self.assertTrue(request.headless)
        self.assertEqual(len(request.image_files), 2)


class SkillInstallerTests(unittest.TestCase):
    """End-to-end tests for ``sau skill install`` / ``remove`` / ``list``.

    These use a throwaway ``HOME`` so we never touch the operator's real
    Claude Desktop / Cursor / Claude Code configs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cursor_dir = self.tmp / ".cursor"
        self.cursor_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_install_writes_sau_entry_under_mcpServers(self) -> None:
        (self.cursor_dir / "mcp.json").write_text("{}", encoding="utf-8")
        with patch("sau_cli.Path.home", lambda: self.tmp):
            code = asyncio.run(
                sau_cli.dispatch(
                    sau_cli.build_parser().parse_args(["skill", "install", "--client", "cursor"])
                )
            )

        self.assertEqual(code, 0)
        written = json.loads((self.cursor_dir / "mcp.json").read_text(encoding="utf-8"))
        self.assertIn("mcpServers", written)
        self.assertIn("sau", written["mcpServers"])
        self.assertEqual(written["mcpServers"]["sau"]["command"], str(sau_cli._resolve_sau_mcp_binary()))
        self.assertEqual(
            written["mcpServers"]["sau"]["env"]["SAU_MCP_DB_PATH"],
            str(sau_cli._resolve_default_db_path()),
        )

    def test_install_dry_run_does_not_touch_disk(self) -> None:
        (self.cursor_dir / "mcp.json").write_text("{}", encoding="utf-8")
        original = (self.cursor_dir / "mcp.json").read_text(encoding="utf-8")
        with patch("sau_cli.Path.home", lambda: self.tmp):
            asyncio.run(
                sau_cli.dispatch(
                    sau_cli.build_parser().parse_args(
                        ["skill", "install", "--client", "cursor", "--dry-run"]
                    )
                )
            )
        self.assertEqual((self.cursor_dir / "mcp.json").read_text(encoding="utf-8"), original)

    def test_install_preserves_other_servers(self) -> None:
        (self.cursor_dir / "mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"other": {"command": "/usr/bin/other-mcp"}}},
            ),
            encoding="utf-8",
        )
        with patch("sau_cli.Path.home", lambda: self.tmp):
            asyncio.run(
                sau_cli.dispatch(
                    sau_cli.build_parser().parse_args(["skill", "install", "--client", "cursor"])
                )
            )
        written = json.loads((self.cursor_dir / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(written["mcpServers"]["other"], {"command": "/usr/bin/other-mcp"})
        self.assertIn("sau", written["mcpServers"])

    def test_remove_drops_only_sau_entry(self) -> None:
        (self.cursor_dir / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "other": {"command": "/usr/bin/other-mcp"},
                        "sau": {"command": "/tmp/sau-mcp"},
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch("sau_cli.Path.home", lambda: self.tmp):
            code = asyncio.run(
                sau_cli.dispatch(
                    sau_cli.build_parser().parse_args(["skill", "remove", "--client", "cursor"])
                )
            )
        self.assertEqual(code, 0)
        written = json.loads((self.cursor_dir / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(list(written["mcpServers"].keys()), ["other"])

    def test_install_skips_unparseable_config(self) -> None:
        (self.cursor_dir / "mcp.json").write_text("not-json", encoding="utf-8")
        with patch("sau_cli.Path.home", lambda: self.tmp):
            code = asyncio.run(
                sau_cli.dispatch(
                    sau_cli.build_parser().parse_args(["skill", "install", "--client", "cursor"])
                )
            )
        self.assertEqual(code, 0)
        # File untouched — installer must never overwrite an unparseable config.
        self.assertEqual((self.cursor_dir / "mcp.json").read_text(encoding="utf-8"), "not-json")


if __name__ == "__main__":
    unittest.main()
