"""狼人杀投票理由的重复检测。

起因：实测出现 4 个人的投票理由**一字不差**，连「完全无视了8号指出的…」
这种自己说自己的第三人称都照抄。根因是狼人杀一直没用 p.style，
几个平民的上下文除编号外完全一致。

这里把局面直接摆到白天投票，只跑投票那几次调用，检查：
  · 有没有完全相同的理由
  · 两两之间的字符重合度（>0.75 基本就是换个说法讲同一件事）
  · 长度是否有分化（原来所有人都写小作文）
"""
from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.games.werewolf import WerewolfGame  # noqa: E402

seed = int(sys.argv[1]) if len(sys.argv) > 1 else 17
n = int(sys.argv[2]) if len(sys.argv) > 2 else 9

g = WerewolfGame.create("我", seed=seed, player_count=n)
g.round = 1
g.phase = "day_discuss"

# 造一段有内容的白天讨论，让投票有东西可引用
lines = [
    "昨晚被刀的是5号，狼队第一刀砍神，说明他们摸到点信息了。",
    "我跳预言家，昨晚验的1号，是好人。3号昨天带节奏太急，我怀疑他。",
    "7号刚跳预言家，可我也验了人——这就有两个预言家了，总有一个是假的。",
    "两个预言家我更信7号，他把心路讲清楚了，3号只说了个结论。",
    "别急着站边。先看昨天谁替3号说过话，那批人现在都不吭声了。",
    "我话不多：投3号。他从头到尾没给过一条具体依据。",
]
speakers = [p for p in g.alive_players if not p.is_human][: len(lines)]
for p, t in zip(speakers, lines):
    g.say(p.id, t)

g.phase = "day_vote"
g.votes = {}
g.divider("投票放逐")

print("=" * 74)
print(f"{n} 人局 seed={seed}")
print("=" * 74)

rows = []
for _ in range(n * 2):
    pend = [p for p in g.alive_players if p.id not in g.votes and not p.is_human]
    if not pend:
        break
    g._step_vote()

for e in g.log.events:
    if e.get("kind") == "vote" and e.get("text"):
        rows.append((e["speaker_name"], e["target_name"], e["text"]))

for who, tgt, txt in rows:
    print(f"\n{who} → {tgt}　（{len(txt)} 字）")
    print(f"  {txt}")

print("\n" + "=" * 74)
exact = 0
for (a, _, ta), (b, _, tb) in combinations(rows, 2):
    if ta.strip() == tb.strip():
        exact += 1
        print(f"❌ 完全相同：{a} 和 {b}")

pairs = []
for (a, _, ta), (b, _, tb) in combinations(rows, 2):
    sa, sb = set(ta), set(tb)
    ov = len(sa & sb) / max(1, len(sa | sb))
    pairs.append((ov, a, b))
pairs.sort(reverse=True)

print(f"完全相同的对数：{exact}")
print("字符重合度最高的三对：")
for ov, a, b in pairs[:3]:
    flag = "  ← 太像了" if ov > 0.75 else ""
    print(f"  {a} vs {b}：{ov:.0%}{flag}")

lens = [len(t) for _, _, t in rows]
print(f"长度：最短 {min(lens)}　最长 {max(lens)}　跨度 {max(lens) - min(lens)}")
print(f"投票分布：{ {t: [r[1] for r in rows].count(t) for t in {r[1] for r in rows}} }")
