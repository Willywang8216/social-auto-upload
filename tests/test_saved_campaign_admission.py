"""Saved-campaign and low-level entry points use real admission and atomic SQL."""
from datetime import timedelta

import pytest

from tests.test_atomic_publication_groups import setup, schema, NOW, _snapshot
from myUtils import campaigns, destination_pacing, jobs, profiles, publication_groups, publish_readiness, publish_orchestrator as po


@pytest.fixture(autouse=True)
def text_route_proof(setup, monkeypatch):
    # Read-only provider proof mock also covers the standalone approved text tests.
    monkeypatch.setattr(publish_readiness, "_project", lambda *a, **k: {
        "ready": True, "reasonCode": "ready", "evidence": {"capabilities": ["text", "video", "image", "carousel", "first_comment"]}})


def saved_case(setup, count=4):
    case = setup.make(count=count)
    case.campaign = campaigns.update_campaign(case.campaign.id, status="prepared", db_path=setup.db)
    for index in range(1, count + 1):
        campaigns.add_campaign_post(case.campaign.id, case.account.platform, account_ids=[case.account.id],
            draft={"message": f"Approved part {index}.", "hashtags": []}, file_record_ids=[case.fid] if count > 1 else None,
            sheet_row={"caption": f"Original sheet {index}"}, status="ready", db_path=setup.db)
    return case


def publish(setup, case):
    return publication_groups.publish_saved_campaign(case.campaign, db_path=setup.db,
        load_media_files=po._load_media_group_files_via_callback,
        artifact_payloads_for_platform=lambda values, platform: values,
        job_to_payload=lambda job: job.to_dict(), now_fn=lambda: NOW)


def test_saved_campaign_keeps_each_draft_complete_order_and_original_links(setup):
    case = saved_case(setup)
    before_posts = campaigns.list_campaign_posts(case.campaign.id, db_path=setup.db)
    result = publish(setup, case)
    assert len(result["jobs"]) == 4
    posts = campaigns.list_campaign_posts(case.campaign.id, db_path=setup.db)
    assert [post.id for post in posts] == [post.id for post in before_posts]
    assert [post.draft for post in posts] == [post.draft for post in before_posts]
    queued = [jobs.get_job(post.last_published_job_id, db_path=setup.db) for post in posts]
    assert [job.payload["artifacts"][0]["metadata"]["part_index"] for job in queued] == [1, 2, 3, 4]
    slots = [destination_pacing.utc_datetime(jobs.list_targets(job.id, db_path=setup.db)[0].schedule_at) for job in queued]
    assert slots[0] == NOW and slots[3].date() > slots[0].date()
    assert all(right - left >= timedelta(minutes=30) for left, right in zip(slots, slots[1:]))
    before = _snapshot(setup)
    assert publish(setup, case) == result
    assert _snapshot(setup) == before


@pytest.mark.parametrize("index", [1, 2, 3, 4])
def test_saved_campaign_sql_failure_rolls_back_jobs_targets_and_links(setup, index):
    case = saved_case(setup)
    before = _snapshot(setup)
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute(f"CREATE TRIGGER saved_fault BEFORE INSERT ON publish_jobs WHEN (SELECT COUNT(*) FROM publish_jobs)={index - 1} BEGIN SELECT RAISE(ABORT,'saved injected fault'); END")
    with pytest.raises(Exception, match="saved injected fault"):
        publish(setup, case)
    assert _snapshot(setup) == before


def test_saved_campaign_missing_draft_tail_refuses_without_writes(setup):
    case = saved_case(setup)
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("DELETE FROM campaign_posts WHERE id=(SELECT MAX(id) FROM campaign_posts)")
    before = _snapshot(setup)
    with pytest.raises(publication_groups.PublicationGroupConflict, match="complete ordered"):
        publish(setup, case)
    assert _snapshot(setup) == before


def test_saved_campaign_replay_does_not_reset_unknown_history_when_account_disabled(setup):
    case = saved_case(setup, count=1)
    result = publish(setup, case)
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("UPDATE accounts SET enabled=0")
        conn.execute("UPDATE publish_job_targets SET status='needs_review',attempts=2,checkpoint_json=?", ('{"submission_started":true}',))
    before = _snapshot(setup)
    again = publish(setup, case)
    assert [job["id"] for job in again["jobs"]] == [job["id"] for job in result["jobs"]]
    assert _snapshot(setup) == before


def test_saved_campaign_changed_approved_caption_after_queue_refuses(setup):
    case = saved_case(setup, count=1)
    publish(setup, case)
    with campaigns._connect(setup.db) as conn, conn:
        conn.execute("UPDATE campaign_posts SET draft_json=?", ('{"message":"changed approval"}',))
    before = _snapshot(setup)
    with pytest.raises(publication_groups.PublicationGroupConflict, match="intent conflict"):
        publish(setup, case)
    assert _snapshot(setup) == before


def ready_spec(setup, *, minutes=0, key="fixed-1", account=None):
    account = account or setup.account
    return jobs.JobSpec(platform=account.platform, profile_id=account.profile_id,
        payload={"message": "Exact approved text.", "artifacts": []},
        targets=[(f"account:{account.id}", "text-only", NOW + timedelta(minutes=minutes))], idempotency_key=key)


def test_ready_standalone_collision_rejected_but_identical_replay_uses_original_job(setup):
    spec = ready_spec(setup)
    first = jobs.enqueue_job(spec, db_path=setup.db)
    before = _snapshot(setup)
    assert jobs.enqueue_job(spec, db_path=setup.db).id == first.id
    assert _snapshot(setup) == before
    with pytest.raises(destination_pacing.PacingRefused, match="shared gap"):
        jobs.enqueue_job(ready_spec(setup, minutes=29, key="collision"), db_path=setup.db)
    assert _snapshot(setup) == before


def test_ready_standalone_daily_cap_cannot_be_bypassed_by_lowlevel_entry(setup):
    for index in range(3):
        jobs.enqueue_job(ready_spec(setup, minutes=30 * index, key=f"daily-{index}"), db_path=setup.db)
    before = _snapshot(setup)
    with pytest.raises(destination_pacing.PacingRefused, match="daily cap"):
        jobs.enqueue_job(ready_spec(setup, minutes=90, key="daily-4"), db_path=setup.db)
    assert _snapshot(setup) == before


def test_ready_standalone_alias_is_the_same_destination(setup):
    alias = profiles.add_account(setup.profile.id, "twitter", "fixture-alias", auth_type="oauth",
        config=setup.account.config, db_path=setup.db)
    jobs.enqueue_job(ready_spec(setup), db_path=setup.db)
    before = _snapshot(setup)
    with pytest.raises(destination_pacing.PacingRefused):
        jobs.enqueue_job(ready_spec(setup, minutes=10, key="alias-collision", account=alias), db_path=setup.db)
    assert _snapshot(setup) == before
