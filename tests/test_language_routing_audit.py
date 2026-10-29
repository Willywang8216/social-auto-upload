"""Language routing must not be decided by STT failure text, and must be auditable.

Two bugs put English videos on the operator's Chinese accounts (2026-10-27):

  * STT refusals stored as transcripts ("The request was rejected because it was
    considered high risk", "这段音频纯为器乐演奏…") — an English refusal made
    Chinese content look English and a Chinese one made English content look
    Chinese, flipping the routing decision in both directions;
  * submissions that passed ``includeZhForEnglish: true``, which the router
    honours by design but which the operator did not want.

The router's own rule is covered by tests/test_language_routing.py; these tests
pin the transcript hygiene and the audit that makes a misroute visible.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myUtils import llm_client, routing_audit  # noqa: E402


class TranscriptionFailureTests(unittest.TestCase):
    ENGLISH_REFUSAL = "The request was rejected because it was considered high risk"
    CHINESE_REFUSAL = (
        "这段音频纯为器乐演奏，没有任何人类说话的声音。因此，无法进行语音转录。LANGUAGE=en"
    )

    def test_refusals_are_recognised(self):
        self.assertTrue(llm_client.is_transcription_failure(self.ENGLISH_REFUSAL))
        self.assertTrue(llm_client.is_transcription_failure(self.CHINESE_REFUSAL))

    def test_real_speech_is_not_a_failure(self):
        self.assertFalse(llm_client.is_transcription_failure("Hey guys, it's me, Will."))
        self.assertFalse(llm_client.is_transcription_failure("大家好，今天想聊自然主義"))
        self.assertFalse(llm_client.is_transcription_failure(""))
        self.assertFalse(llm_client.is_transcription_failure(None))

    def test_usable_transcript_drops_refusals(self):
        self.assertEqual(llm_client.usable_transcript(self.ENGLISH_REFUSAL), "")
        self.assertEqual(llm_client.usable_transcript(self.CHINESE_REFUSAL), "")
        self.assertEqual(llm_client.usable_transcript("  hello  "), "hello")

    def test_detect_language_ignores_the_refusal_body(self):
        # A Chinese refusal on English media (and vice versa) must not decide the
        # routing: the refusal is metadata, not content.
        self.assertEqual(llm_client.detect_language(self.CHINESE_REFUSAL), "en")
        self.assertEqual(llm_client.detect_language("大家好，今天想聊自然主義"), "zh")


class RoutingAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE accounts (id INTEGER PRIMARY KEY, account_name TEXT, platform TEXT,
                                   config_json TEXT);
            CREATE TABLE file_records (id INTEGER PRIMARY KEY, filename TEXT);
            CREATE TABLE media_groups (id INTEGER PRIMARY KEY);
            CREATE TABLE media_group_items (media_group_id INTEGER, file_record_id INTEGER);
            CREATE TABLE campaigns (id INTEGER PRIMARY KEY, media_group_id INTEGER, metadata_json TEXT);
            CREATE TABLE campaign_artifacts (id INTEGER PRIMARY KEY, campaign_id INTEGER,
                                             artifact_kind TEXT, local_path TEXT, metadata_json TEXT);
            CREATE TABLE campaign_posts (id INTEGER PRIMARY KEY, campaign_id INTEGER, draft_json TEXT);
            CREATE TABLE publish_job_targets (id INTEGER PRIMARY KEY, file_ref TEXT, account_ref TEXT,
                                              status TEXT, schedule_at TEXT);
            """
        )
        # account 1: Chinese audience, account 2: English audience
        self.conn.execute(
            "INSERT INTO accounts VALUES (1, '光光', 'twitter', '{\"audience_language\": \"zh-Hant\"}')")
        self.conn.execute(
            "INSERT INTO accounts VALUES (2, 'model_will', 'twitter', '{\"audience_language\": \"en\"}')")

    def _campaign(self, cid: int, *, filename: str, transcript: str, title: str = "") -> None:
        self.conn.execute("INSERT INTO file_records VALUES (?, ?)", (cid, filename))
        self.conn.execute("INSERT INTO media_groups VALUES (?)", (cid,))
        self.conn.execute("INSERT INTO media_group_items VALUES (?, ?)", (cid, cid))
        self.conn.execute(
            "INSERT INTO campaigns VALUES (?, ?, ?)",
            (cid, cid, '{"title": "%s"}' % title),
        )
        if transcript is not None:
            self.conn.execute(
                "INSERT INTO campaign_artifacts VALUES (?, ?, 'transcript', '', ?)",
                (cid, cid, '{"text": %s}' % _json_str(transcript)),
            )
        self.conn.execute(
            "INSERT INTO campaign_posts VALUES (?, ?, '{}')", (cid, cid)
        )

    def _target(self, tid: int, cid: int, account: int) -> None:
        self.conn.execute(
            "INSERT INTO publish_job_targets VALUES (?, ?, ?, 'pending', '2026-11-01T13:00:00')",
            (tid, f"campaign_post:{cid}", f"account:{account}"),
        )

    def test_english_video_queued_for_a_chinese_account_is_a_violation(self):
        self._campaign(10, filename="clip__sfw.mp4",
                       transcript="Hey guys, it's me, Will, talking about naturism.")
        self._target(1, 10, account=1)
        violations = routing_audit.pending_violations(self.conn)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["account_name"], "光光")
        self.assertEqual(violations[0]["media_kind"], "video")
        self.assertEqual(violations[0]["content_language"], "en")

    def test_the_same_english_video_on_the_english_account_is_fine(self):
        self._campaign(11, filename="clip__sfw.mp4", transcript="Hey guys, it's me, Will.")
        self._target(2, 11, account=2)
        self.assertEqual(routing_audit.pending_violations(self.conn), [])

    def test_a_refusal_transcript_does_not_create_a_false_violation(self):
        """The refusal is English text; the title says the media is Chinese.

        The filename is deliberately language-neutral (a date): a *real* word in
        the name is stronger evidence than the generated title and would decide
        instead. What is under test is that the refusal text is not read as a
        transcript at all.
        """
        self._campaign(
            12, filename="20260901__nsfw.mp4",
            transcript="The request was rejected because it was considered high risk",
            title="月底遊行邀請",
        )
        self._target(3, 12, account=1)  # Chinese account
        self.assertEqual(routing_audit.pending_violations(self.conn), [])

    def test_chinese_content_on_an_english_account_is_a_violation(self):
        self._campaign(13, filename="clip__sfw.mp4", transcript="大家好，今天想聊自然主義")
        self._target(4, 13, account=2)
        violations = routing_audit.pending_violations(self.conn)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["content_language"], "zh")

    def test_unknown_language_is_never_a_violation(self):
        # A timestamp is not a language: nothing here says what the media is.
        self._campaign(14, filename="20260901__sfw.mp4", transcript="", title="")
        self._target(5, 14, account=1)
        self.assertEqual(routing_audit.pending_violations(self.conn), [])

    def test_unclassified_account_is_never_a_violation(self):
        self.conn.execute("INSERT INTO accounts VALUES (3, 'misc', 'twitter', '{}')")
        self._campaign(15, filename="clip__sfw.mp4", transcript="Hey guys.")
        self._target(6, 15, account=3)
        self.assertEqual(routing_audit.pending_violations(self.conn), [])


def _json_str(value: str) -> str:
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
