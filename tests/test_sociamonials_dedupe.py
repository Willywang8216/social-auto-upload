"""Tests for the Sociamonials library de-duplication utility.

All tests inject a fake HTTP session; nothing here talks to the live API.
"""

from __future__ import annotations

from scripts import sociamonials_dedupe as dedupe


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, *, assets=None, delete_results=None):
        self.assets = list(assets or [])
        self.delete_results = dict(delete_results or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        limit = kwargs.get("params", {}).get("limit", 200)
        offset = kwargs.get("params", {}).get("offset", 0)
        page = self.assets[offset : offset + limit]
        return FakeResponse(200, {"assets": page, "total": len(self.assets)})

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        asset_id = int(url.rstrip("/").rsplit("/", 1)[-1])
        result = self.delete_results.get(asset_id)
        if result is None:
            return FakeResponse(204, None)
        return FakeResponse(result[0], result[1])

    def deletes(self):
        return [c for c in self.calls if c[0] == "DELETE"]


def _asset(asset_id, filename, size_bytes, *, created="2026-10-05 00:00:00", **extra):
    row = {
        "asset_id": asset_id,
        "filename": filename,
        "size_bytes": size_bytes,
        "media_type": "video" if filename.endswith(".mp4") else "image",
        "processing_status": "ready",
        "created_at": created,
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# Grouping / keeper selection
# --------------------------------------------------------------------------- #

def test_group_duplicates_groups_by_name_and_size():
    assets = [
        _asset(1, "a.mp4", 100),
        _asset(2, "a.mp4", 100),
        _asset(3, "a.mp4", 200),  # same name, different size: not a duplicate
        _asset(4, "b.mp4", 100),  # unique
    ]
    groups = dedupe.group_duplicates(assets)
    assert set(groups) == {("a.mp4", 100)}
    assert len(groups[("a.mp4", 100)]) == 2


def test_group_duplicates_ignores_rows_without_a_usable_size():
    assets = [
        _asset(1, "a.mp4", 100),
        _asset(2, "a.mp4", 100),
        {"asset_id": 3, "filename": "a.mp4"},  # no size -> never collapsed
        {"asset_id": 4, "size_bytes": 100},  # no name
    ]
    groups = dedupe.group_duplicates(assets)
    assert list(groups) == [("a.mp4", 100)]


def test_choose_keeper_prefers_starred_then_oldest():
    group = [
        _asset(1, "a.mp4", 100, created="2026-10-03 00:00:00"),
        _asset(2, "a.mp4", 100, created="2026-10-01 00:00:00"),
        _asset(3, "a.mp4", 100, created="2026-10-05 00:00:00", starred=True),
    ]
    assert dedupe.choose_keeper(group)["asset_id"] == 3
    # With no starred copy the oldest one is kept.
    assert dedupe.choose_keeper(group[:2])["asset_id"] == 2


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

def test_build_deletion_plan_keeps_exactly_one_copy_and_counts_bytes():
    assets = [
        _asset(1, "a.mp4", 100, created="2026-10-03 00:00:00"),
        _asset(2, "a.mp4", 100, created="2026-10-01 00:00:00"),
        _asset(3, "a.mp4", 100, created="2026-10-05 00:00:00"),
        _asset(4, "b.mp4", 50),
    ]
    plan = dedupe.build_deletion_plan(assets)
    deleted_ids = {dedupe._asset_id(entry["asset"]) for entry in plan}
    assert deleted_ids == {1, 3}
    assert all(entry["keeper"]["asset_id"] == 2 for entry in plan)
    reclaim = sum(entry["asset"]["size_bytes"] for entry in plan)
    assert reclaim == 200


def test_build_deletion_plan_never_deletes_the_last_copy():
    assets = [_asset(1, "a.mp4", 100), _asset(2, "b.mp4", 100)]
    assert dedupe.build_deletion_plan(assets) == []


# --------------------------------------------------------------------------- #
# Listing / run
# --------------------------------------------------------------------------- #

def test_list_all_assets_paginates_200_at_a_time():
    assets = [_asset(i, f"f{i}.mp4", 10) for i in range(250)]
    session = FakeSession(assets=assets)
    out = dedupe.list_all_assets(session, {}, "26985", page_size=200)
    assert len(out) == 250
    assert [call[2]["params"]["offset"] for call in session.calls] == [0, 200]


def test_run_dry_run_plans_but_deletes_nothing():
    assets = [
        _asset(1, "a.mp4", 100, created="2026-10-03 00:00:00"),
        _asset(2, "a.mp4", 100, created="2026-10-01 00:00:00"),
    ]
    session = FakeSession(assets=assets)
    summary = dedupe.run(
        session=session, api_key="sm_agent_x", workspace_id="26985", apply=False
    )
    assert summary["assets"] == 2
    assert summary["planned"] == 1
    assert summary["reclaim_bytes"] == 100
    assert summary["deleted"] == 0
    assert session.deletes() == []


def test_run_apply_deletes_and_reports_failures():
    assets = [
        _asset(1, "a.mp4", 100, created="2026-10-03 00:00:00"),
        _asset(2, "a.mp4", 100, created="2026-10-01 00:00:00"),
        _asset(3, "b.jpg", 5, created="2026-10-03 00:00:00"),
        _asset(4, "b.jpg", 5, created="2026-10-01 00:00:00"),
    ]
    session = FakeSession(
        assets=assets,
        delete_results={1: (422, {"error": {"code": "asset_in_use"}})},
    )
    summary = dedupe.run(
        session=session, api_key="sm_agent_x", workspace_id="26985", apply=True
    )
    assert summary["deleted"] == 1
    assert summary["failed"] == 1
    assert summary["failures"][0]["asset_id"] == 1
    assert summary["failures"][0]["reason"] == "asset_in_use"
    assert len(session.deletes()) == 2
