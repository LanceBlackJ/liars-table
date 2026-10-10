"""用真实模型打一局谁是卧底，把每一轮的描述全部打出来，用于诊断质量问题。

玩家（p0）由脚本代打，给一个刻意模糊的描述。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.games.spy import SpyGame  # noqa: E402

seed = int(sys.argv[1]) if len(sys.argv) > 1 else 7

n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
g = SpyGame.create("我", seed=seed, player_count=n)
print("=" * 70)
print(f"平民词【{g.civil_word}】　卧底词【{g.spy_word}】　卧底是 {g.name(g.spy_id)}")
print(f"我是 {g.human.name}，拿到的词是【{g.words[g.human_id]}】"
      f"{'（我就是卧底）' if g.human_id == g.spy_id else ''}")
print("=" * 70)

guard = 0
while not g.finished and guard < 120:
    guard += 1
    out = g.step()
    if out.get("waiting") and out.get("action") == "describe":
        g.act({"type": "describe", "text": "这个东西……挺常见的，我不太好形容。"})
    elif out.get("waiting") and out.get("action") == "vote":
        others = [p for p in g.alive_players if p.id != g.human_id]
        g.act({"type": "vote", "target_id": others[0].id, "reason": "随便投的"})

    # 打印新事件
    for e in out.get("events", []):
        k = e["kind"]
        if k == "speech":
            spy = "  ★卧底" if e["speaker_id"] == g.spy_id else ""
            print(f"  {e['speaker_name']:<5}｜{e['text']}{spy}")
        elif k == "system":
            print(f"\n【{e['text']}】")
        elif k == "vote":
            print(f"    投票 {e['speaker_name']} → {e['target_name']}　（{e.get('text','')[:26]}）")
        elif k == "elimination":
            print(f"  ✂ {e['text']}")
        elif k == "narration":
            print(f"\n{e['text']}")

print("\n" + "=" * 70)
r = g.result or {}
print(f"结果：{r.get('note')}")
print(f"共 {g.round} 轮结束")

# 重复度自查
print("\n各轮描述重复度：")
for rnd, descs in sorted(g.descriptions.items()):
    texts = list(descs.values())
    uniq = len(set(texts))
    print(f"  第{rnd}轮：{len(texts)} 条，去重后 {uniq} 条"
          f"{'　⚠️ 有完全重复' if uniq < len(texts) else ''}")
    avg = sum(len(t) for t in texts) / max(1, len(texts))
    print(f"          平均长度 {avg:.0f} 字")
