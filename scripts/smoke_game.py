"""离线冒烟测试：不调用任何 LLM，把一整局从开场打到复盘。

用来验证状态机、队列调度、线索归属、投票结算是否正确。

    conda run -n scriptgame python scripts/smoke_game.py [script_id]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SG_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fixtures  # noqa: F401
from backend import engine  # noqa: E402
from backend.library import list_scripts, load_script  # noqa: E402
from backend.engine import Phase  # noqa: E402
from backend.main import game_state  # noqa: E402

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else "sc_songfeng"


def auto_player(g: engine.Game, action: str) -> dict:
    """替玩家做一个合理的自动动作。"""
    st = game_state(g)
    others = [r for r in st["roster"] if r["id"] != g.player_char_id]
    if action == "continue":
        return {"type": "continue"}
    if action == "speak":
        return {"type": "speak", "text": "（自动玩家）我把我知道的说一下。",
                "reveal_clues": [c["id"] for c in st["my_clues"][:1]]}
    if action == "answer":
        return {"type": "answer", "text": "（自动玩家）这个问题我回答不了。"}
    if action == "search":
        return {"type": "search", "location": st["map"][g.seq % len(st["map"])]}
    if action == "ask":
        return {"type": "ask", "target_id": others[0]["id"], "question": "你当晚到底在哪？"}
    if action == "vote":
        return {"type": "vote", "target_id": others[0]["id"], "reason": "直觉。"}
    if action == "deduce":
        # 答卷：每题随便挑第一个选项。只为走通流程，不求答对。
        qs = st.get("questions") or []
        return {"type": "deduce",
                "answers": {q["id"]: q["options"][0]["id"] for q in qs if q["options"]}}
    if action == "private":
        # 这一支原来是漏的。_expected() 在 PRIVATE_1/2 会返回 "private"，
        # 于是脚本直接 AssertionError —— 也就是说**这个冒烟测试从来没跑完过一局**。
        # 它是唯一能证明「改完之后老本子还照常玩」的东西，先修它。
        return {"type": "private", "target_id": others[0]["id"],
                "text": "（自动玩家）单独跟你说两句。",
                "show_clues": []}
    raise AssertionError(f"未知动作 {action}")


def run_one(script_id: str, quiet: bool = False) -> int:
    # 玩家角色不能写死 "c1" —— 各本子的 id 前缀不一样（sc_dog 是 g1、sc_rain 是 r1），
    # 写死就只有一部分本子能跑。取第一个角色即可。
    first = load_script(script_id).characters[0].id
    g = engine.new_game(script_id, first)
    print(f"开局 {g.game_id}　剧本={script_id}　玩家扮演={g.script.char(first).name}")
    print(f"（本局凶手是 {g.script.char(g.script.murderer_id).name}，仅测试脚本可见）\n")

    seen_phases: list[str] = []
    guard = 0
    while g.phase != Phase.DONE and guard < 900:
        guard += 1
        out = g.step()
        if g.phase.value not in seen_phases:
            seen_phases.append(g.phase.value)
            print(f"  ▶ 进入阶段: {g.phase.value}")
        if out["waiting"] and out["action"]:
            g.act(auto_player(g, out["action"]))

    if guard >= 900:
        print("\n❌ 超过步数上限，状态机可能死循环")
        return 1

    print(f"\n经历阶段：{' → '.join(seen_phases)}")
    print(f"公开事件总数：{len(g.public_log)}")

    counts: dict[str, int] = {}
    for e in g.public_log:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    print(f"事件类型分布：{counts}")

    held = {cid: len(v) for cid, v in g.private_holdings.items()}
    print(f"线索持有：{held}　合计 {sum(held.values())}/{len(g.script.clues)}")
    n_voters = len([c for c in g.script.characters if not c.is_npc])   # 证人不投票
    print(f"投票数：{len(g.votes)}/{n_voters}")

    r = g.result
    assert r is not None, "结算未产生 result"
    print(f"\n凶手：{r['murderer_name']}　票型：{r['tally']}")
    print(f"抓到凶手：{r['caught']}　玩家投对：{r['player_correct']}")
    print(f"未发现线索：{len(r['missed_clues'])} 条")

    # ---- 断言 ----
    problems = []
    if len(g.votes) != n_voters:
        problems.append(f"投票人数不对：{len(g.votes)}")
    if counts.get("speech", 0) < n_voters * 3:
        problems.append(f"发言数偏少：{counts.get('speech', 0)}")
    if sum(held.values()) == 0:
        problems.append("没有任何人搜到线索")
    if not r["timeline"]:
        problems.append("复盘缺少时间线")

    # 最关键的一条：结束前，玩家可见状态里绝不能出现真相。
    # 注意角色 id / 姓名 / 公开身份本来就是公开的（roster），不算泄漏；
    # 要查的是真相专有的字段和文本。
    s = g.script
    leaks = []
    for pid in [c.id for c in s.characters if not c.is_npc]:      # NPC 不能被扮演
        blob = str(game_state(engine.new_game(script_id, pid), include_brief=True))
        me = s.char(pid)

        for marker in ("is_murderer", "cover_story", "is_red_herring", "points_to",
                       "minimum_clues_required", "reasoning_chain", "fake_timeline"):
            if marker in blob:
                leaks.append(f"[{pid}] 状态里出现了真相字段名 {marker}")

        for step in s.solution.reasoning_chain:
            if step and step in blob:
                leaks.append(f"[{pid}] 状态里出现了推理链原文")

        for c in s.characters:
            if c.id == pid:
                continue
            if c.private_secret and c.private_secret in blob:
                leaks.append(f"[{pid}] 状态里出现了 {c.id} 的秘密")

        # 未搜到的线索原文不得出现
        for k in s.clues:
            if k.text and k.text in blob:
                leaks.append(f"[{pid}] 开局状态里就出现了线索原文 {k.id}")

        # 非凶手的剧本里不得出现凶手专属内容
        if not me.is_murderer:
            killer = s.char(s.solution.murderer)
            if killer.cover_story:
                for f in killer.cover_story.fake_timeline:
                    if f.claimed_action and f.claimed_action in blob:
                        leaks.append(f"[{pid}] 非凶手却拿到了凶手口供")

    problems += leaks

    print()
    if problems:
        print("❌ 未通过：")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✅ 全流程通过，且开局状态无真相泄漏")
    return 0


def main() -> int:
    """默认跑一个本子；`--all` 把全部本子挨个跑一遍。

    `--all` 是这次改造的回归底线：任何一处引擎改动之后，
    21 个本子都必须照旧从开场打到复盘，阶段序列一格不差。
    """
    if "--all" in sys.argv:
        ids = [m["script_id"] for m in list_scripts()]
        bad, sigs = [], {}
        for sid in ids:
            print("=" * 62)
            rc = run_one(sid)
            if rc:
                bad.append(sid)
        print("=" * 62)
        print(f"{len(ids) - len(bad)}/{len(ids)} 个本子通过" + (f"，失败：{bad}" if bad else ""))
        return 1 if bad else 0
    return run_one(SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
