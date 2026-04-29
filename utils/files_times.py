from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from conf import BASE_DIR


def get_absolute_path(relative_path: str, base_dir: str = None) -> str:
    """Convert a relative path to an absolute path under ``BASE_DIR``."""

    absolute_path = Path(BASE_DIR) / base_dir / relative_path
    return str(absolute_path)


def get_title_and_hashtags(filename):
    """Extract title and hashtags from a sidecar `.txt` file next to a video.

    The sidecar's first line is the title, the second line is space-separated
    hashtags (with or without the leading ``#``).
    """

    txt_filename = filename.replace(".mp4", ".txt")
    with open(txt_filename, "r", encoding="utf-8") as f:
        content = f.read()

    parts = content.strip().split("\n")
    title = parts[0]
    hashtags = parts[1].replace("#", "").split(" ")

    return title, hashtags


def _normalise_daily_time(value) -> tuple[int, int]:
    """Accept either ``int`` (legacy hour-only) or ``"HH:MM"`` strings.

    The Vue frontend sends ``"10:00"``-style strings, while the legacy default
    in this module was a list of integer hours. Both forms are supported.
    """

    if isinstance(value, int):
        if not 0 <= value <= 23:
            raise ValueError(f"Hour out of range: {value}")
        return value, 0

    if isinstance(value, str) and ":" in value:
        hour_str, minute_str = value.split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"Time out of range: {value}")
        return hour, minute

    raise TypeError(
        f"daily_times entries must be int hours or 'HH:MM' strings, got {value!r}"
    )


def generate_schedule_time_next_day(
    total_videos: int,
    videos_per_day: int = 1,
    daily_times: Iterable | None = None,
    timestamps: bool = False,
    start_days: int = 0,
):
    """Generate a publish schedule starting from a future day.

    Args:
        total_videos: total number of videos to schedule.
        videos_per_day: how many videos per day.
        daily_times: list of times of day. Each entry is either an integer
            hour (0–23) or an ``"HH:MM"`` string.
        timestamps: when True, return integer Unix timestamps; otherwise
            return ``datetime`` objects.
        start_days: number of days to skip; ``0`` means start tomorrow.

    Returns:
        list of ``datetime`` objects or integer timestamps.
    """

    if videos_per_day <= 0:
        raise ValueError("videos_per_day must be a positive integer")

    if daily_times is None:
        daily_times = [6, 11, 14, 16, 22]
    daily_times_list = list(daily_times)

    if videos_per_day > len(daily_times_list):
        raise ValueError("videos_per_day must not exceed len(daily_times)")

    # Normalise once up-front so both legacy ints and frontend "HH:MM" strings work.
    normalised = [_normalise_daily_time(item) for item in daily_times_list]

    schedule: list[datetime] = []
    now = datetime.now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for video in range(total_videos):
        day_offset = video // videos_per_day + start_days + 1  # +1 → start tomorrow
        daily_index = video % videos_per_day
        hour, minute = normalised[daily_index]
        target = today_midnight + timedelta(days=day_offset, hours=hour, minutes=minute)
        schedule.append(target)

    if timestamps:
        return [int(item.timestamp()) for item in schedule]
    return schedule
