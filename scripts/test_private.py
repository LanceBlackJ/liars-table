"""私聊的信息隔离断言。

这是整个功能里唯一不能出错的地方：私下说的话只有当事两人能看见。
漏一条，凶手私下拉拢的内容就会当着全场亮出来，本局直接废掉。

用 mock 模式跑，不烧 token——检查的是可见性，与模型输出无关。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SG_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fixtures  # noqa: F401
from backend import engine  # noqa: E402
from backend.engine import Phase  # noqa: E402
from backend.library import load_script  # noqa: E402

SECRET = "这句话是私聊内容不该被第三方看到"

g = engine.new_game("sc_songfeng", "c1")
me = g.player_char_id

# 推进到第一个私聊阶段
for _ in range(400):
    out = g.step()
    if g.phase in (Phase.PRIVATE_1, Phase.DONE):
        break
    if out["waiting"]:
        a = out["action"]
        if a == "continue":
            g.act({"type": "continue"})
        elif a == "speak":
            g.act({"type": "speak", "text": "我没什么好说的。"})
        elif a == "search":
            g.act({"type": "search", "location": g.script.setting.map[0]})
        elif a == "ask":
            g.act({"type": "ask", "skip": True})
        elif a == "answer":
            g.act({"type": "answer", "text": "不知道。"})

assert g.phase == Phase.PRIVATE_1, f"没走到私聊阶段，停在 {g.phase}"
print(f"阶段：{g.phase.value}，等待动作：{g._expected()}")

# 玩家找 c2 私聊，并出示手里的第一条线索
held = g.private_holdings.get(me, [])
show = held[:1]
g.act({"type": "private", "target_id": "c2", "text": SECRET, "show_clues": show})

# 让 AI 之间也私聊一轮
for _ in range(12):
    if g.phase != Phase.PRIVATE_1:
        break
    out = g.step()
    if out["waiting"] and out["action"] == "private":
        g.act({"type": "private", "skip": True})

print()
fails = []

# ① 玩家说的悄悄话
priv = [e for e in g.public_log if e["kind"] == "private"]
print(f"私聊事件共 {len(priv)} 条")
for e in priv:
    vt = e.get("visible_to")
    if vt is None:
        fails.append(f"seq={e['seq']} 私聊事件没有 visible_to，等于全场公开")
    elif sorted(vt) != sorted({e["speaker_id"], e["target_id"]}):
        fails.append(f"seq={e['seq']} visible_to={vt} 与当事人不符")

# ② 逐角色核对：非当事人绝不能看到 SECRET
for c in g.script.characters:
    log = g._log_for(c.id)
    texts = " ".join(str(e.get("text", "")) for e in log)
    can_see = SECRET in texts
    should = c.id in (me, "c2")
    mark = "可见" if can_see else "不可见"
    ok = can_see == should
    print(f"  {c.id} {c.name:<6} {mark:<4} {'OK' if ok else '←← 泄漏!'}")
    if not ok:
        fails.append(f"{c.id} 的可见性错误：应{'可见' if should else '不可见'}，实为{mark}")

# ③ private_meta 必须人人可见（看得见有人凑一块儿，听不见内容）
metas = [e for e in g.public_log if e["kind"] == "private_meta"]
print(f"\n私聊公告 {len(metas)} 条，内容示例：{metas[0]['text'] if metas else '无'}")
for e in metas:
    if e.get("visible_to") is not None:
        fails.append(f"seq={e['seq']} private_meta 不该限制可见范围")
    if SECRET in str(e.get("text", "")):
        fails.append(f"seq={e['seq']} private_meta 里混进了私聊正文")

# ④ 出示的线索要真的转移给对方
if show:
    got = show[0] in g.private_holdings.get("c2", [])
    print(f"出示线索 {show[0]} → c2 已持有：{got}")
    if not got:
        fails.append("私下出示的线索没有转移给对方")

# ⑤ 发给前端的载荷同样不能漏
front = " ".join(str(e.get("text", "")) for e in g.log_for_player())
if SECRET not in front:
    fails.append("玩家自己的私聊内容反而看不到了")

print()
if fails:
    print("失败：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("全部通过：私聊内容仅当事人可见，公告全场可见，线索已转移。")
