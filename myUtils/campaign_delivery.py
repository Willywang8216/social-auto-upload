"""Transactional delivery summaries derived from the authoritative queue.

Preparation is not publication. A linked post follows its current job, and a
campaign is published only after all of its linked posts complete. No provider
calls, enqueueing, or retries are performed by these projection helpers.
"""
from __future__ import annotations


def sync_campaign_delivery(conn, campaign_id: int) -> None:
    row = conn.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None or row[0] == "preparing":
        return
    posts = conn.execute(
        "SELECT status, last_published_job_id FROM campaign_posts WHERE campaign_id=?",
        (campaign_id,),
    ).fetchall()
    if not posts or not any(post[1] is not None for post in posts):
        return
    states = {post[0] for post in posts}
    if states == {"published"}:
        conn.execute(
            "UPDATE campaigns SET status='published', published_at=COALESCE(published_at, "
            "strftime('%Y-%m-%dT%H:%M:%S','now')), last_error=NULL WHERE id=?",
            (campaign_id,),
        )
        return
    if states & {"queued", "pending", "running", "retrying", "awaiting_delivery"}:
        status = "publishing"
    elif states == {"cancelled"}:
        status = "cancelled"
    elif states == {"failed"}:
        status = "failed"
    else:
        status = "needs_review"
    conn.execute("UPDATE campaigns SET status=?, published_at=NULL WHERE id=?", (status, campaign_id))


def sync_job_delivery(conn, job_id: int, job_status: str) -> None:
    posts = conn.execute(
        "SELECT id, campaign_id FROM campaign_posts WHERE last_published_job_id=?", (job_id,),
    ).fetchall()
    if not posts:
        return
    post_status = {"succeeded": "published", "pending": "queued"}.get(job_status, job_status)
    conn.execute(
        "UPDATE campaign_posts SET status=?, updated_at=CURRENT_TIMESTAMP WHERE last_published_job_id=?",
        (post_status, job_id),
    )
    for campaign_id in {post[1] for post in posts}:
        sync_campaign_delivery(conn, campaign_id)
