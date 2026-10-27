#!/usr/bin/env python3
"""gen_title.py — 用內容上下文自動生吸睛標題（Task #7 自動命名核心）。

Host 側小工具：給「內容摘要（transcript/畫面描述）」→ LLM 回一個
SEO/geo/attention-grabbing 的標題 + 一句說明。之後接進 watch.py 的
needsTitleGeneration 流程，或由 Claude 發布時呼叫。

LLM 設定讀環境變數（watch.py 的 .env 已存）：
  SAU_WATCH_LLM_BASE  預設 https://2c2ch1u11-share-api-0.hf.space/v1
  SAU_WATCH_LLM_KEY   必要
  SAU_WATCH_LLM_MODEL 預設 deepseek-v4-pro

用法：
  echo "內容 blurb" | python3 gen_title.py --persona nakedwill
  python3 gen_title.py --file transcript.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(_HERE, ".env")):
    for _raw in open(os.path.join(_HERE, ".env"), encoding="utf-8"):
        _line = _raw.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

LLM_BASE = os.environ.get("SAU_WATCH_LLM_BASE", "https://2c2ch1u11-share-api-0.hf.space/v1")
LLM_KEY = os.environ.get("SAU_WATCH_LLM_KEY", "")
LLM_MODEL = os.environ.get("SAU_WATCH_LLM_MODEL", "deepseek-v4-pro")


def chat(prompt: str) -> str:
    if not LLM_KEY:
        raise RuntimeError("SAU_WATCH_LLM_KEY 未設定")
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


def generate_title(context_text: str, *, persona: str = "", sfw: bool = True) -> str:
    """內容摘要 → 標題。回純標題（去掉前後引號/列點）。"""
    flag = "SFW / 可公開（IG/FB/Threads/YT 可露有限）" if sfw else "NSFW / 成人平台（X 等）"
    tone = ""
    if persona == "nakedwill":
        tone = "品牌調性：Nakedwill = nudity as authenticity（裸體作為真實、自然主義、身體自由）。"
    elif persona == "sexualwill":
        tone = "品牌調性：性正向、身體自信、性感但真誠。"
    prompt = (
        "你是社群內容總監。根據以下「影片內容摘要」，幫我想一個 SEO＋地理吸睛＋會讓人有慾望點的標題。\n"
        f"發布性質：{flag}\n{tone}\n"
        "要求：\n"
        "- 只要 1 個主標題（≤60 字，可中英混）＋最多 2 個備選（用｜分隔）\n"
        "- 具體、有鉤子、不誇大內容、不寫違反平台規範的字眼\n"
        "只輸出格式：\n主標題：...\n備選：A｜B\n\n"
        f"內容摘要：\n{context_text[:3000]}"
    )
    out = chat(prompt)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None, help="讀內容摘要的文字檔")
    ap.add_argument("--persona", default="")
    ap.add_argument("--nsfw", action="store_true", help="NSFW 版")
    args = ap.parse_args()
    if args.file:
        text = open(args.file, encoding="utf-8").read().strip()
    else:
        text = sys.stdin.read().strip()
    if not text:
        print("請提供內容摘要（stdin 或 --file）", file=sys.stderr)
        return 2
    print(generate_title(text, persona=args.persona, sfw=not args.nsfw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
