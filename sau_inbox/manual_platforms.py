"""Manual-platform helpers for the SAU-Inbox watcher.

Fansly / OnlyFans / JustForFans / Patreon have no automatable posting API in
SAU, so every ready item also ships a Telegram checklist telling the operator
which platforms still need a manual upload, plus the metadata they need
(title / caption / filename / thumbnail).

Also holds the "both" brand note: a `both` persona item is cross-posted to the
NW and SW brands (promoting naturism inside the sexual channel is allowed; the
reverse is not).
"""

from __future__ import annotations

from pathlib import Path

MANUAL_PLATFORMS = ("Fansly", "OnlyFans", "JustForFans", "Patreon")

_BRIEF_LIMIT = 600


def manual_block(entry: dict) -> str:
    """Telegram block listing the manual platforms + the metadata required.

    Returns a string starting with a newline so it can be appended to an
    existing message body.
    """
    title = (entry.get("topic") or entry.get("titleSuggestion")
             or "(title TBD)")
    brief = (entry.get("brief") or "").strip()
    if len(brief) > _BRIEF_LIMIT:
        brief = brief[:_BRIEF_LIMIT] + "..."

    lines = [
        "",
        "== 手動平台（需自行上傳）==",
        "、".join(MANUAL_PLATFORMS),
        f"標題：{title}",
    ]
    source = entry.get("sourcePath")
    if source:
        lines.append(f"檔案：{Path(source).name}")
    if brief:
        lines.append(f"文案：{brief}")
    thumb = entry.get("thumbPath")
    if thumb:
        lines.append(f"縮圖：{Path(thumb).name}")
    return "\n".join(lines)


def brand_note(entry: dict) -> str:
    """Warn when a `both` item will be published to two brands at once."""
    if entry.get("persona") == "both":
        return ("\n(both 素材：將同時發佈到 NW + SW 兩個品牌；"
                "單向 nudism -> sexual 可以，反向不行)")
    return ""
