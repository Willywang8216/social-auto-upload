"""Notion long-form -> SAU publish scheduling (density-aware).

The operator writes long-form pieces in two Notion databases (NW = nakedwill,
SW = sexualwill) and flips a "publish" column from 草稿 to 完成未發布 when a
piece is ready. This module turns those rows into publish slots that never
jam: at most one post per brand account per calendar day, a small stagger
between same-day posts, and never in the past.

Notion transport is deliberately NOT implemented here — callers pass rows in
and an ``enqueue``/``mark_published`` callable, so the policy is unit-testable
and the transport stays swappable (see ``tools/notion/accessors.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Publishing window: 21:00 Asia/Taipei == 13:00Z, matching the existing
# NW/SW batch slots so blog posts land in the same daily window as the
# short-form content.
DEFAULT_HOUR_UTC = 13
DEFAULT_STAGGER_MINUTES = 5
DEFAULT_MIN_HOURS_AHEAD = 1

STATUS_DRAFT = "草稿"
STATUS_FINISHED_UNPUBLISHED = "完成未發布"
STATUS_FINISHED_PUBLISHED = "完成已發布"

NW_DB_ID = "6cffcdc6586f82c79b85812a928b4286"
SW_DB_ID = "8c7fcdc6586f8294a86a01e9074e9f1d"

# Long-form blog accounts (see HANDOFF §2): NW blog = 112, SW blog = 113.
BLOG_ACCOUNTS = {"nw": 112, "sw": 113}
BLOG_PROFILES = {"nw": 1, "sw": 3}
BLOG_PLATFORM = "nw_sw_blog"


@dataclass(frozen=True)
class Slot:
    """A scheduled publish decision for one Notion row."""

    page_id: str
    title: str
    brand: str
    account_id: int
    profile_id: int
    scheduled_at: str  # naive UTC ISO, the shape publish_job_targets expects


def _parse_iso(value: str) -> datetime:
    """Parse an ISO datetime, coercing to naive UTC (the DB's storage shape)."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _day_key(dt: datetime) -> str:
    return dt.date().isoformat()


def select_rows_to_schedule(
    rows: list[dict],
    booked_by_account: dict[int, list[str]] | None = None,
    *,
    now: str | datetime | None = None,
    hour_utc: int = DEFAULT_HOUR_UTC,
    stagger_minutes: int = DEFAULT_STAGGER_MINUTES,
    ordering: list[int] | None = None,
) -> list[Slot]:
    """Assign each pending row a publish slot without jamming.

    Rules (first matching wins, walking forward in time):

    * at most one post per account per calendar day;
    * a ``stagger_minutes`` offset per account so two brands never land on the
      same minute;
    * never in the past — a candidate at/before ``now`` rolls to the next day;
    * existing bookings (``booked_by_account``) count exactly like new ones.

    ``rows`` items are ``{page_id, title, brand|profile_id|account_id}``.
    ``ordering`` optionally fixes the account order used for the stagger
    offset (defaults to the order accounts first appear).
    """
    booked: dict[int, set[str]] = {}
    for account_id, times in (booked_by_account or {}).items():
        booked[int(account_id)] = {_day_key(_parse_iso(t)) for t in times}

    now_dt = now if isinstance(now, datetime) else (
        _parse_iso(now) if now else datetime.now(timezone.utc).replace(tzinfo=None))
    if now_dt.tzinfo is not None:
        now_dt = now_dt.astimezone(timezone.utc).replace(tzinfo=None)

    accounts: list[int] = list(ordering or [])
    for row in rows:
        account_id = resolve_account(row)
        if account_id is not None and account_id not in accounts:
            accounts.append(account_id)
    offset_of = {account_id: idx for idx, account_id in enumerate(accounts)}

    slots: list[Slot] = []
    for row in rows:
        brand = resolve_brand(row)
        account_id = resolve_account(row)
        if account_id is None:
            raise ValueError(f"row has no resolvable account: {row!r}")
        profile_id = int(row.get("profile_id") or BLOG_PROFILES[brand])

        offset = offset_of.get(account_id, 0) * int(stagger_minutes)
        taken = booked.setdefault(account_id, set())
        day = now_dt.date()
        while True:
            candidate = datetime(day.year, day.month, day.day, int(hour_utc), 0, 0) + timedelta(minutes=offset)
            if candidate > now_dt and day.isoformat() not in taken:
                break
            day = day + timedelta(days=1)
        taken.add(day.isoformat())

        slots.append(Slot(
            page_id=str(row.get("page_id") or ""),
            title=str(row.get("title") or ""),
            brand=brand,
            account_id=account_id,
            profile_id=profile_id,
            scheduled_at=candidate.isoformat(timespec="seconds"),
        ))
    return slots


def resolve_brand(row: dict) -> str:
    brand = str(row.get("brand") or "").strip().lower()
    if brand in BLOG_ACCOUNTS:
        return brand
    account_id = row.get("account_id")
    if account_id is not None:
        for candidate, acct in BLOG_ACCOUNTS.items():
            if int(account_id) == acct:
                return candidate
    raise ValueError(f"row has no resolvable brand (expected 'nw' or 'sw'): {row!r}")


def resolve_account(row: dict) -> int | None:
    if row.get("account_id") is not None:
        return int(row["account_id"])
    try:
        return BLOG_ACCOUNTS[resolve_brand(row)]
    except ValueError:
        return None


def build_job_spec(slot: Slot, body: str, *, idempotency_key: str | None = None) -> dict:
    """The JobSpec kwargs for one scheduled blog publish.

    ``message`` is the MDX body the nw_sw_blog publisher commits; the content
    row's own text is what the operator wrote in Notion.
    """
    return {
        "platform": BLOG_PLATFORM,
        "profile_id": slot.profile_id,
        "payload": {
            "title": slot.title,
            "message": body,
            "artifacts": [],
            "source": "notion",
            "notionPageId": slot.page_id,
        },
        "targets": [(
            f"account:{slot.account_id}",
            f"notion:{slot.page_id}",
            slot.scheduled_at,
        )],
        "idempotency_key": idempotency_key or f"notion-{slot.page_id}",
    }


def next_slots_preview(rows: list[dict], booked_by_account=None, **kwargs) -> list[dict]:
    """Human-readable preview of the next available slots (for --density-check)."""
    return [
        {
            "pageId": slot.page_id,
            "title": slot.title,
            "brand": slot.brand,
            "accountId": slot.account_id,
            "scheduledAt": slot.scheduled_at,
        }
        for slot in select_rows_to_schedule(rows, booked_by_account, **kwargs)
    ]
