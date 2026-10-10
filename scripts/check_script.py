"""投稿前自检：一条命令告诉你，你的本子能不能上架。

    python scripts/check_script.py data/scripts/community/my_script.json
    python scripts/check_script.py my_script.json --no-play     # 只校验，不试玩
    python scripts/check_script.py my_script.json --strict      # 建议项也当错误

做五件事，由硬到软：

  1. JSON 能不能解析（出错告诉你第几行第几列，而不是一句 JSONDecodeError）
  2. 字段对不对（缺哪个必填、哪个字段类型错了，指到具体路径）
  3. 一致性校验：时间线矩阵、目击对称、凶手在案发现场、线索地点、合成链……
     就是游戏载入时会跑的那套 `validate_script`，过不了的本子根本上不了架
  4. **逐幕、逐角色**的信息泄漏检查——第一幕的视角里不能有别人的秘密，
     也不能有自己第三幕才揭晓的东西
  5. 离线试玩：不调模型，从开场一路打到复盘，抓卡死和崩溃

通过 1–5 才算合格。另外会给一批**建议**（线索太短、搜证次数和线索数对不上、
个人目标像是截断的病句……），不挡上架，但值得看一眼。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("SG_MOCK", "1")        # 试玩阶段不调真模型
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from backend import library  # noqa: E402
from backend.models import Script  # noqa: E402

HAN = re.compile(r"[一-鿿]")

_ERR_ZH = {
    "missing": "缺少必填字段",
    "string_type": "应该是文字",
    "int_type": "应该是整数",
    "bool_type": "应该是 true/false",
    "list_type": "应该是列表 [...]",
    "dict_type": "应该是对象 {...}",
    "literal_error": "取值不在允许范围内",
    "float_type": "应该是数字",
}


def han_count(obj) -> int:
    """递归数一个 JSON 对象里所有字符串值的汉字个数。"""
    if isinstance(obj, str):
        return len(HAN.findall(obj))
    if isinstance(obj, dict):
        return sum(han_count(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(han_count(v) for v in obj)
    return 0


def _fail(msg: str) -> None:
    print(f"\n❌ {msg}")


def load_json(path: Path):
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        lines = text.splitlines()
        ctx = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
        _fail(f"JSON 解析失败：第 {e.lineno} 行第 {e.colno} 列 —— {e.msg}")
        print(f"   {ctx[:120]}")
        print(f"   {' ' * max(0, e.colno - 1)}^")
        print("   常见原因：字符串里的英文双引号没转义（写成 \\\"）、最后一个元素后多了逗号、"
              "文字里直接换行了（要写成 \\n）。")
        return None


def explain_validation(e: ValidationError) -> None:
    for err in e.errors()[:25]:
        loc = " → ".join(str(x) for x in err["loc"])
        zh = _ERR_ZH.get(err["type"], err["msg"])
        print(f"   · {loc}：{zh}")
    if len(e.errors()) > 25:
        print(f"   …另有 {len(e.errors()) - 25} 处")


# --------------------------------------------------------------------------
# 建议项（不挡上架）
# --------------------------------------------------------------------------

# 以介词/连词结尾，几乎一定是没写完。
# 不含「的」「了」：「……是你签的」「……回来摊牌的」都是完整句子，之前误报了一片。
_TRUNC_END = re.compile(r"(对|把|被|向|给|替|跟|和|与|为|将)$")
# 「让人对 X」后面没有动词。线上 22 条被截断的目标就是这个形状：
# 「不要让人对那沓修缮发票」「别让人对储藏室的清单」——原句是「…起疑」「…追问」。
_TRUNC_MID = re.compile(r"让(?:人|别人|旁人|大家)对[^，。；,]{2,24}$")


def lint(s: Script) -> list[str]:
    out: list[str] = []
    suspects = [c for c in s.characters if not c.is_npc]
    real = [k for k in s.clues if not k.synth]
    budget = s.meta.search_budget or [2, 1, 1][: s.meta.acts]
    draws = len(suspects) * sum(budget)

    # 搜证次数 vs 线索数：这是「一局不到半小时」的根因
    if len(real) < draws * 0.6:
        out.append(f"可搜线索只有 {len(real)} 条，但全场一共要搜 {draws} 次——"
                   f"至少 {draws - len(real)} 次会空手（「已经被翻遍了」）。多写线索，或调低 search_budget。")
    if len(real) > draws * 2.2:
        out.append(f"可搜线索 {len(real)} 条，全场只搜 {draws} 次——"
                   f"大半线索永远没人看见。调高 search_budget，或者把一部分做成合成线索的原料。")

    # 线索太短
    thin = [k.id for k in real if len(HAN.findall(k.text)) < 14]
    if thin:
        out.append(f"{len(thin)} 条线索不足 14 个汉字（{', '.join(thin[:6])}…）。"
                   "线索读完就用完的话，玩家不需要动脑——写出能和别的线索对上的细节。")

    # 目标文本像是截断的
    for c in suspects:
        for g in c.personal_goals:
            t = g.text.strip()
            if len(t) < 6 or _TRUNC_END.search(t) or _TRUNC_MID.search(t):
                out.append(f"{c.name} 的目标「{t}」读起来像没写完。")

    # 三幕本每个嫌疑人都该有追加剧本
    if s.meta.acts >= 3:
        for c in suspects:
            have = {b.act for b in c.acts}
            miss = [a for a in range(2, s.meta.acts + 1) if a not in have]
            if miss:
                out.append(f"{c.name} 缺第 {miss} 幕的追加剧本——这一幕他会什么新东西都收不到。")
        if not s.solution.questions:
            out.append("三幕本没写答卷（solution.questions）。票对了人就算赢，三幕的曲折就白费了。")
        if not any(k.synth for k in s.clues):
            out.append("三幕本没有一条合成线索。线索全是「读完即用完」，推理没有『拼』这一步。")

    # 合成原料不该是红鲱鱼
    by = {k.id: k for k in s.clues}
    for k in s.clues:
        if k.synth:
            bad = [f for f in k.from_clues if f in by and by[f].is_red_herring]
            if bad:
                out.append(f"合成线索 {k.id} 的原料 {bad} 是红鲱鱼——玩家会拼出一个看似有用的假结论。"
                           "如果这是刻意设计的陷阱可以忽略。")

    if len(s.solution.reasoning_chain) < 3:
        out.append("推理链少于 3 步——复盘时没东西可讲。")
    if len(s.solution.minimum_clues_required) < 2:
        out.append("关键线索少于 2 条——一条线索就能破案，没有推理可言。")

    # 凶手口供覆盖
    for c in s.characters:
        if c.is_murderer and c.cover_story:
            covered = {f.slot for f in c.cover_story.fake_timeline}
            if len(covered) < len(s.setting.time_slots) // 2:
                out.append(f"凶手的假口供只覆盖了 {len(covered)}/{len(s.setting.time_slots)} 个时段，"
                           "被追问到没覆盖的时段时他得现编，容易崩。")

    # 难度排序并列
    from collections import Counter
    for loc in {k.location for k in real}:
        dup = [d for d, n in Counter(k.difficulty for k in real if k.location == loc).items() if n > 1]
        if dup:
            out.append(f"「{loc}」有多条线索难度相同（{dup}），搜到的先后顺序会按 id 排——"
                       "如果先后很重要，请给不同的 difficulty。")
            break
    return out


def stats(raw: dict) -> dict[str, int]:
    chars = raw.get("characters", [])
    return {
        "角色（秘密/动机/性格/目标）": sum(
            han_count({k: v for k, v in c.items() if k not in ("acts", "cover_story")}) for c in chars),
        "追加剧本（各幕）": sum(han_count(c.get("acts", [])) for c in chars),
        "凶手假口供": sum(han_count(c.get("cover_story", {})) for c in chars),
        "时间线（行动/目击）": han_count(raw.get("timeline", [])),
        "线索": han_count(raw.get("clues", [])),
        "背景与幕间": han_count(raw.get("setting", {})) + han_count(raw.get("interludes", []))
        + han_count(raw.get("case", {})) + han_count(raw.get("meta", {})),
        "真相（推理链/答卷）": han_count(raw.get("solution", {})),
    }


# --------------------------------------------------------------------------
# 离线试玩
# --------------------------------------------------------------------------

def play_through(script_id: str, as_char: str) -> tuple[bool, str, list[str]]:
    from backend import engine
    from backend.engine import Phase
    from backend.main import game_state

    try:
        g = engine.new_game(script_id, as_char)
    except Exception as e:  # noqa: BLE001
        return False, f"开局失败：{e}", []
    seen: list[str] = []
    for _ in range(4000):
        if g.phase == Phase.DONE:
            break
        try:
            out = g.step()
            if g.phase.value not in seen:
                seen.append(g.phase.value)
            if out["waiting"] and out["action"]:
                st = game_state(g)
                others = [r for r in st["roster"]
                          if r["id"] != g.player_char_id and not r.get("is_npc")]
                a = out["action"]
                body = {
                    "continue": {"type": "continue"},
                    "speak": {"type": "speak", "text": "（自动试玩）", "reveal_clues": []},
                    "answer": {"type": "answer", "text": "（自动试玩）"},
                    "search": {"type": "search", "location": st["map"][g.seq % len(st["map"])]},
                    "ask": {"type": "ask", "target_id": others[0]["id"], "question": "你当晚在哪？"},
                    "private": {"type": "private", "target_id": others[0]["id"],
                                "text": "（自动试玩）", "show_clues": []},
                    "vote": {"type": "vote", "target_id": others[0]["id"]},
                    "deduce": {"type": "deduce", "answers": {
                        q["id"]: q["options"][0]["id"] for q in st.get("questions", [])}},
                }.get(a)
                if body is None:
                    return False, f"出现了没人处理的动作 {a!r}（phase={g.phase.value}）", seen
                g.act(body)
        except Exception as e:  # noqa: BLE001
            return False, f"在 {g.phase.value} 阶段崩溃：{type(e).__name__}: {e}", seen
    if g.phase != Phase.DONE:
        return False, f"4000 步还没结束，卡在 {g.phase.value}", seen
    if not g.result:
        return False, "打完了但没有产生 result", seen
    return True, "ok", seen


def main() -> int:
    ap = argparse.ArgumentParser(description="本子投稿前自检")
    ap.add_argument("path", type=Path)
    ap.add_argument("--no-play", action="store_true", help="只校验，不离线试玩")
    ap.add_argument("--strict", action="store_true", help="建议项也当错误")
    args = ap.parse_args()
    path: Path = args.path

    if not path.exists():
        _fail(f"找不到文件：{path}")
        return 1
    print(f"检查 {path}\n{'=' * 60}")

    raw = load_json(path)
    if raw is None:
        return 1
    print("✅ 1. JSON 可以解析")

    try:
        script = Script.model_validate(raw)
    except ValidationError as e:
        _fail(f"2. 字段不对（{len(e.errors())} 处）：")
        explain_validation(e)
        return 1
    print(f"✅ 2. 字段结构正确　《{script.meta.title}》 {len(script.characters)} 个角色 / "
          f"{script.meta.acts} 幕 / {len(script.clues)} 条线索")

    errors: list[str] = []
    if path.stem != script.script_id:
        errors.append(f"文件名「{path.stem}」要和 script_id「{script.script_id}」一致——"
                      "加载器靠文件名找本子。")
    errors += library.validate_script(script)
    if errors:
        _fail(f"3. 一致性校验未通过（{len(errors)} 项）：")
        for e in errors:
            print(f"   · {e}")
        return 1
    print("✅ 3. 一致性校验通过")

    leaks = []
    for act in range(1, script.meta.acts + 1):
        for c in script.characters:
            for lk in library.assert_no_leak(script, library.build_view(script, c.id, act=act)):
                leaks.append(f"第 {act} 幕 · {c.name}：{lk}")
    if leaks:
        _fail(f"4. 信息泄漏（{len(leaks)} 处）——这是最严重的一类，本子会作废：")
        for lk in leaks[:20]:
            print(f"   · {lk}")
        return 1
    n = script.meta.acts * len(script.characters)
    print(f"✅ 4. 逐幕泄漏检查通过（{script.meta.acts} 幕 × {len(script.characters)} 人 = {n} 个视角）")

    if not args.no_play:
        library.EXTRA_PATHS[script.script_id] = path.resolve()
        library.load_script.cache_clear()
        suspects = [c for c in script.characters if not c.is_npc]
        trials = [suspects[0].id]
        murderer = script.solution.murderer
        if murderer not in trials:
            trials.append(murderer)           # 玩家当凶手是另一条路径（不考答卷、不栽赃）
        amnesiac = next((c.id for c in suspects if c.amnesia_slots and c.id not in trials), None)
        if amnesiac:
            trials.append(amnesiac)
        for cid in trials:
            ok, why, seen = play_through(script.script_id, cid)
            who = script.char(cid).name + ("（凶手）" if cid == murderer else "")
            if not ok:
                _fail(f"5. 离线试玩失败（扮演 {who}）：{why}")
                if seen:
                    print(f"   走过的阶段：{' → '.join(seen)}")
                return 1
        print(f"✅ 5. 离线试玩通过（分别扮演 {'、'.join(script.char(t).name for t in trials)}，"
              f"共 {len(seen)} 个阶段）")
    else:
        print("⏭  5. 已跳过离线试玩")

    # ---- 报告 ----
    st = stats(raw)
    total = sum(st.values())
    print(f"\n{'─' * 60}\n字数统计（汉字）　合计 {total:,}")
    for k, v in sorted(st.items(), key=lambda kv: -kv[1]):
        print(f"   {k:<18}{v:>7,}  {'█' * max(1, v * 28 // max(1, max(st.values())))}")
    suspects = [c for c in script.characters if not c.is_npc]
    print(f"\n规模　嫌疑人 {len(suspects)}　证人 {len(script.characters) - len(suspects)}　"
          f"时段 {len(script.setting.time_slots)}　地点 {len(script.setting.map)}"
          f"（+{len({i.location for i in script.interludes if i.effect == 'unlock_location'})} 解锁）　"
          f"线索 {sum(1 for k in script.clues if not k.synth)}（+{sum(1 for k in script.clues if k.synth)} 合成）　"
          f"答卷 {len(script.solution.questions)} 题")

    tips = lint(script)
    if tips:
        print(f"\n💡 建议（{len(tips)} 条，不挡上架）：")
        for t in tips:
            print(f"   · {t}")
        if args.strict:
            _fail("--strict：建议项未清零")
            return 1
    print(f"\n{'=' * 60}\n🎉 合格。可以提交了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
