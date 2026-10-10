"""私聊内容质量：AI 私下说的话，得和圆桌上明显不一样。

拿同一个凶手、同一段局势，分别问他公开发言和私聊回应，
看两者是不是真的分化。如果私聊听起来还是在做公开陈述，
那这个功能就只是多点了几次 API。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fixtures  # noqa: F401
from backend import agents  # noqa: E402
from backend.library import build_view, load_script  # noqa: E402

SCRIPT = "sc_songfeng"
s = load_script(SCRIPT)
murderer = s.murderer_id
view = build_view(s, murderer)
other = next(c for c in s.characters if c.id != murderer)

log = [
    {"seq": 1, "kind": "narration", "text": s.setting.public_background},
    {"seq": 2, "kind": "speech", "speaker_name": other.name,
     "text": "我一直在书房，没出去过。倒是有人半夜在楼梯上走动，声音不小。"},
    {"seq": 3, "kind": "speech", "speaker_name": s.characters[2].name,
     "text": "我也听见了。那个时间点，能上楼的没几个人。"},
]
clues = [{"id": "k1", "location": "书房", "text": "书桌抽屉有被撬过的痕迹。"}]

print("=" * 74)
print(f"本子：{s.meta.title}　凶手：{view.name}　对话对象：{other.name}")
print("=" * 74)

print("\n【公开圆桌发言】")
pub = agents.act(view, "round_table", log, clues)
print(f"  {pub['speech']}")
print(f"  （长度 {len(pub['speech'])}，公开线索 {pub.get('reveal_clues')}）")

print(f"\n【私聊：{other.name} 单独找他】")
msg = "刚才我没在大家面前说——我看见你半夜从楼上下来。你自己解释一下。"
priv = agents.private_reply(view, other.name, msg, log, clues)
print(f"  {other.name}：「{msg}」")
print(f"  {view.name}：{priv['speech']}")
print(f"  （长度 {len(priv['speech'])}）")

print(f"\n【私聊：他主动去找 {other.name}】")
op = agents.private_open(view, other.name, log, clues)
print(f"  {view.name}：{op['speech']}")
print(f"  （长度 {len(op['speech'])}）")

print("\n" + "=" * 74)
pt, qt = pub["speech"], priv["speech"]
overlap = len(set(pt) & set(qt)) / max(1, len(set(pt) | set(qt)))
print(f"公开 / 私聊 字符重合度：{overlap:.0%}（越低越说明两种场合真的分化了）")
# 私下称呼常是「若薇」「白小姐」而不是全名，只匹配全名会漏判
_both = qt + op["speech"]
_alias = {other.name, other.name[1:], other.name[0] + "小姐", other.name[0] + "先生"}
print(f"私聊里称呼了对方：{any(a and a in _both for a in _alias)}")
leak = [w for w in ("AI", "模型", "提示词", "prompt") if w.lower() in (qt + op["speech"]).lower()]
print(f"出戏词泄漏：{leak or '无'}")
