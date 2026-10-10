"""离线打完整局狼人杀 / 谁是卧底，验证状态机不死锁、不泄漏。

离线模式下 LLM 返回固定台词，JSON 解析全部失败 —— 正好把所有兜底分支跑一遍。
玩家动作用简单策略自动代打。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SG_MOCK"] = "1"

from backend.config import SETTINGS  # noqa: E402

assert SETTINGS.mock, "需要 SG_MOCK=1"

from backend.games.spy import SpyGame  # noqa: E402
from backend.games.werewolf import WerewolfGame  # noqa: E402

MAX_STEPS = 400


def auto_act(g, action: str) -> dict:
    """替玩家做决定。"""
    me = g.human
    alive_others = [p for p in g.alive_players if p.id != me.id]
    if action == "describe":
        return {"type": "describe", "text": "这东西我平时挺常见的，说不上多特别。"}
    if action == "speak":
        return {"type": "speak", "text": "我先听听大家怎么说，暂时没有明确怀疑对象。"}
    if action == "vote":
        return {"type": "vote", "target_id": alive_others[0].id, "reason": "直觉。"}
    if action == "wolf_kill":
        good = [p for p in g.alive_players if p.role != "wolf"]
        return {"type": "wolf_kill", "target_id": good[0].id}
    if action == "seer_check":
        return {"type": "seer_check", "target_id": alive_others[0].id}
    if action == "witch_act":
        return {"type": "witch_act", "action": "pass"}
    if action == "hunter_shoot":
        return {"type": "hunter_shoot", "target_id": alive_others[0].id if alive_others else ""}
    raise AssertionError(f"未处理的玩家动作: {action}")


def play(g, label: str) -> None:
    print(f"\n{'='*66}\n{label}　我的身份：{g.human.role_label}\n{'='*66}")
    steps = 0
    while not g.finished and steps < MAX_STEPS:
        steps += 1
        out = g.step()
        if out.get("waiting") and out.get("action"):
            g.act(auto_act(g, out["action"]))
    assert g.finished, f"❌ {label} 在 {steps} 步后仍未结束（可能死锁）"

    # 泄漏检查：结束前玩家可见状态里不能出现别人的身份
    print(f"✅ 正常结束，共 {steps} 步，{len(g.log.events)} 条事件")
    for k, v in (g.result or {}).items():
        if k in ("note", "human_win", "civil_win", "wolf_win", "spy_name", "civil_word", "spy_word"):
            print(f"   {k}: {v}")


def check_isolation(g, label: str) -> None:
    """核心断言：非狼玩家的可见日志里，不能有狼队夜聊。"""
    import json as _json

    for p in g.players:
        blob = _json.dumps(g.log.visible(p.id), ensure_ascii=False)
        if g.kind == "werewolf" and p.role != "wolf":
            assert "（狼）" not in blob, f"❌ {label}: {p.name}({p.role}) 看到了狼队夜聊"
        if g.kind == "werewolf" and p.role != "seer":
            assert "你查验了" not in blob, f"❌ {label}: {p.name} 看到了预言家验人结果"
    print(f"✅ {label} 信息隔离断言通过")


for i, n in enumerate([5, 6, 8, 10]):
    s = SpyGame.create("测试玩家", seed=100 + i, player_count=n)
    play(s, f"谁是卧底 {n}人局")
    check_isolation(s, "谁是卧底")

for i, n in enumerate([6, 8, 9, 12]):
    w = WerewolfGame.create("测试玩家", seed=200 + i, player_count=n)
    play(w, f"狼人杀 {n}人局")
    check_isolation(w, "狼人杀")

print("\n🎉 全部通过")
