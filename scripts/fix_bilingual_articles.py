#!/usr/bin/env python3
"""Re-publish the 3 remaining short NW blog posts as bilingual (en + zh-Hant),
humanized articles (humanizer rules for EN, humanizer-zh for 繁體中文).

Usage:
  .venv/bin/python scripts/fix_bilingual_articles.py --apply
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myUtils import profiles as profile_registry  # noqa: E402
from myUtils import prepared_publishers  # noqa: E402

DB = REPO / "db" / "database.db"

# (campaign_id, campaign_post_id, en_title, zh_title, en_body, zh_body)
ARTICLES = [
    (
        2428, 4053,
        "Chin-up in the Dirt 26",
        "在泥土裡拉單槓（第二十六張）",
        """# Chin-up in the Dirt 26

There's a tree near where I was staying, a branch low enough to grab. I remember thinking I'd look ridiculous doing a chin-up with no clothes on. Then I did it anyway.

The dirt was warm under my feet. My palms went around the branch and I pulled, and for a second the only thing in the world was the burn in my arms and the sound of my own breathing.

That's the part I keep coming back to. Not how my body looked — how it worked. A body that hauls itself up is a body that's alive, and I forget that too often.

I've been naked online for a while now. People assume it's about showing off. Sometimes it is, I won't lie. But a lot of it is the opposite: I want to remember this body is a body, not a display. It sweats, it strains, it gets dirt on its back, and none of that needs an audience.

The chin-up became a little series. I'm up to twenty-six now, which is where the number comes from. Each one is a small argument against the idea that a naked body has to be polished or posed. Here it's just me, gravity, and a branch.

I don't have a tidy lesson for this one. I just like the picture. I like that it records effort instead of performance.

— Will""",
        """# 在泥土裡拉單槓

住處附近有棵樹，枝幹低低的，剛好夠我伸手抓住。我那時想，光著身子拉單槓一定很蠢，但還是拉了。

腳下的泥土溫溫的。手掌抓住枝幹，往上使勁，那一瞬間整個世界只剩下手臂的酸，跟自己的呼吸聲。

我一直忘不掉的是這個——不是身體好不好看，是它還能使力。一個能把人撐上去的身體，是活著的身體。我常常忘了這件事。

我裸體上網已經一陣子了。大家都以為是為了秀。有些時候是啊，我不騙你。但更多的時候剛好相反：我想記得這副身體就是一副身體，不是展示品。它會流汗、會出力、背上會沾到土，這些都不需要觀眾。

拉單槓這件事慢慢變成一個小系列，現在拍到第二十六張了，編號就是這麼來的。每一張都是在跟「裸體就該擺好姿勢、修得漂亮」這種想法唱反調。這裡只有我、地心引力，跟一根樹枝。

這篇沒什麼大道理。我就是喜歡這張照片，喜歡它紀錄的是「使勁」，不是「表演」。

— Will""",
    ),
    (
        2441, 4145,
        "What Artistic Nudity Means to Me",
        "「藝術裸體」這四個字，對我來說是什麼",
        """# What Artistic Nudity Means to Me

Someone asked me the other day what "artistic nudity" even is. Fair question. The phrase gets used to make nudity sound more respectable than it is, and I don't want to hide behind it.

For me it's simpler. Some days I photograph my body not to show skin, but to record how it feels to stand in a room with nothing between me and the air. The light does most of the work. I just stand there.

I don't think I look perfect, and I've stopped trying to. The interesting part is what shows up when I'm not arranging myself: a slouch, a scar, a hand resting somewhere it wouldn't in a posed shot. Those are the honest parts.

This image is a preview of a longer set I'm slowly putting together. It's about vulnerability, which sounds grand, but really it just means I didn't crop out the parts I used to hide.

If you look at it and feel something, good. If you look and feel nothing, that's fine too. It's just a body, and that's the point.

— Will""",
        """# 「藝術裸體」這四個字，對我來說是什麼

前幾天有人問我，藝術裸體到底是什麼。問得好。這四個字常常被拿來把裸體講得比較體面，但我不想躲在它後面。

對我來說沒那麼複雜。有些日子我拍自己的身體，不是為了露，是想記住自己站在一個房間裡、身上什麼都沒有的感覺。光線做了大部分的事，我只是站在那裡。

我不覺得自己完美，也早就不追求了。有意思的是，當我不去擺姿勢，會自己跑出來的東西：一個駝背、一道疤、一隻手放在平常拍照不會放的地方。那些才是真的。

這張照片是一整組作品的預告，我慢慢在整理。主題是脆弱——講起來很大，但其實只是：我沒有再把以前會裁掉的地方裁掉。

你看了有感觸，很好。看了沒感覺，也沒關係。它就是一副身體，而這正是重點。

— Will""",
    ),
    (
        2450, 4210,
        "What I Noticed the Second Time",
        "第二次，我注意到的事",
        """# What I Noticed the Second Time

The first video was about doing it at all. Getting the camera out, stepping into frame with nothing on, and not running away from the replay. That part took most of the courage.

The second time is different. I knew the drill, so I could pay attention to what was actually happening instead of managing my nerves. And what I noticed surprised me.

I got colder than I expected, first thing. Nerves burn off fast, and then you're just a person in a field, and the wind has an opinion. My shoulders came up, which I only saw later. I looked smaller than I feel most days.

Then I relaxed. There's a moment in the middle where I stop posing and just talk to the camera like it's a friend. That's the part I kept.

I'm not sure these videos are about anything tidy. If the first one was about fear, this one is about the five minutes after fear — what's left when you stop performing and just stay.

— Will""",
        """# 第二次，我注意到的事

第一支影片是關於「做下去」。把相機拿出來、光著身子走進畫面、回放的時候沒有逃掉。那一步用掉了大部分的勇氣。

第二次不一樣。流程我熟了，所以能把注意力放在真正發生的事上，而不是忙著安撫自己。結果我注意到的東西，讓我有點意外。

一開始比想像中冷。緊張燒掉得很快，然後你就只是個站在野地裡的人，風很有意見。我的肩膀縮起來了——這是後來看回放才發現的。我看起來比平常小。

後來就鬆了。中間有一段，我不擺姿勢了，就像跟朋友聊天一樣對著鏡頭講話。那段我留下來了。

我不確定這些影片「關於」什麼。如果第一支是關於害怕，那這支是關於害怕之後的那五分鐘——當你不表演了，只是留下來，會剩下什麼。

— Will""",
    ),
]


def main() -> int:
    apply = "--apply" in sys.argv
    print(f"apply={apply}")
    for campaign_id, post_id, en_title, zh_title, en_body, zh_body in ARTICLES:
        message = f"{en_body}\n\n---\n\n{zh_body}"
        title = f"{en_title}｜{zh_title}"
        draft = {
            "title": title,
            "message": message,
            "summary": en_title,
            "description": en_title,
            "hashtags": ["#naturism", "#bodyfreedom", "#nakedwill"],
            "firstComment": "",
            "locales": ["en", "zh"],
        }
        print(f"campaign {campaign_id} post {post_id}: {en_title} ({len(message)} chars)")
        if not apply:
            continue
        # update campaign_posts
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT campaign_id, draft_json FROM campaign_posts WHERE id=?", (post_id,)).fetchone()
        old = json.loads(row["draft_json"] or "{}") if row else {}
        merged = dict(old)
        merged.update({k: v for k, v in draft.items() if v})
        con.execute("UPDATE campaign_posts SET draft_json=? WHERE id=?",
                    (json.dumps(merged, ensure_ascii=False), post_id))
        con.commit()
        con.close()
        # get the blog account for this campaign's profile
        con = sqlite3.connect(DB)
        cid_row = con.execute("SELECT profile_id FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        con.close()
        account = None
        for acc in profile_registry.list_accounts(profile_id=cid_row[0], enabled=True):
            if acc.platform in ("nw_sw_blog", "teaching_blog"):
                account = acc
                break
        if account is None:
            print(f"  no blog account for profile {cid_row[0]} — skip publish")
            continue
        payload = {
            "platform": account.platform,
            "campaignId": campaign_id,
            "message": message,
            "draft": draft,
            "artifacts": [],
            "sheetRow": {},
        }
        try:
            prepared_publishers.publish_nw_sw_blog_sync(account, payload)
            print(f"  republished en+zh")
        except Exception as exc:
            print(f"  republish FAILED: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
