"""本库加载、一致性校验、视角切分。

视角切分（build_view）是全项目最关键的一段代码，刻意不用 LLM：
LLM 做切分会"顺手"多给或漏给信息，且不可复现。信息隔离必须是确定性的。
"""
from __future__ import annotations

import json
from functools import lru_cache

from .config import SCRIPTS_DIR
from .models import (
    ActText,
    CharacterView,
    KnownOther,
    MyTimelineEntry,
    PublicClue,
    Script,
)


class ScriptError(ValueError):
    pass


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


# 临时登记的本子路径（不在 data/scripts 下的）。只给 scripts/check_script.py 用：
# 投稿者要在**不把文件塞进 data/ 目录**的前提下，把自己的本子完整打通一遍。
EXTRA_PATHS: dict[str, "Path"] = {}


def script_path(script_id: str):
    """找本子文件。先看 data/scripts/ 顶层，再递归找子目录（社区投稿放 community/）。

    约定文件名 == script_id。不靠读 JSON 里的 id 建索引，是因为那样每次列目录
    都要把所有本子（厚本一个就是 150KB）整个解析一遍。
    """
    if script_id in EXTRA_PATHS:
        return EXTRA_PATHS[script_id]
    direct = SCRIPTS_DIR / f"{script_id}.json"
    if direct.exists():
        return direct
    for p in SCRIPTS_DIR.rglob(f"{script_id}.json"):
        return p
    return None


def all_script_files() -> list:
    """所有本子文件，顶层的排前面，同名时顶层的优先（社区本子不能顶替官方本子）。"""
    seen: set[str] = set()
    out = []
    top = sorted(SCRIPTS_DIR.glob("*.json"))
    rest = sorted(p for p in SCRIPTS_DIR.rglob("*.json") if p.parent != SCRIPTS_DIR)
    for p in top + rest:
        if p.stem.startswith("_") or p.stem in seen:    # _template 之类的不上架
            continue
        seen.add(p.stem)
        out.append(p)
    return out


@lru_cache(maxsize=None)
def load_script(script_id: str) -> Script:
    path = script_path(script_id)
    if path is None:
        raise ScriptError(f"剧本不存在: {script_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    script = Script.model_validate(data)
    problems = validate_script(script)
    if problems:
        raise ScriptError(f"剧本 {script_id} 校验未通过:\n" + "\n".join(f"  - {p}" for p in problems))
    return script


# 上架失败的本子及原因。开放投稿之后，库里一定会混进格式不对的文件——
# 一个坏文件不能拖垮整个选本页，但也不能悄无声息：原因记在这里，
# `GET /api/scripts/errors` 和 check_script 都能看到。
LOAD_ERRORS: dict[str, str] = {}


def list_scripts() -> list[dict]:
    """本库目录，只返回公开信息（绝不含 solution）。"""
    out = []
    LOAD_ERRORS.clear()
    for path in all_script_files():
        try:
            script = load_script(path.stem)
        except Exception as exc:  # noqa: BLE001 —— pydantic 的校验异常不是 ScriptError
            LOAD_ERRORS[path.stem] = str(exc)[:600]
            continue
        out.append(
            {
                "script_id": script.script_id,
                "title": script.meta.title,
                "tagline": script.meta.tagline,
                "difficulty": script.meta.difficulty,
                "player_count": script.meta.player_count,
                "suspect_count": sum(1 for c in script.characters if not c.is_npc),
                "estimated_minutes": script.meta.estimated_minutes,
                "background": script.setting.public_background,
                "acts": script.meta.acts,
                "tags": script.meta.tags,
                "author": script.meta.author,
                "community": path.parent != SCRIPTS_DIR,
                "roles": [
                    {"id": c.id, "name": c.name, "public_role": c.public_role,
                     "is_npc": c.is_npc}
                    for c in script.characters
                ],
            }
        )
    return out


# --------------------------------------------------------------------------
# 一致性校验 —— 纯代码断言，拦掉的次品比任何后续环节都多，而且免费
# --------------------------------------------------------------------------


def _clock(t: str) -> int | None:
    """把「22:40」「夜里十点四十」这类写法折成分钟数。认不出来就返回 None。"""
    import re
    CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    t = t.strip()
    m = re.match(r"^(\d{1,2})[:：](\d{2})$", t)
    if m:
        return int(m[1]) * 60 + int(m[2])
    night = any(w in t for w in ("夜里", "晚上", "凌晨", "子时"))
    m = re.search(r"([一二两三四五六七八九十]{1,3})点"
                  r"(半|一刻|[一二三四五六七八九十]{1,3}分?)?", t)
    if not m:
        return None
    h = CN.get(m[1])
    if h is None:
        return None
    mm = 0
    if m[2] == "半":
        mm = 30
    elif m[2] == "一刻":
        mm = 15
    elif m[2]:
        mm = CN.get(m[2].rstrip("分"), 0) or 0
        if mm and mm < 10 and len(m[2].rstrip("分")) == 1:
            mm *= 10        # 「十点四」当「十点四十」
    if night and h < 12:
        h += 12
    if "凌晨" in t and h >= 12:
        h -= 12
    return h * 60 + mm


def validate_script(s: Script) -> list[str]:
    p: list[str] = []
    ids = [c.id for c in s.characters]
    slots = s.setting.time_slots

    # 背景里写的「几点发现尸体」要和时间线对得上。
    #
    # 《它没有叫》就栽在这儿：背景写「夜里十一点半发现」，可最后一个时间槽是
    # 22:40，而危棠在这一槽的行动正是「推开门就看见井台边的人」——
    # 同一件事两个时间，差五十分钟。玩家拿着自己的行踪表跟公开背景一对就露馅，
    # 而整个本子的核心玩法就是对时间。
    import re
    disc = [x for x in re.split(r"[。\n]", s.setting.public_background)
            if any(k in x for k in ("发现", "看见", "倒在", "死在", "挂在", "躺在"))]
    if disc and slots:
        # 跨零点要接着往后数，不能让 00:00 变回 0 点。
        # 《山门夜》的最后一槽是「23:00-00:00」，取末尾就是 00:00 = 0 分，
        # 一减就成了「差 1380 分钟」——本子被判不合格，load_script 抛异常，
        # list_scripts 静静跳过，整个本子从库里消失了。
        # 校验器误报的代价比漏报大得多，宁可放过也别错杀。
        def _night(v: int | None) -> int | None:
            return None if v is None else (v + 24 * 60 if v < 6 * 60 else v)

        got = _night(_clock(disc[0]))
        last = _night(_clock(slots[-1].split("-")[-1]))
        if got is not None and last is not None:
            # 允许发现时间落在最后一槽当口、或它之后半小时内
            # （很多本子最后一槽就是「发现之后大家的反应」，那种早于也正常）。
            if got > last + 45:
                p.append(
                    f"背景说 {got // 60}:{got % 60:02d} 发现尸体，"
                    f"但最后一个时间槽才 {slots[-1]}——差了 {got - last} 分钟，"
                    "玩家对时间表时会发现对不上")

    if len(set(ids)) != len(ids):
        p.append("角色 id 重复")
    if len(s.characters) != s.meta.player_count:
        p.append(f"角色数 {len(s.characters)} != meta.player_count {s.meta.player_count}")

    murderers = [c.id for c in s.characters if c.is_murderer]
    if len(murderers) != 1:
        p.append(f"必须恰好 1 名凶手，实际 {len(murderers)}: {murderers}")
    elif murderers[0] != s.solution.murderer:
        p.append(f"is_murderer({murderers[0]}) 与 solution.murderer({s.solution.murderer}) 不一致")
    for c in s.characters:
        if c.is_npc and c.is_murderer:
            p.append(f"{c.id} 既是 NPC 又是凶手——NPC 不参与投票，凶手必须能被票中")

    # 时间线必须是完整矩阵：角色数 × 时段数，一格不留空
    seen = {(t.char, t.slot) for t in s.timeline}
    for cid in ids:
        for slot in slots:
            if (cid, slot) not in seen:
                p.append(f"时间线缺失: {cid} @ {slot}")
    if len(s.timeline) != len(ids) * len(slots):
        p.append(f"时间线条目数 {len(s.timeline)} != {len(ids)}×{len(slots)}={len(ids)*len(slots)}")

    for t in s.timeline:
        if t.char not in ids:
            p.append(f"时间线引用未知角色 {t.char}")
        if t.slot not in slots:
            p.append(f"时间线引用未知时段 {t.slot}")
        if t.location not in s.setting.map:
            p.append(f"时间线地点 '{t.location}' 不在地图内 ({t.char} @ {t.slot})")
        for w in t.witnessed_by:
            if w not in ids:
                p.append(f"witnessed_by 引用未知角色 {w}")
            if w == t.char:
                p.append(f"{t.char} @ {t.slot} 目击者列表包含自己")

    # 目击关系必须双向：A 被 B 目击 ⇒ 同时段同地点必须有 B 的条目
    by_key = {(t.char, t.slot): t for t in s.timeline}
    for t in s.timeline:
        for w in t.witnessed_by:
            other = by_key.get((w, t.slot))
            if other is None:
                continue
            if other.location != t.location:
                p.append(
                    f"目击关系矛盾: {w} 目击了 {t.char} @ {t.slot}，"
                    f"但 {w} 在 '{other.location}' 而 {t.char} 在 '{t.location}'"
                )

    # 凶手必须在案发时段出现在案发地点
    if murderers:
        m = murderers[0]
        entry = by_key.get((m, s.case.death_slot))
        if entry is None:
            p.append(f"凶手 {m} 缺少案发时段 {s.case.death_slot} 的时间线")
        elif entry.location != s.case.death_location:
            p.append(
                f"凶手 {m} 在案发时段位于 '{entry.location}'，"
                f"但案发地点是 '{s.case.death_location}'"
            )

    # 线索
    #
    # 地点合法范围 = 基础地图 + 幕间会解锁的地点。原来只认基础地图，
    # 于是任何想放在「解锁后新地点」的线索都会被这条拒收——`unlock_location`
    # 这个 interlude 效果因此从来没被用过一次，21 个本子清一色 reveal_clue。
    unlockable = {it.location for it in s.interludes
                  if it.effect == "unlock_location" and it.location}
    valid_locations = set(s.setting.map) | unlockable
    clue_ids = [k.id for k in s.clues]
    if len(set(clue_ids)) != len(clue_ids):
        p.append("线索 id 重复")
    for k in s.clues:
        # 合成线索没有物理位置（它是「拼」出来的，不是「搜」出来的），location 留空。
        if not k.synth and not k.granted and k.location not in valid_locations:
            p.append(f"线索 {k.id} 的地点 '{k.location}' 不在地图内，也不是任何幕间解锁的地点")
        for target in k.points_to:
            if target not in ids:
                p.append(f"线索 {k.id} 的 points_to 引用未知角色 {target}")

    # 关键线索里不能混红鲱鱼，否则本子逻辑是坏的
    for kid in s.solution.minimum_clues_required:
        if kid not in clue_ids:
            p.append(f"minimum_clues_required 引用不存在的线索 {kid}")
        elif s.clue(kid).is_red_herring:
            p.append(f"关键线索 {kid} 被标为红鲱鱼——矛盾")

    if not any(k.is_red_herring for k in s.clues):
        p.append("没有任何红鲱鱼线索，本子会过于直白")

    # 每个地点至少 1 条线索，否则搜证会空手（解锁地点同理，不然幕间白开）
    for loc in valid_locations:
        if not s.clues_at(loc):
            p.append(f"地点 '{loc}' 没有任何线索")

    # 非凶手也要有隐瞒动机，否则十分钟结案。NPC 不是嫌疑人，不参与这套博弈，
    # 不需要隐瞒动机——他们的价值是被问出信息，不是被怀疑。
    for c in s.characters:
        if c.is_murderer:
            if c.cover_story is None:
                p.append(f"凶手 {c.id} 缺少 cover_story（假口供）")
            continue
        if c.is_npc:
            continue
        if not any(g.type == "conceal" for g in c.personal_goals):
            p.append(f"角色 {c.id} 没有 conceal 类个人目标，缺少隐瞒动机")

    p += _validate_extensions(s, ids, slots, valid_locations)
    return p


def _validate_extensions(s: Script, ids: list[str], slots: list[str],
                         valid_locations: set[str]) -> list[str]:
    """三幕 / 断片 / 合成线索 / 分层真相 的断言。

    老本子这些字段全是默认值，所以这里对它们一条都不会触发——
    加新机制不能让 21 个老本子的校验结果变一个字。
    """
    p: list[str] = []
    acts = s.meta.acts
    clue_ids = {k.id for k in s.clues}
    by_id = {k.id: k for k in s.clues}

    # ---- 幕 ----
    if not 2 <= acts <= 3:
        p.append(f"meta.acts 必须是 2 或 3，实际 {acts}")
    if s.meta.search_budget and len(s.meta.search_budget) != acts:
        p.append(f"meta.search_budget 要写 {acts} 个数（每幕一个），实际 {len(s.meta.search_budget)} 个")
    if any(n < 0 or n > 6 for n in s.meta.search_budget):
        p.append("meta.search_budget 每个数应在 0–6 之间")

    for c in s.characters:
        seen_acts: set[int] = set()
        base_goal_ids = {g.id for g in c.personal_goals}
        all_goal_ids = set(base_goal_ids)
        for b in c.acts:
            if not 2 <= b.act <= acts:
                p.append(f"{c.id} 的追加剧本 act={b.act} 超出范围 2–{acts}")
            if b.act in seen_acts:
                p.append(f"{c.id} 第 {b.act} 幕写了不止一份追加剧本")
            seen_acts.add(b.act)
            for g in b.goals:
                if g.id in all_goal_ids:
                    p.append(f"{c.id} 的目标 id '{g.id}' 重复")
                all_goal_ids.add(g.id)
            for gid in b.retract_goals:
                if gid not in all_goal_ids:
                    p.append(f"{c.id} 第 {b.act} 幕要退休的目标 '{gid}' 不存在")
            for sl in b.restore_slots:
                if sl not in c.amnesia_slots:
                    p.append(f"{c.id} 第 {b.act} 幕要恢复的时段 '{sl}' 本来就没断片")
            for gk in b.grant_clues:
                if gk not in clue_ids:
                    p.append(f"{c.id} 第 {b.act} 幕要发的随身线索 '{gk}' 不存在")
                elif not by_id[gk].granted:
                    p.append(f"{c.id} 第 {b.act} 幕发的 '{gk}' 没标 granted=true"
                             "（普通线索是搜出来的，不是发的）")
        for sl in c.amnesia_slots:
            if sl not in slots:
                p.append(f"{c.id} 的断片时段 '{sl}' 不在 time_slots 里")
        if c.is_murderer and c.amnesia_slots:
            p.append(f"凶手 {c.id} 不能有断片——他要演假口供，不能同时说自己不记得")

    # ---- 幕间 ----
    for i, it in enumerate(s.interludes):
        if it.at not in ("act2", "act3"):
            p.append(f"幕间 #{i} 的 at='{it.at}' 只能是 act2 或 act3")
        if it.at == "act3" and acts < 3:
            p.append(f"幕间 #{i} 写了 act3，但 meta.acts={acts}")
        if it.effect == "reveal_clue":
            if it.clue_id not in clue_ids:
                p.append(f"幕间 #{i} 要公开的线索 '{it.clue_id}' 不存在")
            elif by_id[it.clue_id].synth:
                p.append(f"幕间 #{i} 不能直接公开合成线索 '{it.clue_id}'（它得靠拼出来）")
        if it.effect == "unlock_location" and not it.location:
            p.append(f"幕间 #{i} 是 unlock_location 却没写 location")

    # ---- 合成线索 ----
    for k in s.clues:
        if k.granted:
            if k.location:
                p.append(f"随身线索 {k.id} 的 location 应留空（它不是搜出来的）")
            if k.synth:
                p.append(f"线索 {k.id} 不能既是 synth 又是 granted")
        if not k.synth:
            if k.from_clues:
                p.append(f"线索 {k.id} 写了 from_clues 却没标 synth=true")
            continue
        if k.location:
            p.append(f"合成线索 {k.id} 的 location 应留空（它不是搜出来的）")
        if len(set(k.from_clues)) < 2:
            p.append(f"合成线索 {k.id} 至少要 2 条不同的 from_clues")
        for f in k.from_clues:
            if f not in clue_ids:
                p.append(f"合成线索 {k.id} 的 from_clues 引用不存在的线索 {f}")
            elif f == k.id:
                p.append(f"合成线索 {k.id} 不能由自己合成")
        if not k.combine_note:
            p.append(f"合成线索 {k.id} 缺 combine_note（拼成的当场旁白，没它玩家不知道发生了什么）")
    # 合成链不能成环（A 要 B，B 要 A，谁也拼不出来）
    def _depth(kid: str, trail: tuple[str, ...] = ()) -> int:
        if kid in trail:
            return 10**6
        k = by_id.get(kid)
        if k is None or not k.synth:
            return 0
        return 1 + max((_depth(f, trail + (kid,)) for f in k.from_clues if f in by_id), default=0)
    for k in s.clues:
        if k.synth and _depth(k.id) >= 10**6:
            p.append(f"合成线索 {k.id} 的合成链成环")

    # ---- 分层真相 ----
    qids: set[str] = set()
    for q in s.solution.questions:
        if q.id in qids:
            p.append(f"问题 id '{q.id}' 重复")
        qids.add(q.id)
        if q.points < 1:
            p.append(f"问题 '{q.id}' 分值必须 ≥ 1")
        if q.kind == "character":
            allowed = [o.id for o in q.options] or [c.id for c in s.characters if not c.is_npc]
            if any(o not in ids for o in allowed):
                p.append(f"问题 '{q.id}' 的选项里有不存在的角色")
            if q.answer not in allowed:
                p.append(f"问题 '{q.id}' 的答案 '{q.answer}' 不在可选的人里")
        elif q.kind == "location":
            allowed = [o.id for o in q.options] or list(s.setting.map)
            if q.answer not in allowed:
                p.append(f"问题 '{q.id}' 的答案 '{q.answer}' 不在可选地点里")
        else:
            opt_ids = [o.id for o in q.options]
            if len(opt_ids) < 2:
                p.append(f"问题 '{q.id}' 是 choice 型，至少要 2 个选项")
            if len(set(opt_ids)) != len(opt_ids):
                p.append(f"问题 '{q.id}' 的选项 id 重复")
            if q.answer not in opt_ids:
                p.append(f"问题 '{q.id}' 的答案 '{q.answer}' 不在选项里")
    if len(s.solution.questions) > 10:
        p.append("问题超过 10 个——答卷太长，玩家会乱填")
    if not 0 < s.solution.question_pass <= 1:
        p.append("question_pass 应在 (0, 1] 之间")

    return p


# --------------------------------------------------------------------------
# 视角切分 ★ 信息隔离的实现
# --------------------------------------------------------------------------


def build_view(s: Script, cid: str, act: int = 1) -> CharacterView:
    """从上帝视角真相里，机械地切出单个角色所知的一切。

    切分规则（刻意保持简单以便审计）：
      1. 自己的全部时间线与观察——**断片的时段除外**（见 Character.amnesia_slots）
      2. witnessed_by 含自己的他人行动
      3. 公共信息（背景、地图、死者、发现尸体的经过）
      4. 追加剧本：只给 act <= 当前幕的那些
    其余一律不给。

    `act` 默认 1，也就是**默认只看得见第一幕**：忘了传这个参数，后果是少给，
    不是多给。反过来默认全开的话，漏传一处就等于把第三幕的真相摆在第一幕的屏幕上。
    需要全部内容的地方（复盘、校验）显式传 `act=s.meta.acts`。
    """
    me = s.char(cid)

    blocks = sorted((b for b in me.acts if b.act <= act), key=lambda b: b.act)
    restored = {slot for b in blocks for slot in b.restore_slots}
    gaps = [sl for sl in me.amnesia_slots if sl not in restored]

    def _known(t) -> bool:
        return t.char == cid and t.slot not in gaps

    my_timeline = [
        MyTimelineEntry(slot=t.slot, location=t.location, action=t.action)
        for t in sorted(s.timeline, key=lambda t: s.setting.time_slots.index(t.slot))
        if _known(t)
    ]
    # 断片时段里「注意到的事」也一并拿掉——那几个小时他本来就不记得。
    my_observations = [obs for t in s.timeline if _known(t) for obs in t.observed]
    my_observations += [o for b in blocks for o in b.observations]

    known_others = [
        KnownOther(char_name=s.char(t.char).name, slot=t.slot, fact=f"在{t.location}，{t.action}")
        for t in sorted(s.timeline, key=lambda t: s.setting.time_slots.index(t.slot))
        if t.char != cid and cid in t.witnessed_by
        and t.slot not in gaps          # 断片的时段，连“看见别人在干什么”也一并忘掉
    ]

    retired = {gid for b in blocks for gid in b.retract_goals}
    goals = [g for g in me.personal_goals if g.id not in retired]
    goals += [g for b in blocks for g in b.goals if g.id not in retired]

    secret = me.private_secret
    for b in blocks:
        if b.secret_add:
            secret += f"\n\n【第{b.act}幕·新的真相】{b.secret_add}"

    return CharacterView(
        char_id=cid,
        name=me.name,
        public_role=me.public_role,
        public_background=s.setting.public_background,
        map=s.setting.map,
        time_slots=s.setting.time_slots,
        case_public={
            "死者": s.case.victim,
            "死亡时段": s.case.death_slot,
            "陈尸地点": s.case.death_location,
            "死因": s.case.cause,
            "发现者": s.char(s.case.body_found_by).name,
            "发现时段": s.case.body_found_slot,
        },
        roster=[
            {"name": c.name, "public_role": c.public_role}
            for c in s.characters
            if c.id != cid
        ],
        private_secret=secret,
        motive=me.motive,
        personality=me.personality,
        speech_style=me.speech_style,
        sample_lines=me.sample_lines,
        personal_goals=goals,
        my_timeline=my_timeline,
        my_observations=my_observations,
        known_others=known_others,
        is_murderer=me.is_murderer,
        is_npc=me.is_npc,
        cover_story=me.cover_story if me.is_murderer else None,
        acts_text=[ActText(act=b.act, title=b.title, text=b.text) for b in blocks],
        gaps=gaps,
        act=act,
    )


def public_clue(k) -> PublicClue:
    """剥掉线索的所有元信息，只留玩家/AI 该看到的部分。"""
    return PublicClue(id=k.id, location=k.location, text=k.text)


# --------------------------------------------------------------------------
# 自检：确保切分产物里没有泄漏
# --------------------------------------------------------------------------


def assert_no_leak(s: Script, view: CharacterView) -> list[str]:
    """把视角序列化成文本，检查是否混入了本不该出现的敏感串。

    三幕之后多了一类泄漏：**跨幕**。第一幕的视角里不能出现他自己第三幕的真相，
    也不能出现别人任何一幕的追加剧本。所以校验必须**逐幕**跑——
    `build_view(s, cid, act=k)` 对 k = 1..acts 各检查一遍（validate_all / check_script 都这么做）。
    """
    blob = view.model_dump_json()

    def has(text: str) -> bool:
        # 多行文本在 JSON 里是换行转义的，原样子串匹配会对它们永远失明。
        # 厚本里大量多行文本，两种写法都要查。
        if not text:
            return False
        esc = json.dumps(text, ensure_ascii=False)[1:-1]
        return text in blob or esc in blob

    leaks = []

    # 他人的秘密、他人的追加剧本都不能出现
    for c in s.characters:
        if c.id == view.char_id:
            continue
        if has(c.private_secret):
            leaks.append(f"泄漏了 {c.id} 的秘密")
        for b in c.acts:
            if has(b.text):
                leaks.append(f"泄漏了 {c.id} 第 {b.act} 幕的追加剧本")
            if has(b.secret_add):
                leaks.append(f"泄漏了 {c.id} 第 {b.act} 幕的新秘密")

    # 自己还没解锁的那几幕也不能提前出现
    me = s.char(view.char_id)
    for b in me.acts:
        if b.act > view.act:
            if has(b.text):
                leaks.append(f"第 {view.act} 幕的视角里提前出现了自己第 {b.act} 幕的追加剧本")
            if has(b.secret_add):
                leaks.append(f"第 {view.act} 幕的视角里提前出现了自己第 {b.act} 幕的新秘密")

    # 推理链和关键线索文本不能出现
    for step in s.solution.reasoning_chain:
        if has(step):
            leaks.append(f"泄漏了推理链: {step[:30]}")
    for k in s.clues:
        if has(k.text):
            leaks.append(f"泄漏了线索原文 {k.id}")
        if has(k.reveals):
            leaks.append(f"泄漏了线索解读 {k.id}")
    for q in s.solution.questions:
        if has(q.explain):
            leaks.append(f"泄漏了问题 '{q.id}' 的解析")

    # 非凶手不该知道任何人是凶手
    if not view.is_murderer:
        if view.cover_story is not None:
            leaks.append("非凶手拿到了 cover_story")
        murderer = s.char(s.solution.murderer)
        # 凶手自己的视角里当然有他自己的动机——只查「别人」
        if view.char_id != murderer.id and has(murderer.motive):
            leaks.append("泄漏了凶手动机")

    return leaks
