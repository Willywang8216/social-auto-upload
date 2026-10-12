"""Language-based account routing.

Chinese content is published only to Chinese-audience accounts; English content
goes to English-audience accounts, and to Chinese ones only when the caller
opts in (``options.includeZhForEnglish``). An account with no audience language
is never filtered, so an unclassified destination is not silently dropped.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import db.createTable as create_table  # noqa: E402
from myUtils import campaign_prep, campaigns, media_groups, profiles  # noqa: E402
from myUtils import publish_orchestrator as po  # noqa: E402


def _account(account_id: int, language: str, platform: str = "telegram"):
    return SimpleNamespace(
        id=account_id,
        platform=platform,
        nickname=f"acct-{account_id}",
        account_name=f"acct-{account_id}",
        config={"audience_language": language},
        profile_id=1,
    )


_PROFILE = SimpleNamespace(settings={})


def _reason(content_language, account_language, *, include_zh=False):
    return po.language_routing_skip_reason(
        content_language,
        _account(1, account_language),
        _PROFILE,
        include_zh_for_english=include_zh,
    )


class LanguageRoutingTests(unittest.TestCase):
    def test_chinese_content_reaches_chinese_accounts(self):
        self.assertIsNone(_reason("zh", "zh-Hant"))
        self.assertIsNone(_reason("zh", "zh"))
        self.assertIsNone(_reason("zh", "en,zh-Hant"))  # bilingual

    def test_chinese_content_skips_english_only_accounts(self):
        self.assertIsNotNone(_reason("zh", "en"))

    def test_english_content_reaches_english_accounts(self):
        self.assertIsNone(_reason("en", "en"))
        self.assertIsNone(_reason("en", "en,zh-Hant"))  # bilingual

    def test_english_content_skips_chinese_only_by_default(self):
        self.assertIsNotNone(_reason("en", "zh-Hant"))

    def test_english_content_can_include_chinese_only_on_opt_in(self):
        self.assertIsNone(_reason("en", "zh-Hant", include_zh=True))

    def test_unknown_content_language_is_never_filtered(self):
        for language in ("", None, "fr", "zh-Hant"):
            self.assertIsNone(_reason(language, "en"))
            self.assertIsNone(_reason(language, "zh-Hant"))

    def test_account_without_language_is_never_filtered(self):
        self.assertIsNone(_reason("zh", ""))
        self.assertIsNone(_reason("en", ""))

    def test_multiple_language_tokens_are_parsed(self):
        account = _account(1, "en+zh-Hant")
        self.assertEqual(po._account_language_tokens(account, _PROFILE), {"en", "zh-hant"})
        account = _account(1, "en, zh-Hant / zh")
        self.assertEqual(
            po._account_language_tokens(account, _PROFILE), {"en", "zh-hant", "zh"}
        )

    def test_content_language_prefers_media_context(self):
        self.assertEqual(po._content_language({"language": "zh"}, {"language": "en"}), "zh")
        self.assertEqual(po._content_language({}, {"language": "en"}), "en")
        self.assertIsNone(po._content_language({}, {}))
        self.assertIsNone(po._content_language({"language": "fr"}, {}))


class LanguageRoutingIntegrationTests(unittest.TestCase):
    """The filter must actually run inside the prep/finalize path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "route.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("Brand", db_path=self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO file_records (filename, filesize, file_path) VALUES (?, ?, ?)",
                ("clip.mp4", 1.0, "/tmp/clip.mp4"),
            )
            file_id = int(cursor.lastrowid)
            conn.commit()
        self.media_group = media_groups.create_media_group(
            "Launch", primary_video_file_id=file_id, db_path=self.db_path
        )
        media_groups.add_media_group_item(
            self.media_group.id, file_id, role=media_groups.ROLE_VIDEO, db_path=self.db_path
        )
        self.en = profiles.add_account(
            self.profile.id, profiles.PLATFORM_DOUYIN, "en",
            config={"audience_language": "en"}, db_path=self.db_path,
        )
        self.zh = profiles.add_account(
            self.profile.id, profiles.PLATFORM_DOUYIN, "zh",
            config={"audience_language": "zh-Hant"}, db_path=self.db_path,
        )
        self.bi = profiles.add_account(
            self.profile.id, profiles.PLATFORM_DOUYIN, "bi",
            config={"audience_language": "en,zh-Hant"}, db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, language: str, *, include_zh: bool = False):
        campaign = campaigns.create_campaign(
            self.profile.id,
            self.media_group.id,
            status=campaigns.CAMPAIGN_PREPARING,
            selected_account_ids=[self.en.id, self.zh.id, self.bi.id],
            metadata={
                campaigns.PREP_REQUEST_KEY: {
                    "brief": "b",
                    "options": {"includeZhForEnglish": include_zh},
                    "schedule": {"publishNow": True},
                }
            },
            db_path=self.db_path,
        )
        return campaign_prep.run_campaign_prep(
            campaign,
            db_path=self.db_path,
            prepare_artifacts=lambda *_a, **_k: {
                "videoLocalPath": "/tmp/out.mp4",
                "language": language,
            },
            generate_account_draft=lambda *_a, **_k: {
                "message": "hello",
                "hashtags": [],
                "firstComment": "",
            },
            artifact_payloads_for_platform=lambda *_a, **_k: [{"local_path": "/tmp/out.mp4"}],
            artifact_part_groups_for_platform=None,
            job_to_payload=lambda job: {"id": job.id},
        )

    def test_chinese_content_skips_the_english_account(self) -> None:
        result = self._run("zh")
        self.assertEqual(len(result["jobs"]), 2)
        reasons = [item.get("reason") for item in result["skipped"]]
        self.assertTrue(any("zh" in (r or "") for r in reasons), reasons)

    def test_english_content_skips_the_chinese_account_by_default(self) -> None:
        result = self._run("en")
        self.assertEqual(len(result["jobs"]), 2)
        reasons = [item.get("reason") for item in result["skipped"]]
        self.assertTrue(any("Chinese-only" in (r or "") for r in reasons), reasons)

    def test_english_content_can_include_chinese_on_opt_in(self) -> None:
        result = self._run("en", include_zh=True)
        self.assertEqual(len(result["jobs"]), 3)
        self.assertEqual(result["skipped"], [])


if __name__ == "__main__":
    unittest.main()
