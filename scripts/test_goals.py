"""验证个人目标判定：造两份公开记录，一份秘密被说破、一份没有，看裁判判得对不对。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fixtures  # noqa: F401
from backend import goals  # noqa: E402
from backend.library import build_view, load_script  # noqa: E402

s = load_script("sc_songfeng")
view = build_view(s, "c1")   # 白若薇，秘密是盗用画稿署名参展

print(f"角色：{view.name}")
print(f"秘密：{view.private_secret[:40]}…")
for g in view.personal_goals:
    print(f"  目标[{g.type}] {g.text}")

CASES = {
    "秘密被当众说破": [
        {"kind": "speech", "speaker_name": "郑鹤年",
         "text": "我早查过了！白若薇拿她叔父没发表的画稿去参展，署的是她自己的名字！"},
        {"kind": "speech", "speaker_name": "白若薇",
         "text": "……是。那幅画是叔父的。我认。"},
        {"kind": "speech", "speaker_name": "陈叔平", "text": "原来如此，难怪老爷发火。"},
    ],
    "秘密完全没提": [
        {"kind": "speech", "speaker_name": "郑鹤年", "text": "我看是林砚秋，他一直在书房附近。"},
        {"kind": "speech", "speaker_name": "白若薇", "text": "我晚宴后一直在花园，没进过书房。"},
        {"kind": "speech", "speaker_name": "陈叔平", "text": "老奴可以为白小姐作证。"},
    ],
    "被怀疑但没承认": [
        {"kind": "speech", "speaker_name": "顾云章", "text": "白小姐最近在筹备画展？画是哪来的？"},
        {"kind": "speech", "speaker_name": "白若薇", "text": "我自己画的。顾老板这话什么意思？"},
        {"kind": "speech", "speaker_name": "顾云章", "text": "随口一问，别多心。"},
    ],
}

for label, log in CASES.items():
    res = goals.judge(view, log)
    print(f"\n【{label}】")
    for r in res:
        mark = "✅达成" if r["achieved"] else "❌失败"
        print(f"  {mark}  {r['text'][:26]}…")
        print(f"          理由：{r['reason'][:60]}")
