"""Language-based account routing.

Chinese content is published only to Chinese-audience accounts; English content
goes to English-audience accounts, and to Chinese ones only when the caller
opts in (``options.includeZhForEnglish``). An account with no audience language
is never filtered, so an unclassified destination is not silently dropped.
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        from datetime import datetime, timezone
        import hashlib
        from myUtils import media_compliance, publish_readiness
        from tests.test_atomic_publication_groups import synthetic_video_probe
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = ExitStack()
        self.db_path = Path(self._tmp.name) / "route.db"
        create_table.bootstrap(self.db_path)
        self.profile = profiles.create_profile("Brand", db_path=self.db_path)
        self.source = Path(self._tmp.name) / "SFW clip.mp4"
        self.source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 120)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            size = self.source.stat().st_size
            self.file_id = conn.execute("INSERT INTO file_records (filename,filesize,file_path,sha256,size_bytes) VALUES (?,?,?,?,?)",
                (self.source.name, size, str(self.source), hashlib.sha256(self.source.read_bytes()).hexdigest(), size)).lastrowid
        self.media_group = media_groups.create_media_group("Launch", primary_video_file_id=self.file_id, db_path=self.db_path)
        media_groups.add_media_group_item(self.media_group.id, self.file_id, role=media_groups.ROLE_VIDEO, db_path=self.db_path)
        self._patches.enter_context(patch.object(media_compliance, "_source_metadata", side_effect=synthetic_video_probe))
        self._patches.enter_context(patch.object(media_compliance, "decode_video", return_value=None))
        accounts = []
        for name, language in (("en", "en"), ("zh", "zh-Hant"), ("bi", "en,zh-Hant")):
            account = profiles.add_account(self.profile.id, "telegram", name,
                config={"audience_language": language, "botToken": "fixture-only", "chatId": f"@lang-{name}"}, db_path=self.db_path)
            context = publish_readiness._context(account, self.profile.settings)
            publish_readiness.record_destination_readiness(account, self.profile.settings, provider="direct", db_path=self.db_path,
                proof={"verified": True, "live_check": True, "source": "provider_verification", "requires_approval": False,
                    "config_fingerprint": context["fingerprint"], "identity": context["identity"],
                    "permissions": {"posts.create": True}, "capabilities": ["video"],
                    "verified_at": datetime.now(timezone.utc).isoformat()})
            accounts.append(account)
        self.en, self.zh, self.bi = accounts

    def tearDown(self) -> None:
        self._patches.close()
        self._tmp.cleanup()

    def _run(self, language: str, *, include_zh: bool = False):
        def prepare(campaign_id, *_args, **_kwargs):
            from tests.test_atomic_publication_groups import prepared_video_metadata
            campaigns.add_campaign_artifact(campaign_id, artifact_kind="prepared_video",
                source_file_record_id=self.file_id, local_path=str(self.source),
                metadata=prepared_video_metadata(self.source, self.source, "telegram", generation=f"language-{campaign_id}"),
                db_path=self.db_path)
            return {"videoLocalPath": str(self.source), "language": language}
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
            prepare_artifacts=prepare,
            generate_account_draft=lambda *_a, **_k: {
                "message": "hello",
                "hashtags": [],
                "firstComment": "",
            },
            artifact_payloads_for_platform=lambda artifacts, _platform: artifacts,
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
