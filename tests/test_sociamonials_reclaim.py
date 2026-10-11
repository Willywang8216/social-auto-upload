"""Tests for the progressive Sociamonials reclaim policy.

All tests inject a fake HTTP session and a temporary SQLite file; nothing here
talks to the live API or the real database.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from myUtils import sociamonials_fallback as sm
from myUtils import sociamonials_reclaim as reclaim


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class StorageSession:
    """A session with a mutable storage block and a library of assets."""

    def __init__(self, *, storage, assets, delete_results=None):
        self.storage = dict(storage)
        self.assets = list(assets)
        self.delete_results = dict(delete_results or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "/workspaces/" in url and url.rstrip("/").endswith("/context"):
            return FakeResponse(200, {"data": {"storage": dict(self.storage)}})
        if url.rstrip("/").endswith("/media/assets"):
            params = kwargs.get("params", {})
            limit = params.get("limit", 200)
            offset = params.get("offset", 0)
            page = self.assets[offset : offset + limit]
            return FakeResponse(200, {"assets": page, "total": len(self.assets)})
        raise AssertionError(f"unexpected GET {url}")

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        asset_id = int(url.rstrip("/").rsplit("/", 1)[-1])
        result = self.delete_results.get(asset_id)
        if result is not None:
            return FakeResponse(result[0], result[1])
        freed = 0
        remaining = []
        for asset in self.assets:
            if int(asset.get("asset_id")) == asset_id:
                freed += int(asset.get("size_bytes") or 0)
            else:
                remaining.append(asset)
        self.assets = remaining
        # Simulate the platform freeing the bytes against the allowance.
        if self.storage:
            self.storage["usage_bytes"] = max(
                0, int(self.storage.get("usage_bytes") or 0) - freed
            )
            allowance = int(self.storage.get("allowance_bytes") or 0)
            self.storage["available_bytes"] = max(0, allowance - self.storage["usage_bytes"])
        return FakeResponse(204, None)

    def deletes(self):
        return [c for c in self.calls if c[0] == "DELETE"]


class FlowSession(StorageSession):
    """StorageSession plus the three-step upload and post flow for the fallback."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.uploads = 0

    def get(self, url, **kwargs):
        if "/media/assets/" in url and not url.rstrip("/").endswith("/media/assets"):
            self.calls.append(("GET", url, kwargs))
            return FakeResponse(200, {"asset_id": 77, "processing_status": "ready"})
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/media/uploads"):
            self.uploads += 1
            return FakeResponse(
                200, {"upload_id": 5, "mode": "single", "upload_url": "https://u/x"}
            )
        if url.endswith("/complete"):
            return FakeResponse(200, {"asset_id": 77})
        raise AssertionError(f"unexpected POST {url}")

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return FakeResponse(200)


def _asset(asset_id, filename, size_bytes, *, created="2026-10-05 00:00:00", **extra):
    row = {
        "asset_id": asset_id,
        "filename": filename,
        "size_bytes": size_bytes,
        "media_type": "video" if filename.endswith(".mp4") else "image",
        "processing_status": "ready",
        "starred": False,
        "sent_count": 1,
        "created_at": created,
    }
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def _no_safety_margin(monkeypatch):
    """Zero the 32 MB safety margin so the byte arithmetic in tests is exact."""
    monkeypatch.setenv(reclaim.SAFETY_MB_ENV, "0")
    monkeypatch.delenv(reclaim.GRACE_ENV, raising=False)
    yield


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE publish_jobs (id INTEGER PRIMARY KEY, platform TEXT, payload_json TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE publish_job_targets (
                id INTEGER PRIMARY KEY, job_id INTEGER, account_ref TEXT,
                file_ref TEXT, status TEXT
            )
            """
        )
        conn.commit()
    return path


def _add_target(db_path, *, target_id, job_id, status, filename, file_ref="campaign_post:1"):
    payload = {"artifacts": [{"local_path": f"/app/generated/{filename}", "public_url": ""}]}
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO publish_jobs (id, platform, payload_json) VALUES (?, 'twitter', ?)",
            (job_id, json.dumps(payload)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO publish_job_targets "
            "(id, job_id, account_ref, file_ref, status) VALUES (?, ?, 'account:1', ?, ?)",
            (target_id, job_id, file_ref, status),
        )
        conn.commit()


def _storage(usage, allowance, *, status="ok"):
    return {
        "status": status,
        "usage_bytes": usage,
        "allowance_bytes": allowance,
        "available_bytes": allowance - usage,
        "percent_used": round(usage / allowance * 100, 2) if allowance else None,
    }


# --------------------------------------------------------------------------- #
# Filename / history helpers
# --------------------------------------------------------------------------- #

def test_basename_handles_paths_urls_and_encoding():
    assert reclaim.basename("/app/generated/a/video.mp4") == "video.mp4"
    assert reclaim.basename("https://cdn.example/a/b/clip%20one.mp4?x=1") == "clip one.mp4"
    assert reclaim.basename("") == ""
    assert reclaim.basename("plain.mp4") == "plain.mp4"


def test_artifact_filenames_reads_every_path_field():
    payload = {
        "artifacts": [
            {"local_path": "/a/x.mp4", "public_url": "https://cdn/y/x.mp4"},
            {"remote_path": "campaigns/9/videos/z.mp4"},
            "not-a-dict",
        ]
    }
    assert reclaim.artifact_filenames(payload) == {"x.mp4", "z.mp4"}


def test_target_history_partitions_live_and_delivered(db_path):
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="delivered.mp4")
    _add_target(db_path, target_id=2, job_id=2, status="pending", filename="queued.mp4")
    _add_target(db_path, target_id=3, job_id=3, status="running", filename="running.mp4")
    _add_target(db_path, target_id=4, job_id=4, status="failed", filename="dead.mp4")

    live = reclaim.live_filenames(db_path)
    delivered = reclaim.delivered_filenames(db_path)
    assert set(live) == {"queued.mp4", "running.mp4"}
    assert set(delivered) == {"delivered.mp4"}


def test_malformed_payload_does_not_break_history(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO publish_jobs VALUES (1, 'twitter', 'not json')")
        conn.execute(
            "INSERT INTO publish_job_targets VALUES (1, 1, 'account:1', 'campaign_post:1', 'succeeded')"
        )
        conn.commit()
    assert reclaim.delivered_filenames(db_path) == {}


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def test_plan_reclaims_delivered_asset_with_no_live_target():
    assets = [_asset(1, "a.mp4", 100)]
    plan = reclaim.plan_reclaim(
        assets, live={}, delivered={"a.mp4": {7}}, delivered_asset_ids={}
    )
    assert [entry["asset"]["asset_id"] for entry in plan] == [1]


def test_plan_keeps_asset_a_live_target_needs():
    assets = [_asset(1, "a.mp4", 100)]
    plan = reclaim.plan_reclaim(
        assets, live={"a.mp4": {9}}, delivered={"a.mp4": {7}}, delivered_asset_ids={}
    )
    assert plan == []


def test_plan_keeps_an_asset_whose_post_never_delivered():
    assets = [_asset(1, "a.mp4", 100)]
    plan = reclaim.plan_reclaim(assets, live={}, delivered={}, delivered_asset_ids={})
    assert plan == []


def test_plan_refuses_a_delivered_filename_the_asset_was_never_sent_with():
    # A succeeded target used the name, but this asset was never attached to a
    # post (sent_count 0): it cannot be proven delivered.
    assets = [_asset(1, "a.mp4", 100, sent_count=0)]
    plan = reclaim.plan_reclaim(
        assets, live={}, delivered={"a.mp4": {7}}, delivered_asset_ids={}
    )
    assert plan == []


def test_plan_uses_the_delivered_ledger_even_without_target_history():
    assets = [_asset(1, "a.mp4", 100, sent_count=0)]
    plan = reclaim.plan_reclaim(
        assets,
        live={},
        delivered={},
        delivered_asset_ids={1: "2026-10-01 00:00:00"},
    )
    assert [entry["asset"]["asset_id"] for entry in plan] == [1]


def test_plan_never_deletes_a_starred_asset():
    assets = [_asset(1, "a.mp4", 100, starred=True)]
    plan = reclaim.plan_reclaim(
        assets, live={}, delivered={"a.mp4": {7}}, delivered_asset_ids={1: "2026-10-01 00:00:00"}
    )
    assert plan == []


def test_plan_grace_holds_back_a_recent_delivery():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    assets = [_asset(1, "a.mp4", 100)]
    plan = reclaim.plan_reclaim(
        assets,
        live={},
        delivered={"a.mp4": {7}},
        delivered_asset_ids={1: "2026-10-11 11:59:00"},
        now=now,
        grace_seconds=600,
    )
    assert plan == []
    old = reclaim.plan_reclaim(
        assets,
        live={},
        delivered={"a.mp4": {7}},
        delivered_asset_ids={1: "2026-10-11 11:00:00"},
        now=now,
        grace_seconds=600,
    )
    assert [entry["asset"]["asset_id"] for entry in old] == [1]


def test_plan_orders_largest_first():
    assets = [_asset(1, "small.mp4", 10), _asset(2, "big.mp4", 900)]
    plan = reclaim.plan_reclaim(
        assets,
        live={},
        delivered={"small.mp4": {1}, "big.mp4": {1}},
        delivered_asset_ids={},
    )
    assert [entry["asset"]["asset_id"] for entry in plan] == [2, 1]


def test_partition_reasons_separates_keep_buckets():
    assets = [
        _asset(1, "live.mp4", 100),
        _asset(2, "star.mp4", 100, starred=True),
        _asset(3, "done.mp4", 100),
        _asset(4, "fail.mp4", 100, sent_count=0),
        _asset(5, "unknown.mp4", 100),
    ]
    counts = reclaim.partition_reasons(
        assets,
        live={"live.mp4": {1}},
        delivered={"done.mp4": {1}, "fail.mp4": {1}},
        delivered_asset_ids={},
    )
    assert counts["live_target"] == 1
    assert counts["starred"] == 1
    assert counts["reclaimable"] == 1
    assert counts["not_delivered"] == 1
    assert counts["unmatched"] == 1


# --------------------------------------------------------------------------- #
# Delivered ledger
# --------------------------------------------------------------------------- #

def test_record_and_load_delivered_assets_is_idempotent(db_path):
    written = reclaim.record_delivered_assets(
        db_path,
        [{"asset_id": 5, "filename": "a.mp4", "size_bytes": 10, "media_type": "video"}],
        workspace_id=26985,
        job_id=1,
        target_id=2,
        post_id=99,
        network="tw",
    )
    assert written == 1
    again = reclaim.record_delivered_assets(
        db_path,
        [{"asset_id": 5, "filename": "a.mp4", "size_bytes": 11, "media_type": "video"}],
        workspace_id=26985,
        target_id=3,
    )
    assert again == 1
    ledger = reclaim.load_delivered_assets(db_path)
    assert list(ledger) == [5]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT filename, size_bytes, target_id FROM sociamonials_delivered_assets"
        ).fetchall()
    assert rows == [("a.mp4", 11, 3)]


def test_record_delivered_assets_ignores_rows_without_an_id(db_path):
    assert reclaim.record_delivered_assets(db_path, [{"filename": "x.mp4"}]) == 0


# --------------------------------------------------------------------------- #
# Reclaim orchestration
# --------------------------------------------------------------------------- #

def test_reclaim_dry_run_plans_but_deletes_nothing(db_path):
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="done.mp4")
    assets = [_asset(10, "done.mp4", 500)]
    session = StorageSession(storage=_storage(500, 1000), assets=assets)
    summary = reclaim.reclaim(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        apply=False,
    )
    assert summary["planned"] == 1
    assert summary["reclaim_bytes"] == 500
    assert summary["deleted"] == 0
    assert session.deletes() == []


def test_reclaim_apply_deletes_only_everything_eligible(db_path):
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="done.mp4")
    _add_target(db_path, target_id=2, job_id=2, status="pending", filename="live.mp4")
    assets = [
        _asset(10, "done.mp4", 500),
        _asset(11, "live.mp4", 400),
        _asset(12, "unknown.mp4", 300),
    ]
    session = StorageSession(storage=_storage(500, 1000), assets=assets)
    summary = reclaim.reclaim(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        apply=True,
    )
    assert summary["deleted"] == 1
    assert [int(c[1].rsplit("/", 1)[-1]) for c in session.deletes()] == [10]


def test_reclaim_max_bytes_bounds_a_single_run(db_path):
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="a.mp4")
    _add_target(db_path, target_id=2, job_id=2, status="succeeded", filename="b.mp4")
    assets = [_asset(10, "a.mp4", 500), _asset(11, "b.mp4", 400)]
    session = StorageSession(storage=_storage(500, 1000), assets=assets)
    summary = reclaim.reclaim(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        apply=True,
        max_bytes=600,
    )
    assert summary["deleted"] == 1
    assert [int(c[1].rsplit("/", 1)[-1]) for c in session.deletes()] == [10]


def test_reclaim_marks_the_ledger_row_as_reclaimed(db_path):
    reclaim.record_delivered_assets(
        db_path,
        [{"asset_id": 10, "filename": "done.mp4", "size_bytes": 500, "media_type": "video"}],
        workspace_id=26985,
    )
    assets = [_asset(10, "done.mp4", 500)]
    session = StorageSession(storage=_storage(500, 1000), assets=assets)
    summary = reclaim.reclaim(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        apply=True,
    )
    assert summary["deleted"] == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT reclaimed_at FROM sociamonials_delivered_assets WHERE asset_id=10"
        ).fetchone()
    assert row and row[0]


def test_reclaim_apply_reports_a_failed_delete(db_path):
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="done.mp4")
    assets = [_asset(10, "done.mp4", 500)]
    session = StorageSession(
        storage=_storage(500, 1000),
        assets=assets,
        delete_results={10: (422, {"error": {"code": "asset_in_use"}})},
    )
    summary = reclaim.reclaim(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        apply=True,
    )
    assert summary["deleted"] == 0
    assert summary["failed"] == 1
    assert summary["failures"][0]["reason"] == "asset_in_use"


# --------------------------------------------------------------------------- #
# Quota guard
# --------------------------------------------------------------------------- #

def test_ensure_headroom_passes_when_the_file_fits(db_path):
    session = StorageSession(storage=_storage(300, 1000), assets=[])
    result = reclaim.ensure_headroom(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        size_bytes=100,
    )
    assert result["checked"] is True
    assert result["reclaimed"] == 0
    assert session.deletes() == []


def test_ensure_headroom_reclaims_and_then_fits(db_path):
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="old.mp4")
    assets = [_asset(10, "old.mp4", 600)]
    session = StorageSession(storage=_storage(900, 1000), assets=assets)
    # available is 100; the file is 200 + 32 safety = 232. The one reclaimable
    # asset frees 600, so the pre-upload sweep makes room.
    result = reclaim.ensure_headroom(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        size_bytes=200,
    )
    assert result["checked"] is True
    assert result["reclaimed"] == 1
    assert len(session.deletes()) == 1


def test_ensure_headroom_raises_a_clear_error_when_it_still_cannot_fit(db_path):
    # Nothing reclaimable and not enough room.
    session = StorageSession(storage=_storage(980, 1000), assets=[_asset(10, "x.mp4", 600)])
    with pytest.raises(sm.SociamonialsFallbackError) as excinfo:
        reclaim.ensure_headroom(
            session=session,
            headers={},
            workspace_id=26985,
            db_path=db_path,
            size_bytes=200,
        )
    message = str(excinfo.value)
    assert "storage is full" in message
    assert "needed=" in message
    assert "Free space with scripts/sociamonials_reclaim.py" in message


def test_ensure_headroom_skips_when_storage_is_unavailable(db_path):
    session = StorageSession(storage=_storage(0, 0, status="unavailable"), assets=[])
    result = reclaim.ensure_headroom(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        size_bytes=200,
    )
    assert result["checked"] is False


def test_ensure_headroom_skips_without_a_database():
    result = reclaim.ensure_headroom(
        session=StorageSession(storage=_storage(0, 1000), assets=[]),
        headers={},
        workspace_id=26985,
        db_path=None,
        size_bytes=200,
    )
    assert result["checked"] is False


def test_ensure_headroom_holds_back_a_recent_delivery(db_path):
    # The pre-upload sweep uses a 10-minute default grace, so a just-delivered
    # asset is not freed out from under a platform that may refetch it.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="fresh.mp4")
    assets = [_asset(10, "fresh.mp4", 600, created=now, last_sent_at=now)]
    session = StorageSession(storage=_storage(980, 1000), assets=assets)
    with pytest.raises(sm.SociamonialsFallbackError):
        reclaim.ensure_headroom(
            session=session,
            headers={},
            workspace_id=26985,
            db_path=db_path,
            size_bytes=200,
        )
    assert session.deletes() == []


def test_ensure_headroom_proceeds_when_storage_disappears_after_reclaim(db_path):
    _add_target(db_path, target_id=1, job_id=1, status="succeeded", filename="old.mp4")

    class _Flip(StorageSession):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.deleted = False

        def get(self, url, **kwargs):
            if url.rstrip("/").endswith("/context") and self.deleted:
                self.calls.append(("GET", url, kwargs))
                return FakeResponse(
                    200,
                    {"data": {"storage": {"status": "unavailable", "available_bytes": None}}},
                )
            return super().get(url, **kwargs)

        def delete(self, url, **kwargs):
            self.deleted = True
            return super().delete(url, **kwargs)

    session = _Flip(storage=_storage(900, 1000), assets=[_asset(10, "old.mp4", 600)])
    result = reclaim.ensure_headroom(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        size_bytes=200,
    )
    assert result["checked"] is True
    assert result["reclaimed"] == 1


def test_ensure_headroom_survives_a_failed_reclaim(db_path):
    class _NoLibrary(StorageSession):
        def get(self, url, **kwargs):
            if url.rstrip("/").endswith("/media/assets"):
                self.calls.append(("GET", url, kwargs))
                return FakeResponse(403, {"error": {"code": "asset_access_not_enabled"}})
            return super().get(url, **kwargs)

    session = _NoLibrary(storage=_storage(980, 1000), assets=[])
    result = reclaim.ensure_headroom(
        session=session,
        headers={},
        workspace_id=26985,
        db_path=db_path,
        size_bytes=200,
    )
    # A failed sweep must not raise: the upload is attempted and the platform
    # remains the authority on whether the bytes fit.
    assert result["checked"] is True
    assert result["reclaimed"] == 0
    assert result["reclaim_error"]


def test_collect_used_assets_parses_a_reference(tmp_path):
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x" * 7)
    entry = reclaim.collect_used_assets("asset://42", local_path=local, kind="video")
    assert entry == {
        "asset_id": 42,
        "filename": "clip.mp4",
        "size_bytes": 7,
        "media_type": "video",
    }
    assert reclaim.collect_used_assets("https://cdn/x.mp4") is None


# --------------------------------------------------------------------------- #
# Fallback integration: the guard runs before an upload and the ids are returned
# --------------------------------------------------------------------------- #

def test_fallback_reclaims_before_upload_and_returns_used_assets(tmp_path):
    db_path = tmp_path / "int.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE publish_jobs (id INTEGER PRIMARY KEY, platform TEXT, payload_json TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE publish_job_targets (
                id INTEGER PRIMARY KEY, job_id INTEGER, account_ref TEXT,
                file_ref TEXT, status TEXT
            )
            """
        )
        delivered = {"artifacts": [{"local_path": "/app/generated/old.mp4"}]}
        conn.execute(
            "INSERT INTO publish_jobs VALUES (1, 'twitter', ?)", (json.dumps(delivered),)
        )
        conn.execute(
            "INSERT INTO publish_job_targets VALUES (1, 1, 'account:1', 'campaign_post:1', 'succeeded')"
        )
        conn.commit()

    session = FlowSession(storage=_storage(995, 1000), assets=[_asset(10, "old.mp4", 500)])
    local = tmp_path / "new.mp4"
    local.write_bytes(b"y" * 10)
    used: list[dict] = []
    reference = sm._resolve_local_media(
        session,
        {"Authorization": "Bearer test"},
        "26985",
        local,
        timeout=1.0,
        db_path=db_path,
        used_assets=used,
    )
    # The reuse lookup found nothing, so the guard reclaimed old.mp4 and the new
    # file was uploaded instead of failing on a full library.
    assert reference == "asset://77"
    assert session.uploads == 1
    deletes = [int(c[1].rsplit("/", 1)[-1]) for c in session.deletes()]
    assert deletes == [10]
    assert used and used[0]["asset_id"] == 77
