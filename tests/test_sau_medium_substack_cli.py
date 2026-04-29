"""CLI tests for medium, substack and profile subcommands."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sau_cli


class MediumSubstackParserTests(unittest.TestCase):
    def test_medium_login_accepts_account(self) -> None:
        parser = sau_cli.build_parser()
        args = parser.parse_args(["medium", "login", "--account", "alice"])
        self.assertEqual(args.platform, "medium")
        self.assertEqual(args.action, "login")
        self.assertEqual(args.account, "alice")
        self.assertIsNone(args.profile)

    def test_medium_login_accepts_profile(self) -> None:
        parser = sau_cli.build_parser()
        args = parser.parse_args(["medium", "login", "--account", "alice", "--profile", "acme"])
        self.assertEqual(args.profile, "acme")

    def test_medium_upload_post_required_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("# hello", encoding="utf-8")
            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "medium",
                    "upload-post",
                    "--account",
                    "alice",
                    "--file",
                    str(body),
                    "--title",
                    "Title",
                    "--tags",
                    "ai,python",
                ]
            )
        self.assertEqual(args.title, "Title")
        self.assertEqual(args.tags, "ai,python")
        self.assertFalse(args.draft)

    def test_substack_upload_post_required_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("# hello", encoding="utf-8")
            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "substack",
                    "upload-post",
                    "--account",
                    "alice",
                    "--publication",
                    "acme",
                    "--file",
                    str(body),
                    "--title",
                    "Title",
                ]
            )
        self.assertEqual(args.publication, "acme")

    def test_substack_unsupported_body_extension_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.docx"
            body.write_text("not allowed", encoding="utf-8")
            parser = sau_cli.build_parser()
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "substack",
                        "upload-post",
                        "--account",
                        "alice",
                        "--publication",
                        "acme",
                        "--file",
                        str(body),
                        "--title",
                        "Title",
                    ]
                )


class MediumSubstackDispatchTests(unittest.TestCase):
    def test_medium_check_dispatches(self) -> None:
        args = Namespace(platform="medium", action="check", account="alice", profile=None)
        with patch("sau_cli.check_medium_account", new=AsyncMock(return_value=True)):
            code = asyncio.run(sau_cli.dispatch(args))
        self.assertEqual(code, 0)

    def test_medium_upload_post_immediate_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("# hi", encoding="utf-8")
            args = Namespace(
                platform="medium",
                action="upload-post",
                account="alice",
                profile="acme",
                file=body,
                title="Hello world",
                subtitle="",
                tags="ai,python",
                cover=None,
                draft=False,
                debug=False,
                headless=True,
            )
            with patch("sau_cli.upload_medium_post", new=AsyncMock()) as mock_upload:
                asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.title, "Hello world")
        self.assertEqual(request.tags, ["ai", "python"])
        self.assertEqual(request.publish_strategy, "immediate")
        self.assertEqual(request.profile, "acme")

    def test_medium_upload_post_draft_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("# hi", encoding="utf-8")
            args = Namespace(
                platform="medium",
                action="upload-post",
                account="alice",
                profile=None,
                file=body,
                title="Hello",
                subtitle="",
                tags="",
                cover=None,
                draft=True,
                debug=False,
                headless=True,
            )
            with patch("sau_cli.upload_medium_post", new=AsyncMock()) as mock_upload:
                asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.publish_strategy, "draft")

    def test_substack_upload_post_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("hello", encoding="utf-8")
            args = Namespace(
                platform="substack",
                action="upload-post",
                account="alice",
                profile=None,
                publication="acme",
                file=body,
                title="Hi",
                subtitle="",
                tags="ai",
                schedule=0,
                draft=False,
                debug=False,
                headless=True,
            )
            with patch("sau_cli.upload_substack_post", new=AsyncMock()) as mock_upload:
                asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.publication, "acme")
        self.assertEqual(request.publish_strategy, "immediate")


class ProfileCliTests(unittest.TestCase):
    def test_profile_create_dispatch(self) -> None:
        args = Namespace(platform="profile", action="create", name="Acme Corp", description="brand")

        fake_profile = type("P", (), {"slug": "acme-corp", "id": 1})()
        with patch("sau_cli.profile_registry.create_profile", return_value=fake_profile) as mock_create:
            code = asyncio.run(sau_cli.dispatch(args))

        self.assertEqual(code, 0)
        mock_create.assert_called_once_with("Acme Corp", description="brand")

    def test_profile_show_lists_accounts(self) -> None:
        args = Namespace(platform="profile", action="show", profile="acme")

        fake_profile = type("P", (), {"slug": "acme", "name": "Acme", "id": 7})()
        fake_account = type(
            "A",
            (),
            {
                "platform": "medium",
                "account_name": "alice",
                "status": 1,
                "cookie_path": "/tmp/x.json",
            },
        )()

        with (
            patch("sau_cli.profile_registry.get_profile_by_slug", return_value=fake_profile),
            patch("sau_cli.profile_registry.list_accounts", return_value=[fake_account]),
        ):
            code = asyncio.run(sau_cli.dispatch(args))
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
