"""用大模型 API 生成《探案》的案件（data/cases/ai_*.json）。

和 scriptgen 同一套思路：分阶段写，每一阶段都建立在上一阶段已定的事实上，
最后用**可解性模拟**把关，不过就把问题交回模型修补。

《探案》是单人玩法，没有“角色视角”可以靠代码切分，所以靠两样东西保证能玩：
  1. 证据闭包：从起始场景出发，按「场景进入条件 / 热区打开条件 / 物证组合」一路推演，
     算出玩家**最终能拿到哪些物证**。关键物证、证词解锁物证、每个场景、每个热区，
     只要有一个在闭包之外，案子就是死局，不写入。
  2. 场景够用：下限写死——场景数、每个场景的可查热区数、物证数、人物证词层数——
     宁可多几个热区，也不能让玩家走进一个没东西可查的房间。

单独生成时从主题出发；和剧本杀一起生成时，会把剧本杀的设定（同一个死者、同一个凶手、
同一个诡计）改写成单人探案——同一个故事的两种玩法。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from pydantic import ValidationError

from . import llm
from .case_models import Case
from .config import DATA_DIR, ROOT, SETTINGS

CASES_DIR = DATA_DIR / "cases"
MAX_REPAIR_ROUNDS = 4

# ---- 场景够用的下限（写死，改小要有理由）----
MIN_SCENES = 6
MIN_HOTSPOTS_PER_SCENE = 3
MIN_HOTSPOTS_START = 4
MIN_EVIDENCE = 16
MIN_PEOPLE = 4
MIN_COMBOS = 2
MIN_KEY_EVIDENCE = 5


class CaseGenError(RuntimeError):
    pass


RULES = """【硬性规则——违反任何一条，案子都无法上架】
1. 这是单人探案：玩家是侦探，逐个场景搜查热区（可点的东西）、拿到物证、再拿物证去质问人物。
2. 物证只能从热区（hotspot）的 yields 拿到；热区可以要求先持有某件物证才能打开（requires），
   场景也可以要求先持有某件物证才能进入（requires）。但**不能形成死锁**：起始场景必须能直接进入，
   而每一个要求最终都必须能被满足。
3. 每个人物有三层证词：tier 0（初见，他愿意说的）、tier 1（玩家摆出某件物证 unlocked_by 之后）、
   tier 2（再摆出另一件物证之后，他兜不住了）。凶手三层都要有；其他人至少 tier 0 和 tier 1。
4. 真相只写在 solution 和 tier 2 里；tier 0 的 knows 里不能出现凶手身份或关键真相。
5. 两件物证可以“凑在一起才看出问题”（combines_with / combines_into / combine_note），至少 2 组。
6. 场景名、人物 id、物证 id、热区 id 必须前后完全一致，不能自创、不能改字。
7. 全部用中文，口语化、有画面感；热区描述里最关键的一句用 **加粗** 标出来。
8. 只输出一个 JSON 对象，不要任何解释、不要 markdown 围栏。"""


def _sys() -> dict:
    return {"role": "system", "content": "你是一位资深的推理游戏作者，擅长设计让玩家“看出来”而不是“猜出来”的案子："
            "每条线索都有可验证的物证细节，凶手的谎言会被具体证据戳穿。"}


def _ask(prompt: str, *, max_tokens: int = 6000, temperature: float = 0.8, model: str | None = None) -> Any:
    msgs = [_sys(), {"role": "user", "content": prompt}]
    last = ""
    for attempt in range(3):
        # 输出被截断是最常见的失败原因（括号没闭合）：每重试一次就把上限放宽 60%
        text = llm.chat(msgs, model=model, temperature=temperature if attempt == 0 else 0.3,
                        max_tokens=min(16000, int(max_tokens * (1 + 0.6 * attempt))), json_mode=True)
        last = text
        try:
            return llm.extract_json(text)
        except Exception as exc:                                   # noqa: BLE001
            msgs = msgs + [{"role": "assistant", "content": text[:3000]},
                           {"role": "user", "content": f"上面不是合法 JSON（{str(exc)[:120]}）。请只输出完整、合法的 JSON 对象。"}]
    raise CaseGenError(f"模型连续 3 次没有给出合法 JSON：{last[:200]}")


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------


def _outline_prompt(p: dict, bible: dict | None) -> str:
    if bible:
        base = f"""把下面这个剧本杀的故事改编成**单人探案**：同一个死者、同一个凶手、同一个诡计、同一个舞台，
但玩法变成一个侦探逐处搜查、逐人质问。

书名：{bible['title']}　背景：{bible['background']}
死者：{bible['victim']}（{bible['death_slot']} 死于{bible['death_location']}，{bible['cause']}，凶器 {bible['weapon']}）
凶手：{next(c['name'] for c in bible['characters'] if c['id'] == bible['murderer'])}
角色：{[(c['name'], c['role']) for c in bible['characters']]}
地图（可作为场景的蓝本，可增补）：{bible['map']}
真相：{bible['truth']}
诡计：{bible['trick']}"""
    else:
        base = f"""请为下面的主题设计一个单人探案的案子。

主题：{p['theme']}
{('书名：' + p['title']) if p.get('title') else ''}难度：{p['difficulty']}"""
    return f"""{base}

{RULES}

先输出案子的“骨架”，字段如下（全部必填）：
{{
  "title": "案名，2–8 字", "tagline": "一句话钩子，不剧透",
  "brief": "开场 150–250 字：玩家是谁、发生了什么、为什么今晚谁都走不了",
  "victim": "死者姓名",
  "scenes": [
    {{"id": "s_xxx（ASCII）", "name": "场景名", "gist": "这里是什么样的地方，一句话",
      "requires": "进入需要的物证 id（起始场景必须为空；其余大多为空，最多 1–2 个场景需要）",
      "locked_hint": "没满足条件时的提示，一句话", "people": ["在此处的人物 id"]}}
    // 共 {MIN_SCENES}–8 个，第一个是起始场景（案发现场）
  ],
  "people": [
    {{"id": "p_xxx", "name": "姓名", "role": "身份", "is_culprit": false, "gist": "他今晚做了什么、瞒着什么，60–100 字"}}
    // 共 {MIN_PEOPLE}–6 个，恰好 1 个凶手；每个人必须出现在某个场景的 people 里
  ],
  "evidence": [
    {{"id": "e_xxx", "name": "物证名", "detail": "证物栏里的说明，40–90 字，含具体细节",
      "found_in": "场景 id", "kind": "key / red / plain",
      "combines_with": "可选：与哪件物证凑一起", "combines_into": "可选：凑出的新物证 id（也要在 evidence 里，found_in 留空）",
      "combine_note": "可选：凑成当场的旁白 50–100 字"}}
    // 共 {MIN_EVIDENCE}–22 个（含凑出来的）。每个场景至少放 2 个；
    // kind=key 的至少 {MIN_KEY_EVIDENCE} 个且互相印证能戳穿凶手；kind=red 的至少 2 个（误导性物证）
  ],
  "solution": {{
    "culprit": "凶手人物 id",
    "method": "正确手法的选项 id", "motive": "正确动机的选项 id",
    "method_options": [{{"id": "m_a", "text": "..."}}, "... 共 4 个，错的要合理"],
    "motive_options": [{{"id": "v_a", "text": "..."}}, "... 共 4 个"],
    "explanation": "全对时的完整还原，200–350 字",
    "key_evidence": ["{MIN_KEY_EVIDENCE}–8 个关键物证 id（不含 red）"]
  }}
}}"""


def _scene_prompt(outline: dict, sc: dict, ev_here: list[dict], is_start: bool) -> str:
    need = max(MIN_HOTSPOTS_START if is_start else MIN_HOTSPOTS_PER_SCENE, len(ev_here) + 2)
    items = "\n".join(f"- {e['id']}｜{e['name']}｜{e['detail']}" for e in ev_here) or "（这里不放物证）"
    ppl = "、".join(next((p['name'] for p in outline['people'] if p['id'] == x), x) for x in sc.get("people", []))
    return f"""请写场景“{sc['name']}”（id={sc['id']}）的入场描述和全部热区。

案子：{outline['title']}　{outline['brief']}
真相（只有你知道，别直接抄进描述）：{outline.get('truth', outline['solution']['explanation'])}
场景：{sc['name']}——{sc.get('gist', '')}　{'（起始场景，玩家一进来就在这里）' if is_start else ''}
在场人物：{ppl or '无'}
这个场景要放的物证（每件必须由一个热区的 yields 给出）：
{items}

{RULES}

要求：
- 至少 {need} 个热区；放物证的热区 yields 填物证 id；另外至少 2 个热区是“只能看、不给物证”的（yields 留空），
  它们要么交代环境、要么是误导或排除，让玩家有东西可看、不会觉得空；
- 可以让个别热区需要先持有另一件物证才能打开（requires 填物证 id，locked_text 写打不开时看到什么），
  但 requires 的物证必须是**别的场景**或本场景**无 requires 的热区**能拿到的；
- intro 80–160 字，进门第一眼的画面，有声音气味光线，不剧透；
- 热区 describe 60–140 字，最关键的一句用 **加粗**。

输出 JSON：
{{"intro": "...", "hotspots": [{{"id": "hs_xxx（ASCII，全案唯一）", "label": "「书桌抽屉」之类，直接当按钮文字",
  "describe": "...", "yields": "物证 id 或空", "requires": "", "locked_text": ""}}]}}"""


def _person_prompt(outline: dict, pe: dict) -> str:
    culprit = pe["id"] == outline["solution"]["culprit"]
    evs = "\n".join(f"- {e['id']}｜{e['name']}：{e['detail'][:50]}" for e in outline["evidence"]
                    if e.get("kind") != "red" or not culprit)
    return f"""请写人物“{pe['name']}”（id={pe['id']}，{pe['role']}）。{'他是凶手。' if culprit else '他不是凶手。'}

案子：{outline['title']}　{outline['brief']}
死者：{outline['victim']}
真相（只有你知道）：{outline['solution']['explanation']}
他今晚：{pe['gist']}

可用来撬开他嘴的物证（unlocked_by 只能从这里选，且必须是玩家真能拿到的）：
{evs}

{RULES}

输出 JSON：
{{
  "personality": "性格与小动作，40–80 字",
  "speech_style": "说话风格，一两句",
  "hair": "short|buzz|bun|long|updo|balding|bald 之一", "hair_color": "black|darkbrown|gray|white|auburn 之一",
  "skin": "light|mid|tan|deep 之一", "garment": "indigo|crimson|olive|plum|teal|ochre|slate|rust|moss|wine 之一",
  "accessory": "none|glasses|round_glasses|mustache|beard|scarf|goggles|earrings 之一",
  "testimonies": [
    {{"tier": 0, "mood": "情绪", "knows": ["他愿意说的事实，3–5 条，第二人称“你”指玩家不要用，用客观陈述"], "denies": ["他此刻硬撑的说法，1–3 条"]}},
    {{"tier": 1, "unlocked_by": "物证 id", "reaction": "被摆出这件物证时的当场反应，50–120 字，可含对白",
      "mood": "...", "knows": ["改口后愿意承认的，2–4 条"], "denies": ["仍在否认的，1–2 条"]}}
    {',' if culprit else ''}{'{"tier": 2, "unlocked_by": "另一件物证 id", "reaction": "...", "mood": "防线塌了", "knows": ["承认全部真相，3–5 条"], "denies": ["仍嘴硬的一条"]}' if culprit else ''}
  ]
}}
{'非凶手的 tier 1 应该是他承认自己隐瞒的那件小事（与凶案无关或只是间接相关），并提供一条指向真相的新信息。' if not culprit else ''}"""


def _fix_prompt(case: dict, problems: list[str]) -> str:
    return f"""下面是一个单人探案案子的当前稿件（JSON），自检发现了这些问题：

{chr(10).join('- ' + x for x in problems[:25])}

{RULES}

请只返回需要修改的部分，格式：
{{
  "scenes": {{"场景id": {{"要覆盖的字段如 name/intro/requires/people": "新值"}}}},
  "hotspots": {{"场景id": [{{"完整热区对象（按 id 覆盖或新增）"}}]}},
  "delete_hotspots": ["热区 id"],
  "evidence": [{{"完整物证对象（按 id 覆盖或新增）"}}],
  "people": {{"人物id": {{"要覆盖的字段，testimonies 要给完整列表": "新值"}}}},
  "solution": {{"要覆盖的字段": "新值"}},
  "new_scenes": [{{"id": "...", "name": "...", "intro": "...", "people": [], "hotspots": [完整热区...]}}]
}}
常见修法：某件物证拿不到——给它加一个无 requires 的热区去 yields；场景热区太少——补热区（至少 {MIN_HOTSPOTS_PER_SCENE} 个，
起始场景至少 {MIN_HOTSPOTS_START} 个，其中至少 1 个是只能看的环境热区）；人物不在任何场景——把他加进某个场景的 people；
requires 形成死锁——去掉或改成别处能拿到的物证。

当前稿件：
{json.dumps(case, ensure_ascii=False)}"""


# --------------------------------------------------------------------------
# 校验：结构 + 可解性模拟 + 场景够用
# --------------------------------------------------------------------------


def reachable_evidence(c: Case) -> tuple[set[str], set[str], set[str]]:
    """从起始场景出发推演，返回 (拿得到的物证, 进得去的场景, 打得开的热区)。"""
    held: set[str] = set()
    scenes: set[str] = set()
    spots: set[str] = set()
    changed = True
    while changed:
        changed = False
        for s in c.scenes:
            if s.id in scenes:
                continue
            if s.id == c.start_scene or not s.requires or s.requires in held:
                scenes.add(s.id)
                changed = True
        for s in c.scenes:
            if s.id not in scenes:
                continue
            for h in s.hotspots:
                if h.id in spots:
                    continue
                if not h.requires or h.requires in held:
                    spots.add(h.id)
                    changed = True
                    if h.yields:
                        held.add(h.yields)
        for e in c.evidence:
            if (e.combines_into and e.combines_into not in held
                    and e.id in held and e.combines_with in held):
                held.add(e.combines_into)
                changed = True
    return held, scenes, spots


def check_case(d: dict, strict: bool = True) -> list[str]:
    try:
        c = Case(**d)
    except ValidationError as e:
        return [f"字段不对: {' → '.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:12]]
    p: list[str] = []
    sids = [s.id for s in c.scenes]
    eids = [e.id for e in c.evidence]
    pids = [x.id for x in c.people]
    hids = [h.id for s in c.scenes for h in s.hotspots]
    for what, ids in (("场景", sids), ("物证", eids), ("人物", pids), ("热区", hids)):
        if len(set(ids)) != len(ids):
            p.append(f"{what} id 有重复")
    if c.start_scene not in sids:
        p.append(f"起始场景 {c.start_scene} 不存在")
        return p
    if c.scene(c.start_scene).requires:
        p.append("起始场景不能有进入条件")

    for s in c.scenes:
        if s.requires and s.requires not in eids:
            p.append(f"场景「{s.name}」的进入条件 {s.requires} 不是物证")
        for x in s.people:
            if x not in pids:
                p.append(f"场景「{s.name}」里的人物 {x} 不存在")
        for h in s.hotspots:
            if h.yields and h.yields not in eids:
                p.append(f"热区 {h.id} 给出的物证 {h.yields} 不存在")
            if h.requires and h.requires not in eids:
                p.append(f"热区 {h.id} 的打开条件 {h.requires} 不是物证")
    for e in c.evidence:
        if e.combines_with and e.combines_with not in eids:
            p.append(f"物证 {e.id} 要凑的 {e.combines_with} 不存在")
        if e.combines_into and e.combines_into not in eids:
            p.append(f"物证 {e.id} 凑出的 {e.combines_into} 不存在")
        if bool(e.combines_with) != bool(e.combines_into):
            p.append(f"物证 {e.id} 的 combines_with / combines_into 必须成对出现")

    cul = [x.id for x in c.people if x.is_culprit]
    if len(cul) != 1:
        p.append(f"必须恰好 1 个凶手，实际 {cul}")
    if c.solution.culprit not in pids:
        p.append("solution.culprit 不在人物里")
    elif cul and c.solution.culprit != cul[0]:
        p.append("solution.culprit 与 is_culprit 不一致")
    if c.solution.method not in [o.id for o in c.solution.method_options]:
        p.append("solution.method 不在 method_options 里")
    if c.solution.motive not in [o.id for o in c.solution.motive_options]:
        p.append("solution.motive 不在 motive_options 里")
    for k in c.solution.key_evidence:
        if k not in eids:
            p.append(f"关键物证 {k} 不存在")

    placed = {x for s in c.scenes for x in s.people}
    for x in c.people:
        if x.id not in placed:
            p.append(f"人物「{x.name}」不在任何场景里，玩家见不到他")
        tiers = sorted(t.tier for t in x.testimonies)
        if not tiers or tiers[0] != 0:
            p.append(f"人物「{x.name}」缺 tier 0 证词")
        elif tiers != list(range(len(tiers))):
            p.append(f"人物「{x.name}」的证词层不连续：{tiers}")
        if x.is_culprit and len(tiers) < 3:
            p.append(f"凶手「{x.name}」要有 3 层证词")
        if not x.is_culprit and len(tiers) < 2:
            p.append(f"人物「{x.name}」至少要有 tier 0 和 tier 1")
        for t in x.testimonies:
            if t.tier >= 1:
                if not t.unlocked_by:
                    p.append(f"「{x.name}」tier {t.tier} 缺 unlocked_by")
                elif t.unlocked_by not in eids:
                    p.append(f"「{x.name}」tier {t.tier} 的 unlocked_by {t.unlocked_by} 不是物证")
                if not t.reaction:
                    p.append(f"「{x.name}」tier {t.tier} 缺 reaction（被摆证据时的当场反应）")

    # ---- 可解性：证据闭包 ----
    held, ok_scenes, ok_spots = reachable_evidence(c)
    for s in c.scenes:
        if s.id not in ok_scenes:
            p.append(f"场景「{s.name}」永远进不去（进入条件 {s.requires} 拿不到，或形成死锁）")
    for s in c.scenes:
        for h in s.hotspots:
            if s.id in ok_scenes and h.id not in ok_spots:
                p.append(f"热区 {h.id}（{h.label}）永远打不开：需要的 {h.requires} 拿不到")
    for e in c.evidence:
        if e.id not in held:
            p.append(f"物证「{e.name}」({e.id}) 玩家永远拿不到：没有可达的热区给它，也凑不出来")
    for k in c.solution.key_evidence:
        if k in eids and k not in held:
            p.append(f"关键物证 {k} 拿不到——案子无解")
    for x in c.people:
        if x.id in placed and not any(x.id in s.people for s in c.scenes if s.id in ok_scenes):
            p.append(f"人物「{x.name}」只出现在进不去的场景里")
        for t in x.testimonies:
            if t.tier >= 1 and t.unlocked_by in eids and t.unlocked_by not in held:
                p.append(f"「{x.name}」tier {t.tier} 要的物证 {t.unlocked_by} 拿不到，这一层永远撬不开")

    if not strict:                  # 离线示例（现成案件）不卡数量下限，只卡可解性
        return p

    # ---- 场景够用 ----
    if len(c.scenes) < MIN_SCENES:
        p.append(f"场景只有 {len(c.scenes)} 个，至少要 {MIN_SCENES} 个")
    for s in c.scenes:
        need = MIN_HOTSPOTS_START if s.id == c.start_scene else MIN_HOTSPOTS_PER_SCENE
        if len(s.hotspots) < need:
            p.append(f"场景「{s.name}」只有 {len(s.hotspots)} 个热区，至少要 {need} 个——玩家走进来会没东西可查")
        if not [h for h in s.hotspots if not h.yields]:
            p.append(f"场景「{s.name}」没有“只能看”的环境热区，要至少 1 个")
        if len(s.intro) < 30:
            p.append(f"场景「{s.name}」的入场描述太短")
    if len(c.evidence) < MIN_EVIDENCE:
        p.append(f"物证只有 {len(c.evidence)} 件，至少要 {MIN_EVIDENCE} 件")
    if len(c.people) < MIN_PEOPLE:
        p.append(f"人物只有 {len(c.people)} 个，至少要 {MIN_PEOPLE} 个")
    if sum(1 for e in c.evidence if e.combines_into) < MIN_COMBOS:
        p.append(f"可组合的物证不足 {MIN_COMBOS} 组")
    if len(c.solution.key_evidence) < MIN_KEY_EVIDENCE:
        p.append(f"关键物证至少 {MIN_KEY_EVIDENCE} 个，现在 {len(c.solution.key_evidence)} 个")
    if len(c.solution.method_options) < 3 or len(c.solution.motive_options) < 3:
        p.append("手法/动机选项至少各 3 个")
    return p


# --------------------------------------------------------------------------
# 补丁
# --------------------------------------------------------------------------


def _by_id(items: list[dict]) -> dict[str, dict]:
    return {x["id"]: x for x in items if isinstance(x, dict) and x.get("id")}


def apply_fix(case: dict, fix: dict) -> None:
    scenes = _by_id(case["scenes"])
    for sid, patch in (fix.get("scenes") or {}).items():
        if sid in scenes and isinstance(patch, dict):
            for k, v in patch.items():
                if k in ("name", "intro", "requires", "locked_hint", "people"):
                    scenes[sid][k] = v
    for sid, hs in (fix.get("hotspots") or {}).items():
        if sid in scenes and isinstance(hs, list):
            cur = _by_id(scenes[sid]["hotspots"])
            for h in hs:
                if isinstance(h, dict) and h.get("id"):
                    cur[h["id"]] = _norm_hotspot(h)
            scenes[sid]["hotspots"] = list(cur.values())
    kill = set(fix.get("delete_hotspots") or [])
    if kill:
        for s in case["scenes"]:
            s["hotspots"] = [h for h in s["hotspots"] if h["id"] not in kill]
    ev = _by_id(case["evidence"])
    for e in fix.get("evidence") or []:
        if isinstance(e, dict) and e.get("id"):
            ev[e["id"]] = _norm_evidence(e)
    case["evidence"] = list(ev.values())
    people = _by_id(case["people"])
    for pid, patch in (fix.get("people") or {}).items():
        if pid in people and isinstance(patch, dict):
            for k, v in patch.items():
                if k != "id":
                    people[pid][k] = v
    for k, v in (fix.get("solution") or {}).items():
        if k in case["solution"]:
            case["solution"][k] = v
    for ns in fix.get("new_scenes") or []:
        if isinstance(ns, dict) and ns.get("id") and ns["id"] not in scenes:
            case["scenes"].append({
                "id": ns["id"], "name": str(ns.get("name", ns["id"])), "intro": str(ns.get("intro", "")),
                "people": [x for x in ns.get("people", []) if isinstance(x, str)],
                "requires": str(ns.get("requires", "")), "locked_hint": str(ns.get("locked_hint", "")),
                "hotspots": [_norm_hotspot(h) for h in ns.get("hotspots", []) if isinstance(h, dict)],
            })


def _norm_hotspot(h: dict) -> dict:
    return {"id": str(h.get("id", "")), "label": str(h.get("label", "查看")),
            "describe": str(h.get("describe", "")), "yields": str(h.get("yields", "") or ""),
            "requires": str(h.get("requires", "") or ""), "locked_text": str(h.get("locked_text", "") or ""),
            "once": True}


def _norm_evidence(e: dict) -> dict:
    return {"id": str(e.get("id", "")), "name": str(e.get("name", "")), "detail": str(e.get("detail", "")),
            "combines_with": str(e.get("combines_with", "") or ""), "combines_into": str(e.get("combines_into", "") or ""),
            "combine_note": str(e.get("combine_note", "") or "")}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def generate(params: dict, bible: dict | None = None,
             progress: Callable[[float, str], None] | None = None, *, title_hint: str = "") -> dict:
    """返回 {"case_id","path","title","scenes","evidence","people"}。"""

    def tick(f: float, msg: str) -> None:
        if progress:
            progress(min(0.99, f), msg)

    p = dict(params)
    p.setdefault("difficulty", "medium")
    if SETTINGS.mock:
        return _offline_demo(p, tick)

    tick(0.0, "探案：正在设计案子骨架…")
    outline = _ask(_outline_prompt(p, bible), max_tokens=8000, model=SETTINGS.model_reason)
    _clean_outline(outline, p, bible)
    tick(0.2, f"探案《{outline['title']}》：{len(outline['scenes'])} 个场景、{len(outline['people'])} 个人物、{len(outline['evidence'])} 件物证")

    # ---- 场景（并行）----
    def one_scene(sc: dict) -> tuple[str, dict]:
        here = [e for e in outline["evidence"] if e.get("found_in") == sc["id"]]
        return sc["id"], _ask(_scene_prompt(outline, sc, here, sc["id"] == outline["scenes"][0]["id"]), max_tokens=5000)

    scene_out: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for sid, data in ex.map(one_scene, outline["scenes"]):
            scene_out[sid] = data
            done += 1
            tick(0.2 + 0.4 * done / len(outline["scenes"]), f"探案：已写好 {done}/{len(outline['scenes'])} 个场景")

    # ---- 人物（并行）----
    def one_person(pe: dict) -> tuple[str, dict]:
        return pe["id"], _ask(_person_prompt(outline, pe), max_tokens=7000)

    people_out: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for pid, data in ex.map(one_person, outline["people"]):
            people_out[pid] = data
            done += 1
            tick(0.6 + 0.25 * done / len(outline["people"]), f"探案：已写好 {done}/{len(outline['people'])} 个人物的证词")

    case = _assemble(p, outline, scene_out, people_out, title_hint)
    sanitize(case)
    _ensure_yield_spots(case)

    problems = check_case(case)
    rnd = 0
    while problems and rnd < MAX_REPAIR_ROUNDS:
        rnd += 1
        tick(0.85 + 0.12 * rnd / MAX_REPAIR_ROUNDS, f"探案：自检发现 {len(problems)} 处问题，第 {rnd} 轮修补…")
        try:
            fix = _ask(_fix_prompt(case, problems), max_tokens=8000, temperature=0.4)
            apply_fix(case, fix if isinstance(fix, dict) else {})
            sanitize(case)
            _ensure_yield_spots(case)
        except CaseGenError:
            pass
        problems = check_case(case)
    if problems:                                   # 兜底：去掉仍然卡死的门槛，再验一次
        try:
            unlock_dead_ends(case)
            sanitize(case)
            _ensure_yield_spots(case)
        except Exception:                          # noqa: BLE001
            pass
        problems = check_case(case)
    if problems:
        raise CaseGenError("探案案子自检仍有问题，没有写入：\n" + "\n".join("- " + x for x in problems[:12]))
    return _write(case)


def _clean_outline(o: dict, p: dict, bible: dict | None) -> None:
    need = ("title", "tagline", "brief", "victim", "scenes", "people", "evidence", "solution")
    miss = [k for k in need if not o.get(k)]
    if miss:
        raise CaseGenError(f"探案骨架缺字段：{miss}")
    for i, s in enumerate(o["scenes"]):
        s["id"] = re.sub(r"\W", "_", str(s.get("id") or f"s{i + 1}"))
        s.setdefault("people", [])
        s["requires"] = "" if i == 0 else str(s.get("requires", "") or "")
    for i, e in enumerate(o["evidence"]):
        e["id"] = re.sub(r"\W", "_", str(e.get("id") or f"e{i + 1}"))
        e["detail"] = str(e.get("detail") or e.get("name", ""))
        e["kind"] = e.get("kind") if e.get("kind") in ("key", "red", "plain") else "plain"
    sid0 = {s["id"] for s in o["scenes"]}
    eids = {e["id"] for e in o["evidence"]}
    for s in o["scenes"]:
        if s["requires"] not in eids:
            s["requires"] = ""
    for e in o["evidence"]:
        if e.get("found_in") not in sid0 and not e.get("combines_into_of"):
            e["found_in"] = o["scenes"][0]["id"] if not e.get("found_in") else e["found_in"]
    culprits = [x for x in o["people"] if x.get("is_culprit")]
    if len(culprits) != 1:
        cid = o["solution"].get("culprit")
        for x in o["people"]:
            x["is_culprit"] = x.get("id") == cid
    if p.get("title") and not bible:
        o["title"] = p["title"]
    o["truth"] = o["solution"].get("explanation", "")


def _assemble(p: dict, o: dict, scene_out: dict, people_out: dict, title_hint: str) -> dict:
    scenes = []
    for sc in o["scenes"]:
        d = scene_out.get(sc["id"], {})
        scenes.append({
            "id": sc["id"], "name": str(sc["name"]), "intro": str(d.get("intro") or sc.get("gist", "")),
            "people": [x for x in sc.get("people", []) if isinstance(x, str)],
            "requires": sc.get("requires", ""), "locked_hint": str(sc.get("locked_hint", "") or "过不去。"),
            "hotspots": [_norm_hotspot(h) for h in d.get("hotspots", []) if isinstance(h, dict) and h.get("id")],
        })
    ids = set()
    for s in scenes:                                   # 热区 id 全案唯一
        for h in s["hotspots"]:
            while h["id"] in ids:
                h["id"] += "_"
            ids.add(h["id"])
    people = []
    for pe in o["people"]:
        d = people_out.get(pe["id"], {})
        tests = []
        for t in d.get("testimonies", []) if isinstance(d.get("testimonies"), list) else []:
            if isinstance(t, dict) and isinstance(t.get("tier"), int):
                tests.append({
                    "tier": t["tier"], "unlocked_by": str(t.get("unlocked_by", "") or ""),
                    "reaction": str(t.get("reaction", "") or ""), "mood": str(t.get("mood", "") or ""),
                    "knows": [str(x) for x in t.get("knows", [])], "denies": [str(x) for x in t.get("denies", [])],
                })
        people.append({
            "id": pe["id"], "name": pe["name"], "role": pe["role"], "is_culprit": bool(pe.get("is_culprit")),
            "personality": str(d.get("personality", "沉默寡言")), "speech_style": str(d.get("speech_style", "话不多")),
            "avatar": {"hair": d.get("hair", "short"), "hair_color": d.get("hair_color", "black"),
                       "skin": d.get("skin", "mid"), "garment": d.get("garment", "slate"),
                       "accessory": d.get("accessory", "none")},
            "testimonies": sorted(tests, key=lambda t: t["tier"]),
        })
    sol = o["solution"]
    return {
        "case_id": "case_ai_" + time.strftime("%y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4],
        "title": title_hint or o["title"], "tagline": str(o["tagline"]), "brief": str(o["brief"]),
        "victim": o["victim"], "detective_name": "你", "start_scene": o["scenes"][0]["id"],
        "author": "AI 生成",
        "scenes": scenes, "people": people,
        "evidence": [_norm_evidence(e) for e in o["evidence"]],
        "solution": {
            "culprit": sol["culprit"], "method": sol["method"], "motive": sol["motive"],
            "method_options": [{"id": str(x["id"]), "text": str(x["text"])} for x in sol["method_options"] if isinstance(x, dict)],
            "motive_options": [{"id": str(x["id"]), "text": str(x["text"])} for x in sol["motive_options"] if isinstance(x, dict)],
            "explanation": str(sol["explanation"]),
            "key_evidence": [str(x) for x in sol.get("key_evidence", [])],
        },
    }


def sanitize(case: dict) -> None:
    """机械地清掉悬空引用——这些靠模型修补往往反复修不好，而清掉的代价只是少一道门槛。"""
    eids = {e["id"] for e in case["evidence"]}
    pids = {p["id"] for p in case["people"]}
    for s in case["scenes"]:
        if s.get("requires") and s["requires"] not in eids:
            s["requires"] = ""
        s["people"] = [x for x in s.get("people", []) if x in pids]
        for h in s["hotspots"]:
            if h.get("requires") and h["requires"] not in eids:
                h["requires"], h["locked_text"] = "", ""
            if h.get("yields") and h["yields"] not in eids:
                h["yields"] = ""
    for e in case["evidence"]:
        if e.get("combines_with") not in eids or e.get("combines_into") not in eids:
            e["combines_with"] = e["combines_into"] = e["combine_note"] = ""
    case["solution"]["key_evidence"] = [k for k in case["solution"].get("key_evidence", []) if k in eids]
    for p in case["people"]:
        for t in p["testimonies"]:
            if t.get("tier", 0) >= 1 and t.get("unlocked_by") not in eids and case["solution"]["key_evidence"]:
                t["unlocked_by"] = case["solution"]["key_evidence"][min(t["tier"] - 1, len(case["solution"]["key_evidence"]) - 1)]


def unlock_dead_ends(case: dict) -> None:
    """最后的兜底：仍然进不去/打不开的场景和热区，直接去掉门槛。"""
    c = Case(**case)
    _, ok_scenes, ok_spots = reachable_evidence(c)
    for s in case["scenes"]:
        if s["id"] not in ok_scenes:
            s["requires"] = ""
    for s in case["scenes"]:
        for h in s["hotspots"]:
            if h["requires"] and h["id"] not in ok_spots:
                h["requires"], h["locked_text"] = "", ""


def _ensure_yield_spots(case: dict) -> None:
    """模型漏了给某件物证配热区时，机械地补一个，免得整案报废：
    把它放进它 found_in 的场景（找不到就放起始场景），做成一个直接能开的热区。
    凑出来的物证（combines_into）不需要热区。"""
    derived = {e["combines_into"] for e in case["evidence"] if e.get("combines_into")}
    given = {h["yields"] for s in case["scenes"] for h in s["hotspots"] if h["yields"]}
    start = next(s for s in case["scenes"] if s["id"] == case["start_scene"])
    n = 0
    for e in case["evidence"]:
        if e["id"] in given or e["id"] in derived:
            continue
        n += 1
        start["hotspots"].append({
            "id": f"hs_auto_{e['id']}", "label": f"留意：{e['name']}", "describe": e["detail"],
            "yields": e["id"], "requires": "", "locked_text": "", "once": True,
        })


def _write(case: dict) -> dict:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    path = CASES_DIR / f"{case['case_id']}.json"
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"case_id": case["case_id"], "path": str(path), "title": case["title"],
            "scenes": len(case["scenes"]), "evidence": len(case["evidence"]), "people": len(case["people"])}


def _offline_demo(p: dict, tick) -> dict:
    """没有密钥时：把现成的案件改个名放进去，用来验证流程，不冒充模型产出。"""
    src = json.loads((CASES_DIR / "case_tide.json").read_text(encoding="utf-8"))
    src["case_id"] = "case_ai_" + time.strftime("%y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    src["title"] = "离线示例·" + (p.get("title") or "潮位")[:8]
    src["tagline"] = "（离线示例）没有配置模型密钥，这里放的是《潮位》的副本。"
    src["author"] = "AI 生成（离线示例）"
    tick(0.9, "探案：离线模式，使用现成案件作示例")
    bad = check_case(src, strict=False)
    if bad:
        raise CaseGenError("示例案件自检失败：" + "；".join(bad[:3]))
    return _write(src)
