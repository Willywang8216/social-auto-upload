#!/usr/bin/env python3
"""reddit_schedule.py — 產生 NW/SW 的 Reddit 排程 payload。

⚠️ 前提:目標 sub 已逐版確認過規則。Reddit 的成人版**幾乎全部禁止付費推廣**,
   而且有幾個**明文封殺「看我的 profile / link in bio」這種繞道**。打到禁推廣的版
   = 帳號被 ban,而 105(ryanbossom)是 6.6 年 / 372 karma 的資產,不能拿來賭。

   規則於 2026-09-20 用 Reddit API 逐版實測,結果見下面的 SUBS 註解。

SAU 的 Reddit 路徑(publish_reddit_sync,走 OAuth API):
  * submit_publish **要求至少一個媒體檔** -> 媒體有 public_url 時送出的是 **link post**
  * subreddits 由 draft["subreddits"] 決定(純名稱,不帶 r/;API 要裸名)
  * 標題 = message 第一行,>300 字元截斷

在 EmailVPS 容器內執行(需讀得到 videoFile/_library):
  python3 /app/tools_reddit_schedule.py --start 2026-10-10 --days 12 --out /tmp/reddit_payloads
"""
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import date, datetime, timedelta

LIB = pathlib.Path("/app/videoFile/_library")

# 每個品牌有自己的 Reddit 帳號。原本兩軌都用 106(SW),那是錯的。
TRACKS = {
    "NW": {
        "profile_id": 1,
        "account_id": 105,          # u/ryanbossom
        "dir": LIB / "NW",
        # 2026-09-20 實測。以下三個是查得到的規則裡「沒有禁止營利帳號」的:
        #   GayBody           54K  <- 允許 onlyfans,只是不能放標題
        #   NudistMen         65K  <- 只禁 SC/kik 廣告
        #   BareMenPositivity  3K  <- 只禁 spam
        # 被移除的(禁用原因):
        #   nudists       291K  "No Monetized Accounts... you cannot post here at all"
        #   NakedHiking    39K  同樣禁營利帳號
        #   nudism        168K  "No Sellers"
        #   GayPlusNudists 48K  "do not support paid-for nudity"
        #   naturism       39K  "No image-only posts"(只能發文字)
        "subs": ["GayBody", "NudistMen", "BareMenPositivity"],
        "slot": "21:00",
        # Reddit-safe watermark. The profile default ("Nakedwill.com | @nakedwill")
        # bakes a domain and a social handle into the image, which r/twinks bans
        # outright ("social media profiles ... in embedded watermarks") and
        # r/TwinksAndTwunks calls out as "watermarks for paid sites ... forbidden".
        # r/twinks explicitly permits the Reddit username and nothing else.
        "watermark": "u/ryanbossom",
    },
    "SW": {
        "profile_id": 3,
        "account_id": 106,          # u/sexualwill
        "dir": LIB / "SW",
        # 2026-09-20 實測。九個目標版裡**只有兩個**查不到禁止付費推廣的規則:
        #   TwinkCockandFeet  70K  <- 無 promoting/paid/OF 相關規則
        #   TwinkFemboyPorn  310K  <- 同上
        # 被移除的(全部明文禁止付費推廣,打進去 = ban):
        #   twinks          757K  "Don't promote paid content... in your title, comments, watermark"
        #   HungTwinks      344K  "No advertising/paid content"
        #   TwinkLove       249K  "not the place to advertise your OnlyFans"
        #   TwinksAndTwunks 131K  看一眼 profile 就 ban
        #   AsianLadyboners  66K  **明禁「see more in my profile / link in bio」這種繞道**
        #   TwinksWithKinks  41K  無促銷規則,但內容規範嚴
        #   TwunksFUCK       13K  "No Marketing/Self-Promotion"
        "subs": ["TwinkCockandFeet", "TwinkFemboyPorn"],
        "slot": "21:05",
        "watermark": "u/sexualwill",
    },
}


def load_captions() -> dict[str, dict]:
    """filename -> {"title", "body"} from tools/reddit_captions.json.

    Hand-written per image rather than derived from the library descriptions.
    The description text in captions-*.md is vision-model output ("creating a
    serene yet bold artistic mood"), and the same file's OnlyFans paragraphs
    are outright promotion — pasting either into a Reddit post reads as an ad
    and breaks the no-promotion rules most of these subs enforce.

    Keyed by filename, not by title: an image must get its own copy, and the
    library ordering does not match the file ordering.
    """
    p = pathlib.Path(__file__).with_name("reddit_captions.json")
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def pick_files(track: str, n: int) -> list[pathlib.Path]:
    d = TRACKS[track]["dir"]
    if not d.exists():
        return []
    exts = {".jpg", ".jpeg", ".png"}
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in exts)
    return files[:n]


def body_for(cap: dict | None) -> str | None:
    """Compose the Reddit message: title line, blank line, body.

    Deliberately no call-to-action. "More in my profile" / "link in bio" is
    explicitly banned by at least r/AsianLadyboners, and most other adult subs
    ban promotion in any form, so the copy describes the photo and stops.

    Returns None when there is no hand-written copy for this image — the
    caller skips it rather than falling back to a template, because eight
    identical templated posts is itself a spam signal.
    """
    if not cap:
        return None
    title = (cap.get("title") or "").strip()
    body = (cap.get("body") or "").strip()
    if not title or not body:
        return None
    return f"{title}\n\n{body}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10-10")
    ap.add_argument("--days", type=int, default=12)
    ap.add_argument("--out", default="/tmp/reddit_payloads")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    out = pathlib.Path(args.out)
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)

    caps = load_captions()
    if not caps:
        print("  ! no captions in reddit_captions.json — refusing to emit template posts")
        return 1
    # All files, not the first N: images without hand-written copy are skipped,
    # so the walk needs the whole list to find the ones that have it.
    files = {t: pick_files(t, 10 ** 6) for t in TRACKS}
    ptr = {"NW": 0, "SW": 0}

    made = []
    for i in range(args.days):
        day = start + timedelta(days=i)
        track = "NW" if i % 2 == 0 else "SW"   # 兩品牌不同天
        cfg = TRACKS[track]
        sub = cfg["subs"][(i // 2) % len(cfg["subs"])]

        # Walk forward to the next image that actually has copy.
        f = msg = cap = None
        while ptr[track] < len(files[track]):
            cand = files[track][ptr[track]]
            ptr[track] += 1
            m = body_for(caps.get(cand.name))
            if m:
                f, cap, msg = cand, caps.get(cand.name), m
                break
        if f is None:
            print(f"  ! {track}: no image left with copy — skipped {day}")
            continue

        hh, mm = (int(x) for x in cfg["slot"].split(":"))
        when = datetime(day.year, day.month, day.day, hh, mm)
        acct = cfg["account_id"]
        payload = {
            "profileIds": [cfg["profile_id"]],
            "mediaFilePaths": [str(f).replace("/app/", "")],  # videoFile/_library/...
            "selectedAccountIds": [acct],
            "options": {"useLlm": False},
            # Override the profile watermark. The profile default carries a
            # domain and a social handle burned into the image, which r/twinks
            # bans in embedded watermarks and r/TwinksAndTwunks calls out as a
            # paid-site watermark. The Reddit username is what those rules allow.
            "watermark": cfg["watermark"],
            "schedule": {"scheduledAt": when.strftime("%Y-%m-%dT%H:%M:%S")},
            "accountDrafts": {
                str(acct): {
                    "message": msg,
                    "hashtags": [],
                    "subreddits": [sub],   # 裸名,不帶 r/
                }
            },
        }
        name = f"{track}-{day.isoformat()}-r_{sub}.json"
        if not args.dry_run:
            (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        made.append((str(day), track, sub, f.name))
        print(f"  {day} {track:<3} acct={cfg['account_id']} r/{sub:<18} img={f.name[:34]:<34} -> {when}")

    print(f"\n{len(made)} payload(s) {'(dry-run)' if args.dry_run else 'written to ' + str(out)}")
    print("[!] 送出前確認:目標版規則沒變、subreddits 不帶 r/、媒體檔在機器上或抓得回來")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
