"""用真实模型打一段剧本杀圆桌，检查 AI 是否真的在互相回应（而不是各说各的）。

只跑到第一轮圆桌结束，够看出交锋质量了——打完整局要几十次调用，太慢。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import engine  # noqa: E402
from backend.engine import Phase  # noqa: E402

def _refers(text: str, name: str) -> bool:
    """AI 更爱用「陈管家」「林大夫」这类称呼而不是全名，只匹配全名会严重低估。"""
    surname = name[0]
    forms = {name, name[1:]} | {
        surname + t for t in
        ("小姐", "先生", "老板", "大夫", "管家", "医生", "夫人", "太太", "老", "总")
    }
    return any(f and f in text for f in forms)


sid = sys.argv[1] if len(sys.argv) > 1 else "sc_songfeng"
cid = sys.argv[2] if len(sys.argv) > 2 else "c1"

g = engine.new_game(sid, cid)
g.step()
names = [c.name for c in g.script.characters]
me = g.script.char(cid).name
print("=" * 72)
print(f"《{g.script.meta.title}》　我演 {me}　凶手是 {g.script.char(g.script.murderer_id).name}")
print("=" * 72)

seen = 0
guard = 0
while g.phase != Phase.DONE and guard < 60:
    guard += 1
    out = g.step()
    for e in g.public_log[seen:]:
        if e["kind"] == "speech":
            tag = "  ←凶手" if e["speaker_id"] == g.script.murderer_id else ""
            # 有没有点到别人的名字？
            hits = [n for n in names if n != e["speaker_name"] and _refers(e["text"], n)]
            mark = f"　【点名: {'、'.join(hits)}】" if hits else "　【没点任何人】"
            print(f"\n{e['speaker_name']}{tag}{mark}\n  {e['text']}")
        elif e["kind"] == "system":
            print(f"\n===== {e['text']} =====")
        elif e["kind"] == "narration":
            print(f"\n【旁白】{e['text'][:90]}")
    seen = len(g.public_log)

    if out.get("waiting"):
        a = out.get("action")
        if a == "continue":
            g.act({"type": "continue"})
        elif a == "speak":
            g.act({"type": "speak", "text":
                   "我是白若薇。晚宴后我在花园待着，回来就听说叔父出事了。"
                   "我倒是想问陈管家一句——你说你一直在厨房，可厨房离书房才几步路。"})
        elif a == "search":
            g.act({"type": "search", "location": g.script.setting.map[0]})
        elif a == "ask":
            g.act({"type": "ask", "skip": True})
        elif a == "answer":
            g.act({"type": "answer", "text": "我没什么好说的，我确实在花园。"})
        elif a == "vote":
            other = next(c.id for c in g.script.characters if c.id != cid)
            g.act({"type": "vote", "target_id": other, "reason": "直觉"})
        else:
            print(f"  ⚠️ 未处理的动作: {a!r}")
            break

    # 第一轮圆桌结束就停
    if g.phase.value.startswith("interrogate"):
        break

speeches = [e for e in g.public_log if e["kind"] == "speech" and e["speaker_id"] != cid]
named = sum(
    1 for e in speeches
    if any(_refers(e["text"], n) for n in names if n != e["speaker_name"])
)
quoting = sum(1 for e in speeches if re.search(r"[「‘“].{4,}?[」’”]", e["text"]))
avg = sum(len(e["text"]) for e in speeches) / max(1, len(speeches))
print("\n" + "=" * 72)
print(f"AI 发言 {len(speeches)} 条　平均 {avg:.0f} 字")
print(f"点名回应了别人的：{named}/{len(speeches)}　（{named/max(1,len(speeches)):.0%}）")
print(f"直接引用了别人原话：{quoting}/{len(speeches)}　（{quoting/max(1,len(speeches)):.0%}）")
