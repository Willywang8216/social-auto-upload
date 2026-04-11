import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.account_registry import ensure_account_tables
from utils.profile_pipeline import ensure_profile_tables, save_profile
from utils.publish_jobs import (
    ensure_publish_job_tables,
    execute_due_publish_jobs,
    generate_publish_batch_drafts,
    get_publish_calendar_entries,
    list_publish_jobs,
    regenerate_publish_job_content,
    save_publish_jobs,
    sync_publish_job_statuses,
    update_publish_job_content,
)


class PublishJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.db_path = self.base_dir / "database.db"
        ensure_account_tables(self.db_path)
        ensure_profile_tables(self.db_path)
        ensure_publish_job_tables(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE file_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    filesize REAL,
                    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                    file_path TEXT
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (3, "douyin-cookie.json", "抖音主帳", 1, "douyin", "qr_cookie", "{}"),
            )
            cursor.execute(
                """
                INSERT INTO file_records (filename, filesize, file_path)
                VALUES (?, ?, ?)
                """,
                ("clip.mp4", 12.3, "videoFile/clip.mp4"),
            )
            conn.commit()
        self.profile = save_profile(
            self.db_path,
            {
                "name": "Creator Alpha",
                "accountIds": [1],
                "settings": {
                    "contentAccounts": [
                        {
                            "id": "acct-twitter-main",
                            "platform": "twitter",
                            "name": "X 主帳",
                            "postPreset": "Preset X",
                        }
                    ]
                },
            },
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_generate_publish_batch_drafts_creates_managed_and_content_targets(self):
        fake_batch_result = {
            "profile": {"id": self.profile["id"], "name": "Creator Alpha"},
            "selectedAccountIds": [1],
            "results": [
                {
                    "material": {"id": 1, "filename": "clip.mp4"},
                    "processedMediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                    "storage": {
                        "publicUrl": "https://cdn.example.com/clip.mp4",
                        "mediaKind": "video",
                    },
                    "transcript": "transcript body",
                    "posts": {"twitter": "Tweet copy", "facebook": "FB copy"},
                    "contentAccountResults": [
                        {
                            "account": {
                                "id": "acct-twitter-main",
                                "platform": "twitter",
                                "name": "X 主帳",
                                "postPreset": "Preset X",
                            },
                            "content": "Tweet copy",
                        }
                    ],
                }
            ],
        }

        with patch("utils.publish_jobs.generate_profile_batch_content", return_value=fake_batch_result):
            result = generate_publish_batch_drafts(
                self.db_path,
                self.base_dir,
                {
                    "profileId": self.profile["id"],
                    "materialIds": [1],
                    "selectedAccountIds": [1],
                    "selectedContentAccountIds": ["acct-twitter-main"],
                },
            )

        self.assertEqual(result["summary"]["items"], 2)
        delivery_modes = {item["deliveryMode"] for item in result["items"]}
        self.assertEqual(delivery_modes, {"direct_upload"})
        managed_item = next(item for item in result["items"] if item["targetKind"] == "managed_account")
        self.assertEqual(managed_item["platformKey"], "douyin")
        content_item = next(item for item in result["items"] if item["targetKind"] == "content_account")
        self.assertEqual(content_item["contentAccountId"], "acct-twitter-main")

    def test_generate_publish_batch_drafts_uses_linked_content_persona_for_managed_account(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (0, "", "X 發佈帳", 1, "twitter", "manual", '{"accessToken":"token"}'),
            )
            twitter_account_id = cursor.lastrowid
            conn.commit()

        profile = save_profile(
            self.db_path,
            {
                "name": "Creator Linked",
                "accountIds": [twitter_account_id],
                "settings": {
                    "contentAccounts": [
                        {
                            "id": "acct-twitter-linked",
                            "platform": "twitter",
                            "name": "X Persona",
                            "postPreset": "Preset Linked",
                            "publishingAccountId": twitter_account_id,
                        }
                    ]
                },
            },
        )

        fake_batch_result = {
            "profile": {"id": profile["id"], "name": profile["name"]},
            "selectedAccountIds": [twitter_account_id],
            "results": [
                {
                    "material": {"id": 1, "filename": "clip.mp4"},
                    "processedMediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                    "storage": {
                        "publicUrl": "https://cdn.example.com/clip.mp4",
                        "mediaKind": "video",
                    },
                    "transcript": "transcript body",
                    "posts": {"twitter": "Generic tweet"},
                    "contentAccountResults": [
                        {
                            "account": {
                                "id": "acct-twitter-linked",
                                "platform": "twitter",
                                "name": "X Persona",
                                "postPreset": "Preset Linked",
                                "publishingAccountId": str(twitter_account_id),
                            },
                            "content": "Persona tweet",
                        }
                    ],
                }
            ],
        }

        with patch("utils.publish_jobs.generate_profile_batch_content", return_value=fake_batch_result):
            result = generate_publish_batch_drafts(
                self.db_path,
                self.base_dir,
                {
                    "profileId": profile["id"],
                    "materialIds": [1],
                    "selectedAccountIds": [twitter_account_id],
                    "selectedContentAccountIds": ["acct-twitter-linked"],
                },
            )

        managed_item = next(item for item in result["items"] if item["targetKind"] == "managed_account")
        self.assertEqual(managed_item["message"], "Persona tweet")
        self.assertEqual(managed_item["metadata"]["contentAccountId"], "acct-twitter-linked")
        self.assertEqual(managed_item["metadata"]["publishingAccountId"], str(twitter_account_id))

    def test_generate_publish_batch_drafts_marks_managed_facebook_and_bound_persona_as_direct_upload(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (0, "", "FB 發佈帳", 1, "facebook", "manual", '{"accessToken":"fb-token","pageId":"page-123"}'),
            )
            facebook_account_id = cursor.lastrowid
            conn.commit()

        profile = save_profile(
            self.db_path,
            {
                "name": "Creator Facebook",
                "accountIds": [facebook_account_id],
                "settings": {
                    "contentAccounts": [
                        {
                            "id": "acct-facebook-linked",
                            "platform": "facebook",
                            "name": "FB Persona",
                            "publishingAccountId": facebook_account_id,
                        }
                    ]
                },
            },
        )

        fake_batch_result = {
            "profile": {"id": profile["id"], "name": profile["name"]},
            "selectedAccountIds": [facebook_account_id],
            "results": [
                {
                    "material": {"id": 1, "filename": "clip.mp4"},
                    "processedMediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                    "storage": {
                        "publicUrl": "https://cdn.example.com/clip.mp4",
                        "mediaKind": "video",
                    },
                    "transcript": "transcript body",
                    "posts": {"facebook": "Generic FB copy"},
                    "contentAccountResults": [
                        {
                            "account": {
                                "id": "acct-facebook-linked",
                                "platform": "facebook",
                                "name": "FB Persona",
                                "publishingAccountId": str(facebook_account_id),
                            },
                            "content": "Persona FB copy",
                        }
                    ],
                }
            ],
        }

        with patch("utils.publish_jobs.generate_profile_batch_content", return_value=fake_batch_result):
            result = generate_publish_batch_drafts(
                self.db_path,
                self.base_dir,
                {
                    "profileId": profile["id"],
                    "materialIds": [1],
                    "selectedAccountIds": [facebook_account_id],
                    "selectedContentAccountIds": ["acct-facebook-linked"],
                },
            )

        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(item["deliveryMode"] == "direct_upload" for item in result["items"]))
        managed_item = next(item for item in result["items"] if item["targetKind"] == "managed_account")
        self.assertEqual(managed_item["message"], "Persona FB copy")

    def test_save_publish_jobs_expands_fixed_frequency_queue(self):
        result = save_publish_jobs(
            self.db_path,
            {
                "batchId": "batch-a",
                "mode": "queue",
                "startAt": "2026-05-01T10:00:00",
                "repeatCount": 3,
                "frequencyValue": 6,
                "frequencyUnit": "hours",
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-twitter-main",
                        "platformKey": "twitter",
                        "targetName": "X 主帳",
                        "deliveryMode": "sheet_export",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Tweet copy",
                        "metadata": {"mediaKind": "video"},
                    }
                ],
            },
        )

        self.assertEqual(result["count"], 3)
        jobs = list_publish_jobs(self.db_path, {"profileId": self.profile["id"]})
        self.assertEqual([job["scheduledAt"] for job in jobs], [
            "2026-05-01T10:00:00",
            "2026-05-01T16:00:00",
            "2026-05-01T22:00:00",
        ])
        self.assertTrue(all(job["status"] == "scheduled" for job in jobs))

    def test_update_publish_job_content_appends_revision(self):
        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-twitter-main",
                        "platformKey": "twitter",
                        "targetName": "X 主帳",
                        "deliveryMode": "sheet_export",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Tweet copy",
                        "metadata": {"mediaKind": "video"},
                    }
                ]
            },
        )

        updated = update_publish_job_content(
            self.db_path,
            {
                "jobId": saved["jobIds"][0],
                "title": "clip updated",
                "message": "Tweet copy revised",
                "metadata": {"tone": "sharp"},
            },
        )

        self.assertEqual(updated["title"], "clip updated")
        self.assertEqual(updated["message"], "Tweet copy revised")
        self.assertEqual(updated["revisionCount"], 2)
        self.assertEqual(updated["metadata"]["tone"], "sharp")

    def test_update_publish_job_content_can_reschedule_job(self):
        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-twitter-main",
                        "platformKey": "twitter",
                        "targetName": "X 主帳",
                        "deliveryMode": "sheet_export",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Tweet copy",
                        "metadata": {"mediaKind": "video"},
                    }
                ]
            },
        )

        updated = update_publish_job_content(
            self.db_path,
            {
                "jobId": saved["jobIds"][0],
                "scheduledAt": "2026-06-01T12:30:00",
            },
        )

        self.assertEqual(updated["scheduledAt"], "2026-06-01T12:30:00")
        self.assertEqual(updated["status"], "scheduled")

    def test_regenerate_publish_job_content_updates_content_and_revision(self):
        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-twitter-main",
                        "platformKey": "twitter",
                        "targetName": "X 主帳",
                        "deliveryMode": "sheet_export",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Tweet copy",
                        "metadata": {"mediaKind": "video", "transcript": "old transcript"},
                    }
                ]
            },
        )

        with patch("utils.publish_jobs.generate_post_for_content_account", return_value="Regenerated tweet copy"):
            updated = regenerate_publish_job_content(
                self.db_path,
                self.base_dir,
                {
                    "jobId": saved["jobIds"][0],
                    "instructionText": "Make it more urgent",
                },
            )

        self.assertEqual(updated["message"], "Regenerated tweet copy")
        self.assertEqual(updated["revisionCount"], 2)
        self.assertEqual(updated["metadata"]["transcript"], "old transcript")
        self.assertTrue(updated["metadata"]["lastRegeneratedAt"])

    def test_regenerate_publish_job_content_accepts_unsaved_draft_payload(self):
        with patch("utils.publish_jobs.generate_post_for_content_account", return_value="Fresh regenerated draft"):
            updated = regenerate_publish_job_content(
                self.db_path,
                self.base_dir,
                {
                    "instructionText": "Make it more urgent",
                    "draft": {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-twitter-main",
                        "platformKey": "twitter",
                        "targetName": "X 主帳",
                        "deliveryMode": "sheet_export",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Old draft copy",
                        "metadata": {"mediaKind": "video", "transcript": "old transcript"},
                    },
                },
            )

        self.assertEqual(updated["message"], "Fresh regenerated draft")
        self.assertEqual(updated["title"], "clip")
        self.assertEqual(updated["metadata"]["transcript"], "old transcript")
        self.assertTrue(updated["metadata"]["lastRegeneratedAt"])

    def test_execute_due_publish_jobs_handles_sheet_and_direct_only(self):
        save_publish_jobs(
            self.db_path,
            {
                "mode": "schedule",
                "scheduledAt": "2026-01-01T10:00:00",
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "managed_account",
                        "accountId": 1,
                        "platformKey": "douyin",
                        "targetName": "抖音主帳",
                        "deliveryMode": "direct_upload",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Caption copy",
                        "metadata": {"mediaKind": "video"},
                    },
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-twitter-main",
                        "platformKey": "twitter",
                        "targetName": "X 主帳",
                        "deliveryMode": "sheet_export",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Tweet copy",
                        "metadata": {"mediaKind": "video", "postPreset": "Preset X"},
                    },
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-telegram-main",
                        "platformKey": "telegram",
                        "targetName": "Telegram 主帳",
                        "deliveryMode": "manual_only",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Telegram copy",
                        "metadata": {"mediaKind": "video"},
                    },
                ],
            },
        )

        with patch("utils.publish_jobs._execute_direct_upload_job") as direct_mock, patch("utils.publish_jobs._execute_sheet_export_job") as sheet_mock:
            result = execute_due_publish_jobs(self.db_path, self.base_dir)

        self.assertEqual(result["processed"], 2)
        self.assertEqual(direct_mock.call_count, 1)
        self.assertEqual(sheet_mock.call_count, 1)
        jobs = list_publish_jobs(self.db_path)
        statuses = {job["platformKey"]: job["status"] for job in jobs}
        self.assertEqual(statuses["douyin"], "published")
        self.assertEqual(statuses["twitter"], "exported")
        self.assertEqual(statuses["telegram"], "scheduled")

    def test_save_publish_jobs_creates_scheduler_sheet_copy_for_twitter_direct_jobs(self):
        profile_with_sheet = save_profile(
            self.db_path,
            {
                "name": "Creator With Sheet",
                "settings": {
                    "googleSheet": {"spreadsheetId": "sheet-123"},
                    "contentAccounts": [
                        {
                            "id": "acct-twitter-sheet",
                            "platform": "twitter",
                            "name": "X 排程帳",
                            "postPreset": "Preset X",
                            "publisherTargetId": "publisher-x-main",
                        }
                    ],
                },
            },
        )

        with patch("utils.publish_jobs.append_rows_to_google_sheet", return_value={
            "appended": 1,
            "worksheet": "2026-04-10-Creator With Sheet",
            "spreadsheetId": "sheet-123",
            "rowNumbers": [2],
        }):
            saved = save_publish_jobs(
                self.db_path,
                {
                    "items": [
                        {
                            "profileId": profile_with_sheet["id"],
                            "profileName": profile_with_sheet["name"],
                            "targetKind": "content_account",
                            "contentAccountId": "acct-twitter-sheet",
                            "platformKey": "twitter",
                            "targetName": "X 排程帳",
                            "deliveryMode": "direct_upload",
                            "materialId": 1,
                            "materialName": "clip.mp4",
                            "mediaPath": "videoFile/clip.mp4",
                            "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                            "title": "Tweet Title",
                            "message": "Tweet copy",
                            "metadata": {
                                "mediaKind": "video",
                                "postPreset": "Preset X",
                                "publisherTargetId": "publisher-x-main",
                            },
                        }
                    ],
                    "mode": "queue",
                    "startAt": "2026-04-10T10:00:00",
                    "repeatCount": 1,
                    "frequencyValue": 1,
                    "frequencyUnit": "days",
                },
            )

        job = list_publish_jobs(self.db_path, {"jobId": saved["jobIds"][0]})[0]
        scheduler_sheet = job["metadata"]["schedulerSheet"]
        self.assertEqual(scheduler_sheet["worksheet"], "2026-04-10-Creator With Sheet")
        self.assertEqual(scheduler_sheet["rowNumbers"], [2])
        self.assertEqual(scheduler_sheet["state"], "active")

    def test_execute_due_publish_jobs_retries_scheduler_sheet_copy_one_week_later_on_failure(self):
        profile_with_sheet = save_profile(
            self.db_path,
            {
                "name": "Creator Retry Sheet",
                "settings": {
                    "googleSheet": {"spreadsheetId": "sheet-456"},
                    "contentAccounts": [
                        {
                            "id": "acct-twitter-retry",
                            "platform": "twitter",
                            "name": "X Retry 帳",
                            "postPreset": "Preset Retry",
                            "publisherTargetId": "publisher-x-retry",
                        }
                    ],
                },
            },
        )

        with patch("utils.publish_jobs.append_rows_to_google_sheet", side_effect=[
            {
                "appended": 1,
                "worksheet": "2026-04-10-Creator Retry Sheet",
                "spreadsheetId": "sheet-456",
                "rowNumbers": [2],
            },
            {
                "appended": 1,
                "worksheet": "2026-04-17-Creator Retry Sheet",
                "spreadsheetId": "sheet-456",
                "rowNumbers": [7],
            },
        ]), patch("utils.publish_jobs.delete_rows_from_google_sheet") as delete_rows_mock:
            saved = save_publish_jobs(
                self.db_path,
                {
                    "items": [
                        {
                            "profileId": profile_with_sheet["id"],
                            "profileName": profile_with_sheet["name"],
                            "targetKind": "content_account",
                            "contentAccountId": "acct-twitter-retry",
                            "platformKey": "twitter",
                            "targetName": "X Retry 帳",
                            "deliveryMode": "direct_upload",
                            "materialId": 1,
                            "materialName": "clip.mp4",
                            "mediaPath": "videoFile/clip.mp4",
                            "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                            "title": "Tweet Retry",
                            "message": "Tweet retry copy",
                            "metadata": {
                                "mediaKind": "video",
                                "postPreset": "Preset Retry",
                                "publisherTargetId": "publisher-x-retry",
                            },
                        }
                    ],
                    "mode": "queue",
                    "startAt": "2026-04-10T10:00:00",
                    "repeatCount": 1,
                    "frequencyValue": 1,
                    "frequencyUnit": "days",
                },
            )

            with patch("utils.publish_jobs._execute_direct_upload_job", side_effect=RuntimeError("x failed")):
                result = execute_due_publish_jobs(self.db_path, self.base_dir, job_ids=saved["jobIds"])

        self.assertEqual(result["items"][0]["status"], "failed")
        job = list_publish_jobs(self.db_path, {"jobId": saved["jobIds"][0]})[0]
        scheduler_sheet = job["metadata"]["schedulerSheet"]
        self.assertEqual(scheduler_sheet["worksheet"], "2026-04-17-Creator Retry Sheet")
        self.assertEqual(scheduler_sheet["rowNumbers"], [7])
        self.assertEqual(scheduler_sheet["state"], "retry_active")
        self.assertEqual(scheduler_sheet["retryCount"], 1)
        delete_rows_mock.assert_called()

    def test_execute_due_publish_jobs_builds_direct_target_from_managed_twitter_account(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0,
                    "",
                    "X Direct",
                    1,
                    "twitter",
                    "manual",
                    '{"apiKey":"key","apiKeySecret":"secret","accessToken":"token","accessTokenSecret":"token-secret"}',
                ),
            )
            twitter_account_id = cursor.lastrowid
            conn.commit()

        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": self.profile["name"],
                        "targetKind": "managed_account",
                        "accountId": twitter_account_id,
                        "platformKey": "twitter",
                        "targetName": "X Direct",
                        "deliveryMode": "direct_upload",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": "videoFile/clip.mp4",
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "Tweet title",
                        "message": "Tweet body",
                        "metadata": {"mediaKind": "video"},
                    }
                ],
                "mode": "now",
            },
        )

        with patch("utils.publish_jobs.publish_job_to_direct_target") as publish_mock:
            result = execute_due_publish_jobs(self.db_path, self.base_dir, job_ids=saved["jobIds"])

        self.assertEqual(result["processed"], 1)
        self.assertEqual(publish_mock.call_count, 1)
        _, job_payload, target = publish_mock.call_args[0]
        self.assertEqual(job_payload["platformKey"], "twitter")
        self.assertEqual(target["platform"], "twitter")
        self.assertEqual(target["config"]["apiKey"], "key")
        self.assertEqual(target["config"]["accessTokenSecret"], "token-secret")

    def test_execute_due_publish_jobs_builds_direct_target_from_managed_facebook_account(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0,
                    "",
                    "FB Direct",
                    1,
                    "facebook",
                    "manual",
                    '{"accessToken":"fb-token","pageId":"page-123"}',
                ),
            )
            facebook_account_id = cursor.lastrowid
            conn.commit()

        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": self.profile["name"],
                        "targetKind": "managed_account",
                        "accountId": facebook_account_id,
                        "platformKey": "facebook",
                        "targetName": "FB Direct",
                        "deliveryMode": "direct_upload",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": "videoFile/clip.mp4",
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "FB title",
                        "message": "FB body",
                        "metadata": {"mediaKind": "video"},
                    }
                ],
                "mode": "now",
            },
        )

        with patch("utils.publish_jobs.publish_job_to_direct_target") as publish_mock:
            result = execute_due_publish_jobs(self.db_path, self.base_dir, job_ids=saved["jobIds"])

        self.assertEqual(result["processed"], 1)
        self.assertEqual(publish_mock.call_count, 1)
        _, job_payload, target = publish_mock.call_args[0]
        self.assertEqual(job_payload["platformKey"], "facebook")
        self.assertEqual(target["platform"], "facebook")
        self.assertEqual(target["config"]["pageId"], "page-123")
        self.assertEqual(target["config"]["accessToken"], "fb-token")

    def test_execute_due_publish_jobs_keeps_tiktok_job_in_processing_with_publish_id(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0,
                    "",
                    "TikTok Direct",
                    1,
                    "tiktok",
                    "manual",
                    '{"accessToken":"tt-token","privacyLevel":"SELF_ONLY"}',
                ),
            )
            tiktok_account_id = cursor.lastrowid
            conn.commit()

        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": self.profile["name"],
                        "targetKind": "managed_account",
                        "accountId": tiktok_account_id,
                        "platformKey": "tiktok",
                        "targetName": "TikTok Direct",
                        "deliveryMode": "direct_upload",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": "videoFile/clip.mp4",
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "TikTok title",
                        "message": "TikTok body",
                        "metadata": {"mediaKind": "video"},
                    }
                ],
                "mode": "now",
            },
        )

        with patch("utils.publish_jobs.publish_job_to_direct_target", return_value={"platform": "tiktok", "publishId": "publish-1", "finalStatus": "processing"}):
            result = execute_due_publish_jobs(self.db_path, self.base_dir, job_ids=saved["jobIds"])

        self.assertEqual(result["items"][0]["status"], "processing")
        job = list_publish_jobs(self.db_path, {"jobId": saved["jobIds"][0]})[0]
        self.assertEqual(job["status"], "processing")
        self.assertEqual(job["metadata"]["publishId"], "publish-1")

    def test_sync_publish_job_statuses_marks_youtube_job_published_and_backfills_url(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0,
                    "",
                    "YouTube Direct",
                    1,
                    "youtube",
                    "manual",
                    '{"accessToken":"yt-token","privacyStatus":"private","categoryId":"22"}',
                ),
            )
            youtube_account_id = cursor.lastrowid
            conn.commit()

        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": self.profile["name"],
                        "targetKind": "managed_account",
                        "accountId": youtube_account_id,
                        "platformKey": "youtube",
                        "targetName": "YouTube Direct",
                        "deliveryMode": "direct_upload",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": "videoFile/clip.mp4",
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "YT title",
                        "message": "YT body",
                        "metadata": {"mediaKind": "video", "videoId": "yt-123"},
                    }
                ],
                "mode": "now",
            },
        )
        update_publish_job_content(self.db_path, {"jobId": saved["jobIds"][0], "status": "processing"})

        with patch("utils.publish_jobs.refresh_direct_publish_job_status", return_value={
            "status": "published",
            "platform": "youtube",
            "remoteId": "yt-123",
            "url": "https://www.youtube.com/watch?v=yt-123",
            "details": {"uploadStatus": "processed", "processingStatus": "succeeded"},
        }):
            result = sync_publish_job_statuses(self.db_path, self.base_dir, job_ids=saved["jobIds"])

        self.assertEqual(result["items"][0]["status"], "published")
        job = list_publish_jobs(self.db_path, {"jobId": saved["jobIds"][0]})[0]
        self.assertEqual(job["status"], "published")
        self.assertEqual(job["metadata"]["publishedUrl"], "https://www.youtube.com/watch?v=yt-123")

    def test_sync_publish_job_statuses_backfills_threads_thread_id_after_container_finishes(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_info (type, filePath, userName, status, platform_key, auth_mode, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0,
                    "",
                    "Threads Direct",
                    1,
                    "threads",
                    "manual",
                    '{"accessToken":"threads-token","userId":"user-123"}',
                ),
            )
            account_id = cursor.lastrowid
            conn.commit()

        saved = save_publish_jobs(
            self.db_path,
            {
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": self.profile["name"],
                        "targetKind": "managed_account",
                        "accountId": account_id,
                        "platformKey": "threads",
                        "targetName": "Threads Direct",
                        "deliveryMode": "direct_upload",
                        "materialId": 1,
                        "materialName": "clip.png",
                        "mediaPath": "videoFile/clip.mp4",
                        "mediaPublicUrl": "https://cdn.example.com/clip.png",
                        "title": "Threads title",
                        "message": "Threads body",
                        "metadata": {"mediaKind": "image", "creationId": "creation-1"},
                    }
                ],
                "mode": "now",
            },
        )
        update_publish_job_content(self.db_path, {"jobId": saved["jobIds"][0], "status": "processing"})

        with patch("utils.publish_jobs.refresh_direct_publish_job_status", return_value={
            "status": "processing",
            "platform": "threads",
            "creationId": "creation-1",
            "threadId": "thread-9",
            "details": {"publishStage": "publish_triggered", "containerStatusCode": "FINISHED"},
        }):
            result = sync_publish_job_statuses(self.db_path, self.base_dir, job_ids=saved["jobIds"])

        self.assertEqual(result["items"][0]["status"], "processing")
        job = list_publish_jobs(self.db_path, {"jobId": saved["jobIds"][0]})[0]
        self.assertEqual(job["status"], "processing")
        self.assertEqual(job["metadata"]["creationId"], "creation-1")
        self.assertEqual(job["metadata"]["threadId"], "thread-9")

    def test_get_publish_calendar_entries_groups_by_day(self):
        save_publish_jobs(
            self.db_path,
            {
                "mode": "queue",
                "startAt": "2026-05-01T10:00:00",
                "repeatCount": 2,
                "frequencyValue": 1,
                "frequencyUnit": "days",
                "items": [
                    {
                        "profileId": self.profile["id"],
                        "profileName": "Creator Alpha",
                        "targetKind": "content_account",
                        "contentAccountId": "acct-twitter-main",
                        "platformKey": "twitter",
                        "targetName": "X 主帳",
                        "deliveryMode": "sheet_export",
                        "materialId": 1,
                        "materialName": "clip.mp4",
                        "mediaPath": str(self.base_dir / "videoFile" / "clip.mp4"),
                        "mediaPublicUrl": "https://cdn.example.com/clip.mp4",
                        "title": "clip",
                        "message": "Tweet copy",
                        "metadata": {"mediaKind": "video"},
                    }
                ],
            },
        )

        calendar = get_publish_calendar_entries(
            self.db_path,
            "2026-05-01T00:00:00",
            "2026-05-03T00:00:00",
        )

        self.assertEqual(len(calendar["items"]), 2)
        self.assertEqual(calendar["items"][0]["date"], "2026-05-01")
        self.assertEqual(calendar["items"][1]["date"], "2026-05-02")


if __name__ == "__main__":
    unittest.main()
