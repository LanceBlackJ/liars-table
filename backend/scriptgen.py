"""用大模型 API 现场生成剧本杀本子。

不是让模型一口气吐一个 JSON——那样 90% 会在某个角落违反校验。这里分阶段，
每一阶段的输入都是上一阶段已经定下来的事实，最后交给和人工投稿同一套自检：

    1. 设定（bible）  标题、背景、地图、时段、死者、凶手、诡计、角色名册、线索规划
    2. 角色           每人一次：秘密、动机、个人目标、口吻范例；凶手另写假口供；多幕本另写追加剧本
    3. 行踪           先出「谁在哪」矩阵，再逐人写行动（旁人可见）与观察（只给本人）
    4. 线索           按地点分批，实现 bible 里规划的每一个线索 id，另加合成线索
    5. 结局           推理链、关键线索、答卷、幕间事件
    6. 自检与修补     和 scripts/check_script.py 同一套规则；不过就把问题和当前稿件
                     交回模型，只让它返回要改的部分，最多 4 轮

写出去的文件放在 data/scripts/community/，作者署名「AI 生成」。
离线模式（没有密钥）只能产出模板示例，用来验证流程，不会假装是模型写的。
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import llm
from .config import ROOT, SCRIPTS_DIR, SETTINGS

COMMUNITY_DIR = SCRIPTS_DIR / "community"

MAX_REPAIR_ROUNDS = 4

# 阶段权重，用于进度条
_STAGES = [("设定", 0.12), ("角色", 0.30), ("行踪", 0.28), ("线索", 0.18), ("结局", 0.07), ("自检", 0.05)]


class GenError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 造本工具（scripts/_scriptkit.py 不在包里，按路径加载）
# --------------------------------------------------------------------------

_kit = None


def _load_kit():
    global _kit
    if _kit is None:
        p = ROOT / "scripts" / "_scriptkit.py"
        spec = importlib.util.spec_from_file_location("_scriptkit_gen", p)
        mod = importlib.util.module_from_spec(spec)           # type: ignore[arg-type]
        spec.loader.exec_module(mod)                          # type: ignore[union-attr]
        _kit = mod
    return _kit


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

DIFF_CN = {"easy": "简单（线索直白、红鲱鱼少）", "medium": "中等", "hard": "硬核（要对时间、要拼合成线索才能破）"}


def clean_params(raw: dict) -> dict:
    theme = str(raw.get("theme", "")).strip()
    if len(theme) < 4:
        raise GenError("主题太短了，至少写几个字，比如「民国戏院后台的一桩命案」")
    if len(theme) > 400:
        raise GenError("主题请控制在 400 字以内")
    suspects = max(3, min(6, int(raw.get("suspects", 4))))
    npc = max(0, min(2, int(raw.get("npc", 1))))
    acts = int(raw.get("acts", 2))
    acts = 3 if acts >= 3 else 2
    diff = raw.get("difficulty", "medium")
    if diff not in DIFF_CN:
        diff = "medium"
    with_script = bool(raw.get("with_script", True))
    with_case = bool(raw.get("with_case", False))
    if not (with_script or with_case):
        raise GenError("至少要选一种：剧本杀本子 或 探案案子")
    return {
        "theme": theme, "suspects": suspects, "npc": npc, "acts": acts,
        "difficulty": diff, "title": str(raw.get("title", "")).strip()[:20],
        "with_script": with_script, "with_case": with_case,
    }


def search_capacity(p: dict, n_map: int) -> int:
    """每个地点至少要有几条线索，才能保证怎么搜都不会空手。

    一局里每个嫌疑人每幕有 search_budget 次搜证，玩家可以把次数全砸在同一个地点上，
    AI 也会。所以按「总搜证次数平均摊到每个地点，再多留 1 条」算，下限 3、上限 6。"""
    budget = {2: [3, 2], 3: [3, 2, 2]}[p["acts"]]
    total = p["suspects"] * sum(budget)
    return max(3, min(6, -(-total // max(1, n_map)) + 1))


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

RULES = """【硬性规则——违反任何一条，本子都无法上架】
1. 信息隔离：actions（行动）会被同一时段、同一地点的所有人读到，所以只能写“旁人看得见的举动”，
   绝不能写心理、动机、秘密或真相。只有本人才知道的事（偷偷塞了东西、心里怕什么）写进 observed（只给本人看）。
2. 凶手必须在案发时段位于案发地点。其他嫌疑人在案发时段不要和凶手同处一地（尸体发现者另算，在发现时段）。
3. 每个嫌疑人（非证人）至少有一个 type 为 conceal 的目标（隐瞒某件事），否则他没有说谎的理由。
   目标类型只能是 conceal / acquire / protect / expose。
4. 线索：每个地点至少 2 条；至少 2 条红鲱鱼（red=true，指向无辜者的误导，要有实物细节）；
   关键线索不能是红鲱鱼；pts 里只能写角色 id。
5. 合成线索（synth=true）：由 2 条及以上已有线索拼成（from 里写它们的 id），不写 loc，必须写 note（拼成时的一句旁白）。
6. 地点名、角色 id、时段名必须与设定完全一致，不能自创、不能改字。
7. 全部用中文，口语化、有画面感。不要出现“作为AI”“本剧本”之类出戏的话。
8. 只输出一个 JSON 对象，不要任何解释、不要 markdown 围栏。"""


def _sys(extra: str = "") -> dict:
    return {"role": "system", "content": "你是一位资深的剧本杀 / 推理游戏作者，擅长设计严密的不在场证明与时间诡计，"
            "写的本子逻辑自洽、线索公平、玩家能靠推理而不是运气破案。" + extra}


def _ask(prompt: str, *, max_tokens: int = 6000, temperature: float = 0.8,
         model: str | None = None) -> Any:
    """调模型并解析 JSON；解析失败时带着错误让它重来一次。"""
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
            msgs = msgs + [
                {"role": "assistant", "content": text[:3000]},
                {"role": "user", "content": f"上面的输出不是合法 JSON（{str(exc)[:120]}）。请只输出完整、合法的 JSON 对象，不要围栏、不要解释。"},
            ]
    raise GenError(f"模型连续 3 次没有给出合法 JSON：{last[:200]}")


def _bible_prompt(p: dict) -> str:
    n_total = p["suspects"] + p["npc"]
    return f"""请为下面这个主题设计一个剧本杀本子的“设定”。

主题：{p['theme']}
嫌疑人 {p['suspects']} 个（其中恰好 1 个是凶手），证人 {p['npc']} 个（证人不投票，只用来被问话，不能是凶手）。
幕数：{p['acts']}　难度：{DIFF_CN[p['difficulty']]}
{('书名：' + p['title']) if p['title'] else ''}

{RULES}

输出 JSON，字段如下（全部必填）：
{{
  "title": "书名，2–8 字",
  "tagline": "一句话简介，不能剧透凶手和诡计",
  "background": "公开背景，300–500 字。写清时间地点、死者是谁、谁发现的尸体、为什么现场是封闭的；不得点名凶手、不得泄露诡计",
  "map": ["地点1", "...共 6–7 个，名称简短；每个地点都要是玩家会想去搜的、有区别的场所"],
  "slots": ["19:00", "...共 5–6 个，整点或半点，时间递增，最后一个时段略晚于发现尸体的时间"],
  "victim": "死者姓名",
  "death_slot": "案发时段（必须是 slots 之一）",
  "death_location": "案发地点（必须是 map 之一）",
  "weapon": "凶器", "cause": "死因，一句话",
  "found_by": "发现尸体者的角色 id（必须是下面 characters 之一）",
  "found_slot": "发现尸体的时段（必须是 slots 之一，不早于 death_slot）",
  "murderer": "凶手的角色 id",
  "truth": "真相，200–400 字：凶手为什么杀人、怎么杀、怎么布置不在场、哪一环最容易露馅",
  "trick": "核心诡计，一两句话；难度较高时要包含一个需要对时间或数字的细节",
  "characters": [
    {{"id": "c1", "name": "姓名", "role": "公开身份，一句话", "is_npc": false,
      "gist": "此人今夜真正做了什么、藏着什么，60–120 字"}}
    // 共 {n_total} 个；id 用 c1、c2…；证人 is_npc=true
  ],
  "clue_plan": [
    {{"id": "ASCII短id如 k_knife", "loc": "地点（必须在 map 里）",
      "kind": "key / red / plain", "pts": ["角色id"], "gist": "这条线索的内容与作用，一句话"}}
    // 每个地点 {search_capacity(p, 6)} 条左右（每个地点至少 {search_capacity(p, 7)} 条），所有地点都要有；
    // 至少 3 条 key（互相印证能指向凶手）、至少 2 条 red（误导）；
    // 其中 2–3 条要能暴露凶手假口供的破绽，kind 写 key
  ]
}}"""


def _char_prompt(p: dict, bible: dict, ch: dict) -> str:
    is_m = ch["id"] == bible["murderer"]
    others = "\n".join(f"- {c['id']} {c['name']}（{c['role']}）" for c in bible["characters"] if c["id"] != ch["id"])
    weak_pool = [c for c in bible["clue_plan"] if c.get("kind") == "key"]
    weak_txt = "\n".join(f"  {c['id']}：{c['gist']}" for c in weak_pool)
    cover = ""
    if is_m:
        cover = f"""
  "cover": {{
    "claim": "一句话总口供：我今晚一直在……",
    "fake_timeline": [ {{"slot": "时段", "claimed_location": "他声称所在的地点", "claimed_action": "他声称做的事"}} ],
    // slots 里每一个时段都要有一条：{bible['slots']}
    "weak_points": [ {{"clue_id": "必须取自下面这些线索 id", "risk": "这条线索为什么会戳穿他", "fallback": "被问到时他怎么圆"}} ],
    "redirect_targets": ["他想嫁祸的角色 id"],
    "redirect_hook": "他用来把嫌疑引向别人的一句话"
  }},"""
    acts_part = ""
    if p["acts"] >= 2:
        acts_part = f"""
  "acts": [
    // 每一幕（第 2–{p['acts']} 幕）一个对象：这一幕他新想起/新发现/新处境
    {{"act": 2, "title": "小标题", "text": "第二幕追加给他本人的剧本，120–250 字。不得泄露他不该知道的真相",
     "obs": ["本幕新获得的一条只有他知道的观察"], "goals": [["conceal 或 acquire 等", "本幕新目标"]], "secret": "本幕新增的一条秘密，一句话"}}
  ],"""
    return f"""现在写角色“{ch['name']}”（id={ch['id']}）的个人剧本。

【全局设定】
书名：{bible['title']}
背景：{bible['background']}
地图：{bible['map']}　时段：{bible['slots']}
死者：{bible['victim']}（{bible['death_slot']} 死于{bible['death_location']}，{bible['cause']}）
真相（只有你知道，不要原样抄进别人能看到的字段）：{bible['truth']}
此人：{ch['name']}，{ch['role']}。{'他是凶手。' if is_m else ''}他今晚真正做的事：{ch['gist']}
其他人：
{others}

{RULES}

输出 JSON：
{{
  "secret": "第二人称“你……”，150–300 字，写明他今晚真实做了什么、藏着什么、对谁有什么情绪。{'要写清他如何杀人、如何布置不在场。' if is_m else '不要写成凶手。他的秘密和凶案无关或只是间接相关。'}",
  "motive": "他的动机/嫌疑点，一句话（会被别人看到，不能暴露凶手身份）",
  "persona": "性格与小动作，一句话",
  "style": "说话风格，一句话",
  "lines": ["口吻范例 3 句，要像这个人说的话"],
  "goals": [["conceal", "不要让人知道……"], ["acquire", "……"]],{cover}{acts_part}
  "hair": "short|buzz|bun|long|updo|balding|bald 之一", "hc": "black|darkbrown|gray|white|auburn 之一",
  "skin": "light|mid|tan|deep 之一", "garment": "indigo|crimson|olive|plum|teal|ochre|slate|rust|moss|wine 之一",
  "acc": "none|glasses|round_glasses|mustache|beard|scarf|goggles|earrings 之一"
}}
{('可用于 weak_points 的线索（id：内容）：' + chr(10) + weak_txt) if is_m else ''}"""


def _npc_prompt(p: dict, bible: dict, ch: dict) -> str:
    return f"""现在写证人“{ch['name']}”（id={ch['id']}）。证人不是嫌疑人，不投票，只被问话；他看到或听到了一些有用的东西，但不知道全貌。

书名：{bible['title']}　背景：{bible['background']}
地图：{bible['map']}　时段：{bible['slots']}
死者：{bible['victim']}（{bible['death_slot']} 死于{bible['death_location']}）
真相（只有你知道，不要抄进证人的字段）：{bible['truth']}
此人：{ch['name']}，{ch['role']}。他的经历：{ch['gist']}

{RULES}

输出 JSON：
{{
  "secret": "第二人称“你……”，100–200 字：他今晚看到/听到了什么（具体到时段和地点），以及他不知道什么。",
  "motive": "", "persona": "性格一句话", "style": "说话风格一句话",
  "lines": ["口吻范例 3 句"],
  "hair": "short", "hc": "gray", "skin": "mid", "garment": "slate", "acc": "none"
}}"""


def _where_prompt(p: dict, bible: dict) -> str:
    chars = "\n".join(f"- {c['id']} {c['name']}：{c['gist']}" for c in bible["characters"])
    return f"""请给每个角色在每个时段安排一个所在地点，形成“行踪矩阵”。

地图：{bible['map']}
时段（按顺序）：{bible['slots']}
死者 {bible['victim']}：{bible['death_slot']} 死于 {bible['death_location']}；发现者 {bible['found_by']} 在 {bible['found_slot']} 发现。
凶手 {bible['murderer']} 必须在 {bible['death_slot']} 位于 {bible['death_location']}。
真相：{bible['truth']}
角色：
{chars}

要求：
- 每个角色恰好 {len(bible['slots'])} 个地点，与 slots 一一对应，地点必须取自地图；
- 同一时段同一地点的人会互相看见，所以要有意识地设计谁和谁碰面、谁没有人证；
- 凶手在案发之前/之后要有一段“空档”，这是破案的关键；
- 死者也是一个角色吗？不是——死者不在名册里，不用安排。

只输出 JSON：{{"where": {{"角色id": ["地点", "..."], ...}}}}"""


def _action_prompt(p: dict, bible: dict, where: dict, ch: dict) -> str:
    slots = bible["slots"]
    rows = []
    for s_i, s in enumerate(slots):
        mates = [c["name"] for c in bible["characters"]
                 if c["id"] != ch["id"] and where[c["id"]][s_i] == where[ch["id"]][s_i]]
        rows.append(f"  {s} 在「{where[ch['id']][s_i]}」" + (f"，同处的人：{'、'.join(mates)}" if mates else "，独自一人"))
    return f"""请为角色“{ch['name']}”（id={ch['id']}，{ch['role']}）写每个时段的行动。

背景：{bible['background']}
真相：{bible['truth']}
他今晚的真实经历：{ch['gist']}
{'他是凶手，案发时段在案发地点。' if ch['id'] == bible['murderer'] else ''}
他的行踪（已定，不要改）：
{chr(10).join(rows)}

{RULES}

输出 JSON：
{{
  "actions": {{"时段": "旁人看得见的行动，一两句。同处的人都会读到这句话，所以不要写心理、秘密或真相"}},
  "observed": {{"时段": ["只有他自己知道的观察：他看见/听见/摸到的细节，或他偷偷做的事（0–2 条，可为空数组）"]}}
}}
actions 必须覆盖全部时段：{slots}"""


def _clue_prompt(p: dict, bible: dict, plan: list[dict], is_first: bool) -> str:
    items = "\n".join(f"- {c['id']}｜{c['loc']}｜{c['kind']}｜指向 {c.get('pts') or []}｜{c['gist']}" for c in plan)
    chars = "、".join(f"{c['id']}={c['name']}" for c in bible["characters"])
    return f"""请把下面规划好的线索写成具体的线索文本。

书名：{bible['title']}　地点：{bible['map']}
真相：{bible['truth']}
诡计：{bible['trick']}
角色：{chars}　凶手：{bible['murderer']}

要写的线索（id｜地点｜类型｜指向｜要点）：
{items}

{RULES}

写法：
- text：玩家搜到时读到的原文，60–140 字，有具体的物证细节（颜色、数字、位置、气味），不要直接说“凶手是某某”；
- reveals：玩家得到的结论，一句话；
- kind=red 的线索 red=true，指向一个无辜的人，但要有后来能被排除的细节；
- kind=key 的线索 red=false；
- d：难度 1–5，越大越不容易被搜到。
输出 JSON：
{{"clues": [{{"id": "与规划一致", "loc": "与规划一致", "text": "...", "reveals": "...", "pts": ["角色id"], "red": false, "d": 1}}]}}"""


def _topup_prompt(p: dict, bible: dict, loc: str, k: int, have: list[dict]) -> str:
    seen = "\n".join(f"- {c.get('id')}：{c.get('reveals') or c.get('text', '')[:40]}" for c in have) or "（还没有）"
    chars = "、".join(f"{c['id']}={c['name']}" for c in bible["characters"])
    return f"""地点「{loc}」的线索不够，请再补 {k} 条。

书名：{bible['title']}　真相：{bible['truth']}
角色：{chars}　凶手：{bible['murderer']}
这个地点已有的线索：
{seen}

{RULES}

新线索要和已有的不重复，可以是：环境细节、某人来过的痕迹、与案情无关的小秘密（让每个人的 conceal 目标有来源）、或误导性的红鲱鱼（red=true）。
至少 1 条是纯环境/氛围细节，让玩家觉得这个地方“真实”。text 60–120 字，有具体物证细节。
输出 JSON：{{"clues": [{{"id": "x_xxx", "loc": "{loc}", "text": "...", "reveals": "...", "pts": [], "red": false, "d": 2}}]}}"""


def _synth_prompt(p: dict, bible: dict, clues: list[dict]) -> str:
    keys = "\n".join(f"- {c['id']}｜{c['loc']}｜{c['reveals']}" for c in clues if not c.get("red"))
    return f"""下面是已经写好的线索。请再设计 {2 if p['difficulty'] == 'easy' else 3} 条“合成线索”：
玩家把其中两条以上的线索（from）凑在一起，才能得出一个新结论。合成线索要比单条线索更接近真相，最后一条应当把矛头指向凶手 {bible['murderer']}。

真相：{bible['truth']}
可用原料（id｜地点｜结论）：
{keys}

{RULES}

输出 JSON：
{{"synth": [{{"id": "z_xxx", "from": ["原料id", "原料id"], "text": "拼成后得到的新信息，60–120 字", "reveals": "新结论一句话",
  "pts": ["角色id"], "note": "拼成当场的一句旁白，20–40 字"}}]}}
注意：from 里只能用上面列出的 id，且至少 2 个不同的 id。"""


def _end_prompt(p: dict, bible: dict, clue_ids: list[str], synth_ids: list[str], locs: list[str]) -> str:
    inter = ""
    if p["acts"] >= 2:
        inter = f"""
  "interludes": [
    // 每幕开场一个：第 2 幕{'、第 3 幕' if p['acts'] >= 3 else ''}各一个。作用是推翻玩家刚建立的结论或抛出新变数
    {{"at": "act2", "title": "小标题", "text": "100–200 字旁白", "effect": "none"}}
  ],"""
    return f"""请写这个本子的结局部分。

书名：{bible['title']}　凶手：{bible['murderer']}（{next(c['name'] for c in bible['characters'] if c['id'] == bible['murderer'])}）
真相：{bible['truth']}
诡计：{bible['trick']}
角色：{[(c['id'], c['name'], c.get('is_npc', False)) for c in bible['characters']]}
地点：{locs}
可选作关键线索的 id（非红鲱鱼）：{clue_ids}
合成线索 id：{synth_ids}

{RULES}

输出 JSON：
{{
  "chain": ["推理链，5–8 步，每步一句话，写明用哪些线索（线索 id）得出什么"],
  "key_clues": ["3–6 个关键线索 id，必须取自上面的“可选作关键线索”"],
  "questions": [
    // 答卷 4–6 题。第一题必须是 kind=character 的“谁是凶手”，answer 是凶手 id。
    // 其余是 kind=choice：问诡计怎么做的、动机是什么、某人那段时间在干什么……
    // 每道 choice 给 3–4 个选项，错误选项必须看起来合理（别写一眼假的），正确答案不要总是最长的。
    {{"id": "q_who", "prompt": "谁杀了{bible['victim']}？", "kind": "character", "answer": "{bible['murderer']}", "points": 3, "explain": "一句话讲理由"}},
    {{"id": "q_how", "prompt": "……？", "kind": "choice", "answer": "a",
      "options": [{{"id": "a", "text": "..."}}, {{"id": "b", "text": "..."}}, {{"id": "c", "text": "..."}}],
      "points": 2, "explain": "..."}}
  ],{inter}
  "tags": ["2–4 个标签，如 民国 / 封闭空间 / 时间诡计"]
}}"""


def _fix_prompt(spec: dict, problems: list[str]) -> str:
    slim = json.dumps(spec, ensure_ascii=False)
    return f"""下面是一个剧本杀本子的当前稿件（JSON），自检发现了这些问题：

{chr(10).join('- ' + x for x in problems[:25])}

{RULES}

请只返回需要修改的部分（没问题的不要重复），格式：
{{
  "meta": {{"可选，覆盖顶层标量字段如 death_slot / death_location / found_slot / found_by"}},
  "chars": {{"角色id": {{"要覆盖的字段": "新值"}}}},
  "where": {{"角色id": ["完整的新地点列表"]}},
  "actions": {{"角色id": {{"时段": "新的行动文本"}}}},
  "observed": {{"角色id": {{"时段": ["新的观察列表"]}}}},
  "clues": [{{"完整的线索对象（按 id 覆盖或新增）"}}],
  "delete_clues": ["要删除的线索 id"],
  "key_clues": ["如需整体替换关键线索列表"],
  "map": ["如需整体替换地图"]
}}
常见修法：线索地点不在地图里就改线索的 loc 或补上地点；某地点没线索就加一条线索；
泄漏（私人文本被别人读到）就改写那段文本，让它只含旁人看得见的内容；
凶手不在案发地点就改 where；缺 conceal 目标就给该角色补 goals。

当前稿件：
{slim}"""


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def _tuple_keys(tbl: dict) -> dict:
    return {(cid, slot): v for cid, per in tbl.items() for slot, v in per.items()}


def to_kit_spec(spec: dict) -> dict:
    """JSON 友好的稿件 → _scriptkit 的 spec（行动/观察用元组键，目标用元组）。"""
    k = copy.deepcopy(spec)
    k["actions"] = _tuple_keys(spec["actions"])
    k["observed"] = _tuple_keys(spec.get("observed", {}))
    for c in k["chars"]:
        c["goals"] = [tuple(g) for g in c.get("goals", [])]
        for a in c.get("acts", []):
            a["goals"] = [tuple(g) for g in a.get("goals", [])]
    k.setdefault("no_witness_slots", [])
    return k


def _fill_blanks(spec: dict) -> None:
    """模型漏写的格子给一个安全的占位，免得整本因为一格空白报废。"""
    slots = spec["slots"]
    for c in spec["chars"]:
        cid = c["id"]
        spec["actions"].setdefault(cid, {})
        spec["observed"].setdefault(cid, {})
        for i, s in enumerate(slots):
            if not str(spec["actions"][cid].get(s, "")).strip():
                spec["actions"][cid][s] = "在原地，神色如常。"
            obs = spec["observed"][cid].get(s, [])
            spec["observed"][cid][s] = [str(o) for o in obs] if isinstance(obs, list) else [str(obs)]
        c.setdefault("motive", "")
        c.setdefault("lines", [])
        if not c.get("npc") and not any(g[0] == "conceal" for g in c.get("goals", [])):
            if cid != spec["murderer"]:
                c.setdefault("goals", []).append(["conceal", "不要让人知道你今晚隐瞒的那件事"])
        if not c.get("npc") and not c.get("goals"):
            c["goals"] = [["conceal", "不要被投成凶手"]]


def check_spec(spec: dict) -> list[str]:
    kit = _load_kit()
    try:
        d = kit.build(to_kit_spec(spec))
    except Exception as exc:                                      # noqa: BLE001
        return [f"稿件结构不完整，无法组装：{type(exc).__name__}: {exc}"]
    bad = kit.self_check(d)
    # 场景够用：每个地点的线索数不得低于容量下限（只算搜得到的，不算合成/随身）
    n_players = sum(1 for c in spec["chars"] if not c.get("npc"))
    cap = search_capacity({"suspects": n_players, "acts": spec["acts"]}, len(spec["map"]))
    for loc in spec["map"]:
        n = sum(1 for c in spec["clues"] if c.get("loc") == loc and not c.get("synth") and not c.get("granted"))
        if n < cap:
            bad.append(f"地点「{loc}」只有 {n} 条可搜线索，至少要 {cap} 条（搜证次数可以全砸在这里，不能让人翻空）")
    if len(spec["map"]) < 6:
        bad.append(f"地点只有 {len(spec['map'])} 个，至少要 6 个")
    return bad


def apply_fix(spec: dict, fix: dict) -> None:
    for k, v in (fix.get("meta") or {}).items():
        if k in ("death_slot", "death_location", "found_slot", "found_by", "weapon", "cause", "victim",
                 "tagline", "background", "title") and isinstance(v, str):
            spec[k] = v
    if isinstance(fix.get("map"), list) and fix["map"]:
        spec["map"] = [str(x) for x in fix["map"]]
    by = {c["id"]: c for c in spec["chars"]}
    for cid, patch in (fix.get("chars") or {}).items():
        if cid in by and isinstance(patch, dict):
            for k, v in patch.items():
                if k not in ("id",):
                    by[cid][k] = v
    for cid, locs in (fix.get("where") or {}).items():
        if cid in by and isinstance(locs, list) and len(locs) == len(spec["slots"]):
            spec["where"][cid] = locs
    for cid, per in (fix.get("actions") or {}).items():
        if cid in by and isinstance(per, dict):
            spec["actions"].setdefault(cid, {}).update({s: str(t) for s, t in per.items() if s in spec["slots"]})
    for cid, per in (fix.get("observed") or {}).items():
        if cid in by and isinstance(per, dict):
            spec["observed"].setdefault(cid, {}).update(
                {s: [str(x) for x in (t if isinstance(t, list) else [t])] for s, t in per.items() if s in spec["slots"]})
    cl = {c["id"]: c for c in spec["clues"]}
    for c in fix.get("clues") or []:
        if isinstance(c, dict) and c.get("id"):
            cl[c["id"]] = c
    for cid in fix.get("delete_clues") or []:
        cl.pop(cid, None)
    spec["clues"] = list(cl.values())
    if isinstance(fix.get("key_clues"), list) and fix["key_clues"]:
        spec["key_clues"] = fix["key_clues"]
    # 删线索后，别处引用它的地方一并清掉
    ids = set(cl)
    spec["key_clues"] = [x for x in spec["key_clues"] if x in ids]
    for c in spec["clues"]:
        if c.get("synth"):
            c["from"] = [x for x in c.get("from", []) if x in ids]
    spec["clues"] = [c for c in spec["clues"] if not (c.get("synth") and len(set(c.get("from", []))) < 2)]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def generate(params: dict, progress: Callable[[float, str, str], None] | None = None) -> dict:
    """返回 {"script_id", "path", "problems", "han"}；失败抛 GenError。"""
    p = clean_params(params)

    def tick(done_stage: int, frac: float, msg: str) -> None:
        if progress:
            base = sum(w for _, w in _STAGES[:done_stage])
            progress(min(0.99, base + _STAGES[done_stage][1] * frac), _STAGES[done_stage][0], msg)

    if SETTINGS.mock:
        return _offline_demo(p, progress)

    # ---- 1 设定 ----
    tick(0, 0, "正在构思设定与线索规划…")
    bible = None
    for attempt in range(3):
        bible = _ask(_bible_prompt(p) + ("" if attempt == 0 else f"\n\n上一次的问题：{err}。请修正后重新输出完整 JSON。"),
                     max_tokens=7000, model=SETTINGS.model_reason)
        try:
            _check_bible(p, bible)
            break
        except GenError as exc:
            err = str(exc)
            if attempt == 2:
                raise

    tick(0, 1, f"《{bible['title']}》：{len(bible['characters'])} 个角色、{len(bible['map'])} 个地点")

    # ---- 2 角色 ----
    chars_out: dict[str, dict] = {}

    def one_char(ch: dict) -> tuple[str, dict]:
        prompt = _npc_prompt(p, bible, ch) if ch.get("is_npc") else _char_prompt(p, bible, ch)
        return ch["id"], _ask(prompt, max_tokens=4500)

    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for cid, data in ex.map(one_char, bible["characters"]):
            chars_out[cid] = data
            done += 1
            tick(1, done / len(bible["characters"]), f"已写好 {done}/{len(bible['characters'])} 个角色")

    # ---- 3 行踪 ----
    tick(2, 0, "正在编排每个人的行踪…")
    where = _ask(_where_prompt(p, bible), max_tokens=3000).get("where", {})
    ids = [c["id"] for c in bible["characters"]]
    for cid in ids:
        locs = where.get(cid)
        if not isinstance(locs, list) or len(locs) != len(bible["slots"]):
            where[cid] = [bible["death_location"] if cid == bible["murderer"] and s == bible["death_slot"] else bible["map"][0]
                          for s in bible["slots"]]
    for i, s in enumerate(bible["slots"]):
        if s == bible["death_slot"]:
            where[bible["murderer"]][i] = bible["death_location"]
    acts_out: dict[str, dict] = {}

    def one_act(ch: dict) -> tuple[str, dict]:
        return ch["id"], _ask(_action_prompt(p, bible, where, ch), max_tokens=4000)

    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for cid, data in ex.map(one_act, bible["characters"]):
            acts_out[cid] = data
            done += 1
            tick(2, done / len(ids), f"已写好 {done}/{len(ids)} 个人的行动")

    # ---- 4 线索 ----
    plan = [c for c in bible["clue_plan"] if c.get("loc") in bible["map"]]
    batches: list[list[dict]] = []
    for loc in bible["map"]:
        grp = [c for c in plan if c["loc"] == loc]
        if grp:
            batches.append(grp)
    merged: list[list[dict]] = []
    for g in batches:                      # 每批 2 个地点
        if merged and len(merged[-1]) + len(g) <= 6:
            merged[-1] += g
        else:
            merged.append(list(g))

    def one_batch(grp: list[dict]) -> list[dict]:
        return _ask(_clue_prompt(p, bible, grp, False), max_tokens=5000).get("clues", [])

    clues: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for i, out in enumerate(ex.map(one_batch, merged), 1):
            clues += [c for c in out if isinstance(c, dict) and c.get("id")]
            tick(3, i / (len(merged) + 1), f"已写好 {len(clues)} 条线索")
    for c in clues:
        c.setdefault("pts", [])
        c.setdefault("red", False)
        c.setdefault("d", 1)
        c["pts"] = [x for x in c["pts"] if x in ids]
    # 每个地点补足到容量：搜证次数可以全砸在一个地点上，不能让人在某处翻空
    need = search_capacity(p, len(bible["map"]))
    short = [(loc, need - sum(1 for c in clues if c.get("loc") == loc)) for loc in bible["map"]]
    short = [(loc, k) for loc, k in short if k > 0]

    def topup(item: tuple[str, int]) -> list[dict]:
        loc, k = item
        have = [c for c in clues if c.get("loc") == loc]
        out = _ask(_topup_prompt(p, bible, loc, k, have), max_tokens=3500).get("clues", [])
        fixed = []
        for j, c in enumerate(out[:k + 1]):
            if isinstance(c, dict) and c.get("text"):
                c["id"] = re.sub(r"\W", "_", str(c.get("id") or f"x_{bible['map'].index(loc)}_{j}"))
                c["loc"] = loc
                fixed.append(c)
        return fixed

    if short:
        tick(3, 0.8, f"补足线索：{len(short)} 个地点不够 {need} 条")
        with ThreadPoolExecutor(max_workers=3) as ex:
            for out in ex.map(topup, short):
                existing = {c["id"] for c in clues}
                for c in out:
                    while c["id"] in existing:
                        c["id"] += "_"
                    existing.add(c["id"])
                    c.setdefault("pts", [])
                    c.setdefault("red", False)
                    c.setdefault("d", 2)
                    c["pts"] = [x for x in c["pts"] if x in ids]
                    clues.append(c)
    synth = _ask(_synth_prompt(p, bible, clues), max_tokens=3000).get("synth", [])
    for z in synth:
        if isinstance(z, dict) and z.get("id"):
            z["synth"] = True
            z.pop("loc", None)
            z["pts"] = [x for x in z.get("pts", []) if x in ids]
            clues.append(z)
    tick(3, 1, f"线索 {len(clues)} 条（含合成线索 {len(synth)} 条）")

    # ---- 5 结局 ----
    tick(4, 0, "正在写推理链与答卷…")
    non_red = [c["id"] for c in clues if not c.get("red") and not c.get("synth")]
    end = _ask(_end_prompt(p, bible, non_red, [z["id"] for z in synth if isinstance(z, dict) and z.get("id")], bible["map"]),
               max_tokens=5000)

    # ---- 组装 ----
    spec = _assemble(p, bible, chars_out, where, acts_out, clues, end)
    _fill_blanks(spec)

    # ---- 6 自检与修补 ----
    problems = check_spec(spec)
    rnd = 0
    while problems and rnd < MAX_REPAIR_ROUNDS:
        rnd += 1
        tick(5, rnd / (MAX_REPAIR_ROUNDS + 1), f"自检发现 {len(problems)} 处问题，第 {rnd} 轮修补…")
        try:
            fix = _ask(_fix_prompt(spec, problems), max_tokens=7000, temperature=0.4)
            apply_fix(spec, fix if isinstance(fix, dict) else {})
            _fill_blanks(spec)
        except GenError:
            pass
        problems = check_spec(spec)

    if problems:
        raise GenError("自检仍有问题，没有写入：\n" + "\n".join("- " + x for x in problems[:12]))

    tick(5, 1, "写入文件…")
    out = _write(spec)
    out["_bible"] = bible
    return out


def generate_all(params: dict, progress: Callable[[float, str, str], None] | None = None) -> dict:
    """按勾选生成：剧本杀本子 和/或 探案案子。两个都选时，探案沿用剧本杀的设定——同一个故事两种玩法。

    探案失败不会让已经写好的剧本杀本子报废：剧本杀照常写入，错误放在 case_error 里。"""
    p = clean_params(params)
    both = p["with_script"] and p["with_case"]
    w_script = 0.62 if both else 1.0
    res: dict = {"script": None, "case": None, "case_error": ""}
    bible = None
    if p["with_script"]:
        def sp(f: float, stage: str, msg: str) -> None:
            if progress:
                progress(f * w_script, stage, msg)

        r = generate(p, sp)
        bible = r.pop("_bible", None)
        res["script"] = r
    if p["with_case"]:
        from . import casegen
        base = w_script if p["with_script"] else 0.0

        def cp(f: float, msg: str) -> None:
            if progress:
                progress(base + f * (1 - base), "探案", msg)

        try:
            res["case"] = casegen.generate(p, bible, cp)
        except casegen.CaseGenError as exc:
            if res["script"] is None:
                raise GenError(str(exc)) from exc
            res["case_error"] = str(exc)
    res["title"] = (res["script"] or res["case"])["title"]
    return res


def _check_bible(p: dict, b: dict) -> None:
    need = ("title", "tagline", "background", "map", "slots", "victim", "death_slot", "death_location",
            "weapon", "cause", "found_by", "found_slot", "murderer", "truth", "trick", "characters", "clue_plan")
    miss = [k for k in need if not b.get(k)]
    if miss:
        raise GenError(f"设定缺字段：{miss}")
    ids = [c.get("id") for c in b["characters"]]
    if len(set(ids)) != len(ids) or len(ids) < 3:
        raise GenError("角色 id 重复或太少")
    if b["murderer"] not in ids:
        raise GenError("凶手不在角色名册里")
    for c in b["characters"]:
        if c["id"] == b["murderer"]:
            c["is_npc"] = False
        c.setdefault("is_npc", False)
        c.setdefault("gist", "")
    if len(b["map"]) < 6:
        raise GenError(f"地图只有 {len(b['map'])} 个地点，至少要 6 个，场景才够用")
    if b["death_slot"] not in b["slots"] or b["found_slot"] not in b["slots"]:
        raise GenError("案发/发现时段不在时段表里")
    if b["death_location"] not in b["map"]:
        raise GenError("案发地点不在地图里")
    if b["found_by"] not in ids:
        b["found_by"] = next(c["id"] for c in b["characters"] if c["id"] != b["murderer"])
    if p["title"]:
        b["title"] = p["title"]
    plan = []
    for i, c in enumerate(b["clue_plan"]):
        if not isinstance(c, dict):
            continue
        c["id"] = re.sub(r"\W", "_", str(c.get("id") or f"k{i + 1}"))
        c["gist"] = str(c.get("gist") or c.get("desc") or c.get("text") or c.get("content") or "")
        c["pts"] = [x for x in (c.get("pts") or []) if x in ids]
        c["kind"] = c.get("kind") if c.get("kind") in ("key", "red", "plain") else "plain"
        plan.append(c)
    seen_ids: set[str] = set()
    for c in plan:                                   # id 去重
        while c["id"] in seen_ids:
            c["id"] += "_"
        seen_ids.add(c["id"])
    b["clue_plan"] = plan


def _assemble(p: dict, b: dict, chars_out: dict, where: dict, acts_out: dict,
              clues: list[dict], end: dict) -> dict:
    chars = []
    for ch in b["characters"]:
        d = chars_out[ch["id"]]
        e = {
            "id": ch["id"], "name": ch["name"], "role": ch["role"],
            "secret": str(d.get("secret", "")), "motive": str(d.get("motive", "")),
            "persona": str(d.get("persona", "沉默寡言")), "style": str(d.get("style", "话不多")),
            "lines": [str(x) for x in d.get("lines", [])][:5],
            "goals": [list(g)[:2] for g in d.get("goals", []) if isinstance(g, (list, tuple)) and len(g) >= 2],
            "hair": d.get("hair", "short"), "hc": d.get("hc", "black"), "skin": d.get("skin", "mid"),
            "garment": d.get("garment", "slate"), "acc": d.get("acc", "none"),
        }
        if ch.get("is_npc"):
            e["npc"] = True
            e["goals"] = []
        if ch["id"] == b["murderer"]:
            e["cover"] = d.get("cover") or {
                "claim": "我一直待在原地，什么都没做。",
                "fake_timeline": [{"slot": s, "claimed_location": where[ch["id"]][i], "claimed_action": "待在这里。"}
                                  for i, s in enumerate(b["slots"])],
                "weak_points": [], "redirect_targets": [], "redirect_hook": "",
            }
        acts = []
        for a in d.get("acts", []) if isinstance(d.get("acts"), list) else []:
            if isinstance(a, dict) and 2 <= int(a.get("act", 0) or 0) <= p["acts"]:
                acts.append({
                    "act": int(a["act"]), "title": str(a.get("title", "")), "text": str(a.get("text", "")),
                    "obs": [str(x) for x in a.get("obs", [])],
                    "goals": [list(g)[:2] for g in a.get("goals", []) if isinstance(g, (list, tuple)) and len(g) >= 2],
                    "secret": str(a.get("secret", "")),
                })
        seen = set()
        e["acts"] = [a for a in acts if not (a["act"] in seen or seen.add(a["act"]))]
        chars.append(e)

    actions = {c["id"]: dict(acts_out[c["id"]].get("actions", {})) for c in b["characters"]}
    observed = {c["id"]: dict(acts_out[c["id"]].get("observed", {})) for c in b["characters"]}
    minutes = 30 + 10 * len(chars) + 15 * (p["acts"] - 1) + (15 if p["difficulty"] == "hard" else 0)
    spec = {
        "id": "ai_" + time.strftime("%y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4],
        "title": b["title"], "tagline": b["tagline"], "author": "AI 生成",
        "difficulty": p["difficulty"], "minutes": minutes, "acts": p["acts"],
        "search_budget": [3, 2, 2][: p["acts"]] if p["acts"] > 1 else [3],
        "tags": ["AI 生成"] + [str(t) for t in end.get("tags", [])][:4],
        "background": b["background"], "map": b["map"], "slots": b["slots"],
        "victim": b["victim"], "death_slot": b["death_slot"], "death_location": b["death_location"],
        "weapon": b["weapon"], "cause": b["cause"], "found_by": b["found_by"], "found_slot": b["found_slot"],
        "murderer": b["murderer"], "chars": chars, "where": where, "actions": actions, "observed": observed,
        "clues": clues, "interludes": [i for i in end.get("interludes", []) if isinstance(i, dict)],
        "chain": [str(x) for x in end.get("chain", [])], "key_clues": [str(x) for x in end.get("key_clues", [])],
        "questions": [q for q in end.get("questions", []) if isinstance(q, dict)],
        "question_pass": 0.6,
    }
    if spec["acts"] == 3 and len(spec["search_budget"]) != 3:
        spec["search_budget"] = [3, 2, 2]
    # 幕间补全：缺 at 时按顺序补
    for i, it in enumerate(spec["interludes"]):
        it.setdefault("at", "act2" if i == 0 else "act3")
        it.setdefault("effect", "none")
        if it["effect"] == "reveal_clue" and not it.get("clue_id"):
            it["effect"] = "none"
        if it["effect"] == "unlock_location" and not it.get("location"):
            it["effect"] = "none"
    spec["interludes"] = [i for i in spec["interludes"] if not (i["at"] == "act3" and spec["acts"] < 3)]
    return spec


def _write(spec: dict) -> dict:
    kit = _load_kit()
    d = kit.build(to_kit_spec(spec))
    COMMUNITY_DIR.mkdir(parents=True, exist_ok=True)
    path = COMMUNITY_DIR / f"{spec['id']}.json"
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    han = len(re.findall(r"[一-鿿]", json.dumps(d, ensure_ascii=False)))
    return {"script_id": spec["id"], "path": str(path), "problems": [], "han": han, "title": spec["title"]}


# --------------------------------------------------------------------------
# 离线示例：没有密钥时，把模板改个名，用来验证整条流程（不冒充模型产出）
# --------------------------------------------------------------------------


def _offline_demo(p: dict, progress) -> dict:
    spec_mod_path = ROOT / "scripts" / "make_template.py"
    sp = importlib.util.spec_from_file_location("_mk_tpl", spec_mod_path)
    mod = importlib.util.module_from_spec(sp)                      # type: ignore[arg-type]
    sp.loader.exec_module(mod)                                     # type: ignore[union-attr]
    spec = copy.deepcopy(mod.SPEC)
    spec["id"] = "ai_" + time.strftime("%y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    spec["title"] = (p["title"] or "离线示例")[:12]
    spec["tagline"] = f"（离线示例）主题「{p['theme'][:30]}」——没有配置模型密钥，这里放的是模板本子。"
    spec["author"] = "AI 生成（离线示例）"
    spec["tags"] = ["AI 生成", "离线示例"]
    if progress:
        progress(0.5, "设定", "离线模式：没有可用的模型，使用模板示例")
    kit = _load_kit()
    d = kit.build(spec)
    bad = kit.self_check(d)
    if bad:
        raise GenError("模板示例自检失败：" + "；".join(bad[:3]))
    COMMUNITY_DIR.mkdir(parents=True, exist_ok=True)
    path = COMMUNITY_DIR / f"{spec['id']}.json"
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"script_id": spec["id"], "path": str(path), "problems": [], "title": spec["title"],
            "han": len(re.findall(r"[一-鿿]", json.dumps(d, ensure_ascii=False))), "offline": True}


# --------------------------------------------------------------------------
# 后台任务
# --------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
MAX_RUNNING = 2


def start_job(params: dict) -> str:
    p = clean_params(params)                       # 参数不对立刻报错，不进后台
    with _LOCK:
        if sum(1 for j in _JOBS.values() if j["status"] == "running") >= MAX_RUNNING:
            raise GenError("正在生成的本子太多了，等一个完成再来")
        jid = uuid.uuid4().hex[:10]
        _JOBS[jid] = {"status": "running", "progress": 0.0, "stage": "排队", "log": [], "result": None,
                      "error": "", "started": time.time(), "params": p}
        for old in sorted(_JOBS, key=lambda k: _JOBS[k]["started"])[:-30]:      # 只留最近 30 个
            _JOBS.pop(old, None)

    def log(frac: float, stage: str, msg: str) -> None:
        with _LOCK:
            j = _JOBS[jid]
            j["progress"], j["stage"] = round(frac, 3), stage
            if not j["log"] or j["log"][-1] != msg:
                j["log"].append(msg)

    def run() -> None:
        try:
            res = generate_all(p, log)
            with _LOCK:
                _JOBS[jid].update(status="done", progress=1.0, stage="完成", result=res)
                if res["script"]:
                    _JOBS[jid]["log"].append(f"剧本杀《{res['script']['title']}》约 {res['script']['han']:,} 汉字")
                if res["case"]:
                    c = res["case"]
                    _JOBS[jid]["log"].append(f"探案《{c['title']}》{c['scenes']} 个场景、{c['evidence']} 件物证、{c['people']} 个人物")
                if res["case_error"]:
                    _JOBS[jid]["log"].append("探案案子没能通过自检，未写入：" + res["case_error"][:200])
            try:                                    # 让选本页立刻能看到
                from . import library
                library.load_script.cache_clear()
            except Exception:                       # noqa: BLE001
                pass
        except Exception as exc:                    # noqa: BLE001
            with _LOCK:
                _JOBS[jid].update(status="error", error=str(exc)[:1500])
                _JOBS[jid]["log"].append("失败：" + str(exc)[:300])

    threading.Thread(target=run, daemon=True, name=f"scriptgen-{jid}").start()
    return jid


def job_state(jid: str) -> dict | None:
    with _LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return None
        return {k: (list(v) if k == "log" else v) for k, v in j.items() if k != "params"} | {
            "elapsed": round(time.time() - j["started"], 1)}
