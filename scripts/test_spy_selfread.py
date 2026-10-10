"""验证卧底能不能自己察觉「我可能是那个例外」并改变说法。

关注两件事：
  1. 真卧底是第几轮把 fit 翻成「对不上」（越早越好，但不能是第 1 轮——
     第 1 轮没人说过话，那时候翻说明模型在瞎猜）
  2. 翻了之后描述有没有真的变——是不是从"说自己的词"转向"往大家那边靠"

平民误判成「对不上」也要看：偶尔有是正常的（词相近本来就容易迷惑），
但如果平民大面积翻，说明这个判断没有信号，纯噪声。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.games.spy import SpyGame  # noqa: E402

seed = int(sys.argv[1]) if len(sys.argv) > 1 else 11
n = int(sys.argv[2]) if len(sys.argv) > 2 else 6

g = SpyGame.create("我", seed=seed, player_count=n)
spy_names = "、".join(g.name(i) for i in g.spy_ids)
print("=" * 74)
print(f"平民词【{g.civil_word}】  卧底词【{g.spy_word}】  卧底：{spy_names}")
print(f"我是 {g.human.name}，词【{g.words[g.human_id]}】"
      f"{'  ← 我就是卧底' if g.human_id in g.spy_ids else ''}")
print("=" * 74)

guard = 0
while not g.finished and guard < 120:
    guard += 1
    out = g.step()
    if out.get("waiting") and out.get("action") == "describe":
        g.act({"type": "describe", "text": "这个东西挺常见的，我不太好形容。"})
    elif out.get("waiting") and out.get("action") == "vote":
        tgt = next(p.id for p in g.alive_players if not p.is_human)
        g.act({"type": "vote", "target_id": tgt})

print()
for rnd in sorted(g.descriptions):
    print(f"── 第 {rnd} 轮 " + "─" * 58)
    reads = g.self_reads.get(rnd, {})
    for pid, desc in g.descriptions[rnd].items():
        tag = "卧底" if pid in g.spy_ids else "平民"
        r = reads.get(pid) or {}
        fit = r.get("fit", "—")
        mark = "  ⚠" if fit == "对不上" else ""
        print(f"  [{tag}] {g.name(pid):<6} {fit:<4}{mark}  「{desc}」")
        if r.get("why"):
            print(f"         └ {r['why']}")

print()
print("=" * 74)
# 真卧底什么时候察觉的
for sid in g.spy_ids:
    if g.player(sid).is_human:
        print(f"{g.name(sid)}（卧底）是真人，不参与自动判断")
        continue
    flips = [rnd for rnd in sorted(g.self_reads)
             if g.self_reads[rnd].get(sid, {}).get("fit") == "对不上"]
    print(f"{g.name(sid)}（卧底）翻成「对不上」的轮次：{flips or '从未察觉'}")

# 平民误判率
tot = wrong = 0
for rnd, reads in g.self_reads.items():
    for pid, r in reads.items():
        if pid in g.spy_ids:
            continue
        tot += 1
        if r.get("fit") == "对不上":
            wrong += 1
print(f"平民误判成「对不上」：{wrong}/{tot}")
print(f"结局：{g.result.get('note') if g.result else '未结束'}")
