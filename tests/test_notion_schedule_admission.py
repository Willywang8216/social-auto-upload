"""Notion importer consumes the shared calendar and never claims queue delivery."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from myUtils import jobs, notion_posting, profiles
from tools import notion_schedule


def test_importer_queue_does_not_mark_notion_published(monkeypatch, tmp_path):
    accessor = Mock()
    accessor.fetch_pending_rows.return_value = [{"page_id": "offline-page", "title": "Approved title", "content": "Approved body"}]
    monkeypatch.setattr(notion_schedule, "NotionAccessor", lambda: accessor)
    monkeypatch.setattr(notion_schedule, "_existing_page_job", lambda *args: None)
    monkeypatch.setattr(notion_schedule, "_booked_by_account", lambda *args: {})
    enqueue = Mock(return_value={"jobId": 42, "targets": 1})
    monkeypatch.setattr(notion_schedule, "_enqueue", enqueue)
    assert notion_schedule.run(["nw"], publish=True, dry_run=False, db_path=tmp_path / "isolated.db") == 0
    enqueue.assert_called_once()
    accessor.mark_published.assert_not_called()


def test_importer_existing_unknown_job_is_not_requeued(monkeypatch, tmp_path):
    accessor = Mock()
    accessor.fetch_pending_rows.return_value = [{"page_id": "offline-page", "title": "Approved title", "content": "Approved body"}]
    monkeypatch.setattr(notion_schedule, "NotionAccessor", lambda: accessor)
    monkeypatch.setattr(notion_schedule, "_existing_page_job", lambda *args: SimpleNamespace(id=42, status="needs_review"))
    monkeypatch.setattr(notion_schedule, "_booked_by_account", lambda *args: {})
    enqueue = Mock(side_effect=AssertionError("unknown job was resubmitted"))
    monkeypatch.setattr(notion_schedule, "_enqueue", enqueue)
    assert notion_schedule.run(["nw"], publish=True, dry_run=False, db_path=tmp_path / "isolated.db") == 2
    enqueue.assert_not_called()
    accessor.mark_published.assert_not_called()


def test_importer_missing_database_is_not_an_empty_calendar(tmp_path):
    with pytest.raises(FileNotFoundError):
        notion_schedule._booked_by_account(tmp_path / "missing.db")


@pytest.mark.parametrize("status", ["pending", "awaiting_delivery", "needs_review", "failed", "succeeded"])
def test_importer_existing_page_keeps_history_and_checks_approved_intent(monkeypatch, status):
    row = {"page_id": "offline-page", "title": "Approved title", "content": "Approved body", "brand": "nw"}
    job = SimpleNamespace(id=42, status=status, platform=notion_posting.BLOG_PLATFORM, profile_id=1,
                          payload={"notionPageId": "offline-page", "message": "Approved body", "title": "Approved title"})
    monkeypatch.setattr(jobs, "get_job_by_idempotency_key", lambda *args, **kw: job)
    monkeypatch.setattr(jobs, "list_targets", lambda *args, **kw: [SimpleNamespace(account_ref="account:112", file_ref="notion:offline-page")])
    monkeypatch.setattr(profiles, "get_account", lambda *args, **kw: SimpleNamespace(profile_id=1))
    assert notion_schedule._existing_page_job(row, None) is job
    with pytest.raises(ValueError, match="approved content"):
        notion_schedule._existing_page_job({**row, "content": "Changed copy"}, None)


def test_importer_enqueue_passes_exact_workspace(monkeypatch, tmp_path):
    slot = notion_posting.Slot("offline-page", "Approved title", "nw", 112, 1, "2035-01-02T13:00:00")
    monkeypatch.setattr(profiles, "get_account", lambda *args, **kw: SimpleNamespace(profile_id=1, platform=notion_posting.BLOG_PLATFORM, workspace_id="offline-tenant"))
    enqueue = Mock(return_value=SimpleNamespace(id=42, total_targets=1))
    monkeypatch.setattr(jobs, "enqueue_job", enqueue)
    assert notion_schedule._enqueue(slot, "Approved body", tmp_path / "isolated.db")["jobId"] == 42
    assert enqueue.call_args.kwargs["workspace_id"] == "offline-tenant"
    assert enqueue.call_args.args[0].payload["message"] == "Approved body"
