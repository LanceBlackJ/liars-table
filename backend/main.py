"""FastAPI 应用。

安全不变式：本模块任何返回给前端的东西都**不得**包含
solution / is_murderer(他人) / 他人秘密 / 线索的 points_to·is_red_herring。
唯一例外是 REVEAL 阶段之后的 result。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import avatar, config, engine, games, llm, scriptgen
from .config import SETTINGS, WEB_DIR
from .engine import PHASE_LABEL, SEARCH_PHASES, Phase
from .library import ScriptError, build_view, list_scripts, load_script
from .models import AvatarSpec, CharacterView

app = FastAPI(title="AI 剧本杀")


# --------------------------------------------------------------------------
# 玩家剧本渲染
# --------------------------------------------------------------------------


def render_brief(view: CharacterView) -> str:
    """把角色视角渲染成给真人玩家读的剧本。内容与 AI 拿到的**完全一致**。"""
    label = {"conceal": "隐瞒", "acquire": "取得", "protect": "保护", "expose": "揭穿"}
    lines = [
        f"# {view.name}",
        f"*{view.public_role}*",
        "",
        "## 你的秘密",
        view.private_secret,
        "",
        f"**动机**：{view.motive}",
        "",
        "## 你的性格",
        view.personality,
        "",
        "## 当晚你的行踪",
    ]
    lines += [f"- **{t.slot}** 在{t.location}：{t.action}" for t in view.my_timeline]
    if view.gaps:
        # 断片的时段：不能只是少几行，要明明白白告诉玩家「这几个钟头你不记得」，
        # 否则他会以为是剧本漏写了。
        lines += ["", f"> ⚠️ **{'、'.join(view.gaps)}** 这几个时段你**没有任何记忆**。"
                      "别人看见了你什么、现场有什么痕迹，是你倒推那会儿干了什么的唯一办法。"]

    lines += ["", "## 你注意到的事"]
    lines += (
        [f"- {o}" for o in view.my_observations]
        if view.my_observations
        else ["- （没有特别注意到什么）"]
    )

    lines += ["", "## 你能为谁作证"]
    lines += (
        [f"- **{k.slot}** {k.char_name}：{k.fact}" for k in view.known_others]
        if view.known_others
        else ["- （没有——你无法为任何人作证，也没人能为你作证）"]
    )

    if not view.is_npc:
        # NPC 没有个人目标——见 agents.py 同一处的注释。
        lines += ["", "## 你的个人目标"]
        lines += [f"- **[{label.get(g.type, g.type)}]** {g.text}" for g in view.personal_goals]

    for a in view.acts_text:
        lines += ["", "---", f"## 第{a.act}幕　{a.title or '你的剧本多了一页'}", a.text]

    if view.is_npc:
        lines += [
            "",
            "---",
            "## 你是证人，不是嫌疑人",
            "你不参与投票，也不会被投票。你的角色是被问出信息——如实说你看到的、知道的。",
        ]
    elif view.is_murderer and view.cover_story:
        cs = view.cover_story
        lines += [
            "",
            "---",
            "## ⚠️ 你就是凶手",
            f"你杀了{view.case_public['死者']}。**胜利条件：投票结束时不被票出。**",
            "",
            "### 你准备好的口供",
        ]
        if cs.claim:
            lines += [f"> {cs.claim}", ""]
        lines += [
            f"- **{f.slot}** 你声称在{f.claimed_location}：{f.claimed_action}"
            for f in cs.fake_timeline
        ]
        if cs.weak_points:
            lines += ["", "### 口供的薄弱环节"]
            lines += [
                f"- 若出现线索 **{w.clue_id}** — 风险：{w.risk}  \n  应对：{w.fallback}"
                for w in cs.weak_points
            ]
        if cs.redirect_hook:
            lines += ["", f"**可引导的方向**：{cs.redirect_hook}"]
    else:
        lines += [
            "",
            "---",
            "## 你不是凶手",
            "你的目标：找出真凶，同时保住自己的秘密。",
            "为了自证清白而把秘密全抖出来，会让你的个人目标失败。",
        ]

    return "\n".join(lines)


# --------------------------------------------------------------------------
# 状态序列化
# --------------------------------------------------------------------------


def _timeline_board(g: engine.Game, view: CharacterView) -> dict[str, Any]:
    """时间线整理板。

    格子分两种：
      · **确凿**——来自玩家自己的剧本（他自己的行踪、以及他能为谁作证）。
        这些是他本来就知道的事实，预填进去省得他抄一遍。
      · **空白**——他只能靠听别人说来填，笔记内容完全由他自己写。

    这里绝不能把 script.timeline 直接摊开：那是上帝视角，
    等于把所有人的真实行踪送到玩家眼前，本子当场作废。
    只用 build_view 切出来的东西。
    """
    slots = view.time_slots
    mine = {t.slot: f"{t.location}：{t.action}" for t in view.my_timeline}
    # known_others 是「我能为谁作证」，按 (人名, 时段) 索引
    vouch: dict[tuple[str, str], str] = {
        (k.char_name, k.slot): k.fact for k in view.known_others
    }
    # 行用剧本的角色表（id / 姓名 / 公开身份都是公开信息，选角页就在用）。
    # view.roster 不含自己、也没有 id，做不了行键。
    return {
        "slots": slots,
        "rows": [
            {
                "id": c.id,
                "name": c.name,
                "role": c.public_role,
                "is_me": c.id == g.player_char_id,
                "cells": [
                    {
                        "slot": s,
                        # 确凿信息：自己的行踪 / 我能作证的他人行踪
                        "fact": (mine.get(s, "") if c.id == g.player_char_id
                                 else vouch.get((c.name, s), "")),
                        "note": g.notes.get(f"{c.id}|{s}", ""),
                    }
                    for s in slots
                ],
            }
            for c in g.script.characters
        ],
    }


def game_state(g: engine.Game, *, include_brief: bool = False) -> dict[str, Any]:
    s = g.script
    view = g._view(g.player_char_id)

    state: dict[str, Any] = {
        "game_id": g.game_id,
        "script_id": g.script_id,
        "title": s.meta.title,
        "phase": g.phase.value,
        "phase_label": PHASE_LABEL.get(g.phase, g.phase.value),
        "player_char_id": g.player_char_id,
        "player_name": view.name,
        "map": g.searchable_map(),   # 含幕间解锁的新地点
        "time_slots": s.setting.time_slots,
        "roster": [
            # speech_style 用于朗读时区分嗓音。它描述的是「这个人怎么说话」，
            # 玩家本来就听得见，不是秘密。personality 不下发——那更接近内心刻画。
            {"id": c.id, "name": c.name, "public_role": c.public_role,
             "speech_style": c.speech_style,
             # 朗读挑音色。优先用剧本里写明的 voice；没写才按发型猜。
             "voice_hint": (c.voice or ("f" if (c.avatar and c.avatar.hair in
                                                ("bun", "long", "updo")) else "m")),
             # 前端要靠这个把 NPC 和嫌疑人区分开——不参与投票的人
             # 混在嫌疑人名单里，玩家会以为漏掉了一票。
             "is_npc": c.is_npc}
            for c in s.characters
        ],
        # 必须过可见性过滤，否则续玩时会把别人的私聊内容一次性灌给前端
        "public_log": g.log_for_player(),
        "my_clues": g._my_clues(g.player_char_id),
        "waiting": g._is_player_turn(),
        "action": g._expected(),
        "pending_question": g.pending_question
        if g.pending_question and g.pending_question["target_id"] == g.player_char_id
        else None,
        "questions_left": g.questions_left.get(g.player_char_id, 0),
        "board": _timeline_board(g, view),
        "pad": g.pad,
        "private_left": sum(1 for c in g.queue if c == g.player_char_id),
        "searches_left": sum(1 for c in g.queue if c == g.player_char_id)
        if g.phase in SEARCH_PHASES
        else 0,
        "finished": g.phase == Phase.DONE,
        # 第几幕 / 共几幕——前端据此在徽章里标出「第二幕」
        "act": g.cur_act,
        "acts": g.script.meta.acts,
    }
    # 答卷：只在答卷阶段下发题目。绝不带 answer / explain——那是答案本身。
    if g.phase == Phase.DEDUCTION:
        state["questions"] = [
            {"id": q.id, "prompt": q.prompt, "kind": q.kind, "points": q.points,
             "options": g.question_options(q),
             "mine": g.truth_answers.get(q.id, "")}
            for q in s.solution.questions
        ]
    if include_brief:
        state["brief"] = render_brief(view)
    # 真相只在结束后释放
    if g.phase == Phase.DONE and g.result:
        state["result"] = g.result
    return state


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


class NewGameReq(BaseModel):
    script_id: str
    player_char_id: str


@app.get("/api/health")
def api_health() -> dict:
    h = llm.health()
    h["base_url"] = SETTINGS.base_url
    h["model_chat"] = SETTINGS.model_chat
    h["rate"] = llm.rate_state()
    return h


def _local_only(request: Request) -> None:
    """改模型设置只允许本机操作：这个接口能换掉接口地址和密钥，
    部署到公网后绝不能让陌生人调用。"""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "模型设置只能在运行服务的这台电脑上修改")


class ConfigReq(BaseModel):
    base_url: str = ""
    api_key: str = ""            # 空 = 不改
    model_chat: str = ""
    model_reason: str = ""
    bypass_proxy: bool | None = None


@app.get("/api/config")
def api_config_get(request: Request) -> dict:
    _local_only(request)
    return config.public_view()


@app.post("/api/config")
def api_config_set(req: ConfigReq, request: Request) -> dict:
    _local_only(request)
    try:
        config.update_user_config(req.model_dump())
    except config.ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    return config.public_view()


@app.delete("/api/config")
def api_config_reset(request: Request) -> dict:
    _local_only(request)
    config.reset_user_config()
    return config.public_view()


@app.post("/api/config/test")
def api_config_test(request: Request) -> dict:
    """用当前设置真调一次模型，告诉用户通不通、多快。"""
    import time
    _local_only(request)
    if SETTINGS.mock:
        return {"ok": False, "detail": "还没有可用的密钥，当前是离线模式"}
    t0 = time.time()
    h = llm.health()
    h["seconds"] = round(time.time() - t0, 1)
    return h


@app.get("/api/rate")
def api_rate() -> dict:
    """网关限流余量。前端在等待时显示，让玩家知道为什么慢。"""
    return llm.rate_state()


@app.get("/api/avatar/{script_id}/{char_id}.svg")
def api_avatar(script_id: str, char_id: str) -> Response:
    """角色头像。确定性生成，可长期缓存。"""
    try:
        script = load_script(script_id)
        char = script.char(char_id)
    except (ScriptError, KeyError) as exc:
        raise HTTPException(404, str(exc)) from exc

    spec = char.avatar or avatar.fallback_spec(char.id, char.name)
    return Response(
        content=avatar.render(spec),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --------------------------------------------------------------------------
# 多游戏路由
# --------------------------------------------------------------------------


class NewAnyGameReq(BaseModel):
    kind: str
    script_id: str | None = None
    player_char_id: str | None = None
    human_name: str | None = None
    player_count: int | None = None
    roles: dict[str, int] | None = None
    case_id: str | None = None      # 探案选案件；不给就用第一个


@app.get("/api/avatar/case/{case_id}/{pid}.svg")
def api_case_avatar(case_id: str, pid: str) -> Response:
    """探案人物头像。特征写在案件 JSON 里。"""
    from .games.detective import load_case

    try:
        person = load_case(case_id).person(pid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    spec = person.avatar or avatar.fallback_spec(person.id, person.name)
    return Response(
        content=avatar.render(spec),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/cases")
def api_cases() -> list[dict]:
    from .games.detective import list_cases

    return list_cases()


@app.get("/api/catalog")
def api_catalog() -> list[dict]:
    """首页：三款游戏的门面信息。"""
    return games.CATALOG


@app.get("/api/saves")
def api_saves() -> list[dict]:
    return games.list_saves()


@app.delete("/api/saves/{game_id}")
def api_delete_save(game_id: str) -> dict:
    return {"deleted": games.delete(game_id)}


@app.get("/api/avatar/game/{game_id}/{player_id}.svg")
def api_game_avatar(game_id: str, player_id: str) -> Response:
    """狼人杀 / 谁是卧底的玩家头像。特征存在存档里，不来自剧本 JSON。"""
    try:
        g = games.load(game_id)
        spec_dict = next(
            p.avatar for p in g.players if p.id == player_id  # type: ignore[attr-defined]
        )
    except (KeyError, StopIteration, AttributeError) as exc:
        raise HTTPException(404, "头像不存在") from exc
    spec = AvatarSpec(**spec_dict) if spec_dict else avatar.fallback_spec(player_id, player_id)
    return Response(
        content=avatar.render(spec),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/g")
def api_new_any(req: NewAnyGameReq) -> dict:
    opts: dict = {
        "human_name": req.human_name,
        "player_count": req.player_count,
        "roles": req.roles,
        "case_id": req.case_id,     # 探案选的案子，不传就用默认那个
    }
    if req.kind == "mystery":
        if not req.script_id or not req.player_char_id:
            raise HTTPException(400, "剧本杀需要 script_id 和 player_char_id")
        opts.update(script_id=req.script_id, player_char_id=req.player_char_id)
    try:
        g = games.create(req.kind, **opts)
    except (ScriptError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return g.player_state()


@app.get("/api/g/{game_id}")
def api_get_any(game_id: str) -> dict:
    try:
        return games.load(game_id).player_state()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/g/{game_id}/step")
def api_step_any(game_id: str) -> dict:
    try:
        g = games.load(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        out = g.step()
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    g.save()
    out["state"] = g.player_state()
    return out


@app.post("/api/g/{game_id}/autospeak")
def api_autospeak(game_id: str) -> dict:
    """「替我说」——AI 用**你自己的视角**替你写一整句可以直接发的话。

    四款游戏都支持。每一款走的都是**该游戏自己的 AI 发言函数**，
    只是把角色换成玩家：
      · 剧本杀   agents.act / agents.answer（和 AI 角色发言同一个函数）
      · 狼人杀   compose_speech（带「今天谁说过什么」和必须点名回应的要求）
      · 谁是卧底 _ai_describe（带前面的人已说过什么、禁止重复）
      · 探案     按手里的物证 + 这个人说过的话，拟一句要问的

    刻意不为「替我说」另写一套提示词：另写一套，生成的话迟早会和
    AI 的行为、和玩家实际掌握的信息对不上。
    """
    try:
        g = games.load(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    try:
        return _autospeak_for(g)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc


def _autospeak_for(g: Any) -> dict:
    from . import agents

    kind = getattr(g, "kind", "mystery")

    # ---- 剧本杀 ----
    if kind == "mystery":
        inner = g.inner
        act = inner._expected()
        view = inner._view(inner.player_char_id)
        log = inner.log_for_player()
        clues = inner._my_clues(inner.player_char_id)

        if act == "answer" and inner.pending_question:
            q = inner.pending_question
            out = agents.answer(view, q.get("question", ""),
                                q.get("asker_name", "有人"), log, clues)
        elif act == "speak":
            ph = inner.phase
            kindname = ("intro" if ph == Phase.INTRO
                        else "final_words" if ph == Phase.FINAL_WORDS
                        else "round_table")
            out = agents.act(view, kindname, log, clues)
        elif act == "private":
            out = agents.act(view, "private", log, clues,
                             extra="你要把某个人单独拉到一边说话，内容别人听不到。"
                                   "开口第一句该说什么？")
        else:
            raise HTTPException(400, f"当前阶段（{act}）不能代发言")
        # reveal_clues 一并带回：前端据此把要公开的线索自动勾上
        return {"text": out.get("speech", ""),
                "reveal_clues": out.get("reveal_clues", [])}

    # ---- 狼人杀 ----
    if kind == "werewolf":
        if not g._is_player_turn():
            raise HTTPException(400, "现在不是你的发言回合")
        # 夜里的狼队商议也要能代发言——那个面板上同样挂着「替我说」按钮，
        # 只认白天的话，玩家一点就是 400。
        if g.phase == "night_wolf":
            return {"text": g.compose_wolf_talk(g.human_id), "reveal_clues": []}
        if g.phase == "last_words":
            return {"text": g.compose_last_words(g.human_id), "reveal_clues": []}
        if g.phase != "day_discuss":
            raise HTTPException(400, "现在不是你的发言回合")
        return {"text": g.compose_speech(g.human_id), "reveal_clues": []}

    # ---- 谁是卧底 ----
    if kind == "spy":
        if g.phase not in ("describe", "pk_describe") or not g._is_player_turn():
            raise HTTPException(400, "现在不是你的描述回合")
        # record=False：这是替玩家代写，不该把 AI 的自我判断记成玩家做过的判断
        return {"text": g._ai_describe(g.human_id, record=False), "reveal_clues": []}

    # ---- 探案 ----
    if kind == "detective":
        raise HTTPException(400, "探案的代提问请带上 person_id")

    raise HTTPException(400, f"未知游戏类型 {kind}")


class AskForMeReq(BaseModel):
    person_id: str


@app.post("/api/g/{game_id}/autoask")
def api_autoask(game_id: str, req: AskForMeReq) -> dict:
    """探案专用：替玩家想一句该问这个人的话。

    依据只有两样，都是玩家已经有的：他手里的物证，和这个人已经说过的话。
    案件真相、这个人下一层证词里有什么，一律不给。
    """
    try:
        g = games.load(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if getattr(g, "kind", "") != "detective":
        raise HTTPException(400, "这个接口只给探案用")

    from .games.detective import load_case
    from .hints import ask_for

    c = load_case(g.case_id)
    try:
        person = c.person(req.person_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    said = [str(e.get("text", "")) for e in g.log
            if e.get("kind") in ("say", "crack") and e.get("person_id") == req.person_id]
    asked = [str(e.get("text", "")) for e in g.log
             if e.get("kind") == "ask" and e.get("person_id") == req.person_id]

    return {"text": ask_for(
        person=person.name, role=person.role,
        evidence=[f"{c.ev(x).name}——{c.ev(x).detail}" for x in g.evidence],
        their_words=said, already_asked=asked,
    )}


class PadReq(BaseModel):
    text: str


@app.post("/api/g/{game_id}/pad")
def api_pad(game_id: str, req: PadReq) -> dict:
    """存通用记事本。四款游戏都有，纯玩家私有。"""
    try:
        g = games.load(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    inner = getattr(g, "inner", g)
    inner.pad = req.text[:8000]
    g.save()
    return {"ok": True, "len": len(inner.pad)}


class NoteReq(BaseModel):
    key: str        # "角色id|时段"
    text: str


@app.post("/api/g/{game_id}/note")
def api_note(game_id: str, req: NoteReq) -> dict:
    """存一格时间线笔记。纯玩家私有，不参与判定也不喂给 AI。"""
    try:
        g = games.load(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    inner = getattr(g, "inner", g)
    if not hasattr(inner, "notes"):
        raise HTTPException(400, "这个游戏没有时间线板")
    text = req.text.strip()[:200]
    if text:
        inner.notes[req.key] = text
    else:
        inner.notes.pop(req.key, None)
    g.save()
    return {"ok": True, "count": len(inner.notes)}


@app.post("/api/g/{game_id}/hint")
def api_hint(game_id: str) -> dict:
    """「我不知道说什么」——给玩家几个发言方向。

    安全性全靠 hints 模块的入参：它只拿得到玩家自己的视角和
    **过滤过的**日志，拿不到 solution、拿不到别人的秘密。
    这里每一路分支都要亲自把日志过一遍可见性。
    """
    try:
        g = games.load(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    # 不轮到你就别问。
    #
    # 界面上「给我点方向」只挂在动作面板里，正常玩不会在别的时候点到；
    # 但实测直接调这个接口，开局第一夜就能拿到「可以亮出昨晚已使用解药的
    # 事实」这种提示——那会儿一晚都还没过完，解药当然没人用过。
    # 模型手上只有一句开场白，硬要它给三条方向，它就只能编。
    # 与其去调提示词，不如干脆别在没有上下文的时候问。
    # 只对说得清「现在轮到谁」的游戏设这道闸。探案没有回合概念
    # （它是自由行动，随时都能问人、搜物），拿不到 _expected 就别拦，
    # 否则以后给探案接上提示，会莫名其妙地一直 400。
    inner = getattr(g, "inner", g)
    if hasattr(inner, "_expected") and not inner._expected():
        raise HTTPException(400, "现在不是你的回合，没什么可提示的")

    try:
        tips = _hints_for(g)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"tips": tips}


def _hints_for(g: Any) -> list[str]:
    from . import hints

    kind = getattr(g, "kind", "mystery")

    if kind == "mystery":
        inner = getattr(g, "inner", g)         # MysteryAdapter 把 engine.Game 包在 .inner
        view = inner._view(inner.player_char_id)
        act = inner._expected()
        sit = {
            "speak": "轮到他在圆桌上发言。",
            "answer": "有人当众质问他，他必须正面回应。",
            "ask": "轮到他自由质询，可以点名问任何一个人一个问题。",
            "private": "他可以把某个人单独拉到一边说话，内容别人听不到。",
            "vote": "该投票了，他要说清楚投谁、为什么。",
        }.get(act, "轮到他行动。")
        extra = ""
        if inner.pending_question and inner.pending_question["target_id"] == inner.player_char_id:
            extra = f"质问他的原话是：「{inner.pending_question.get('question', '')}」"
        return hints.suggest(
            view, inner.log_for_player(), inner._my_clues(inner.player_char_id),
            situation=sit, extra=extra,
        )

    if kind == "detective":
        # 探案的提示走 /autoask（要带 person_id，问的是**具体某个人**）。
        # 这里不能往下走：下面那段拿的是 g.human、g.log 这些桌游才有的东西，
        # 探案没有，直接就是一个 AttributeError → 500。
        raise HTTPException(400, "探案请用 /autoask，并带上 person_id")

    # 狼人杀 / 谁是卧底：没有 CharacterView，手工组装可见信息
    me = g.human
    lines = []
    for e in g.log.visible(g.human_id):     # ← 可见性过滤在这里
        if e["kind"] == "speech":
            lines.append(f"{e['speaker_name']}：{e['text']}")
        elif e["kind"] in ("narration", "system", "elimination"):
            lines.append(f"【{e['text']}】")

    if kind == "werewolf":
        private = f"你的身份是【{me.role_label}】。"
        mates = [p.name for p in g.players if p.role == "wolf" and p.id != me.id]
        if me.role == "wolf" and mates:
            private += f" 你的狼同伴是 {'、'.join(mates)}——这件事只有你们知道。"
        if me.role == "seer" and g.seer_results:
            private += " 你验过：" + "、".join(
                f"{g.name(t)} 是{r}" for t, r in g.seer_results.items()
            )
        # 阶段要照实说。原来这里不管当前是什么阶段，一律写「轮到他白天发言」——
        # 于是第一夜狼队商议时，模型也按白天来想，张口就是
        # 「询问昨晚被杀者的具体情况」，可那会儿一个人都还没死。
        sit = {
            "night_wolf": f"现在是第 {g.round} 夜，狼队在私下商量今晚刀谁。"
                          "这些话只有狼队听得见，不用伪装。",
            "night_wolf_vote": f"第 {g.round} 夜，狼队要投出今晚刀谁，必须全队一致。",
            "night_seer": f"第 {g.round} 夜，他要挑一个人查验身份。",
            "night_witch": f"第 {g.round} 夜，他要决定用不用解药/毒药。",
            "night_guard": f"第 {g.round} 夜，他要挑一个人守护。",
            "day_discuss": f"第 {g.round} 天白天，轮到他发言，要说出怀疑谁、依据是什么。",
            "day_vote": f"第 {g.round} 天要投票放逐一个人了。",
            "last_words": "他已经出局，这是最后一次说话的机会。",
            "hunter_shoot": "他倒下了，可以开枪带走一个人。",
        }.get(g.phase, f"第 {g.round} 天，轮到他行动。")
        if g.round == 1 and not any(e["kind"] == "elimination" for e in g.log.events):
            sit += "\n**注意：现在还没有任何人出局，也还没有过白天的发言。**"
    else:
        private = f"你手里的词是【{g.words.get(g.human_id, '')}】。你不知道自己是不是卧底。"
        sit = ("轮到他描述自己的词。不能说出词本身，也不能说出词里的任何一个字。"
               f"这是第 {g.round} 轮。")

    return hints.suggest_plain(
        who=me.name, situation=sit, log_lines=lines, private=private,
    )


@app.post("/api/g/{game_id}/act")
def api_act_any(game_id: str, action: dict) -> dict:
    try:
        g = games.load(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        out = g.act(action)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        # 传了个不存在的 id（场景、人物、物证…）。这是客户端的问题，
        # 该给 400 而不是 500——探案里 case_models 的 scene()/person()/ev()
        # 查不到就抛 KeyError，一路冒到这儿就是一个 500 加一屏红字。
        # 正常点界面点不出来，但换了案子的旧页面、或者存档读回来之后
        # 手里还攥着上一个案子的 id，就会撞上。
        raise HTTPException(400, f"找不到这个东西：{exc}") from exc
    g.save()
    out["state"] = g.player_state()
    return out


class GenScriptReq(BaseModel):
    theme: str
    suspects: int = 4
    npc: int = 1
    acts: int = 2
    difficulty: str = "medium"
    title: str = ""
    with_script: bool = True
    with_case: bool = False


@app.post("/api/gen/script")
def api_gen_script(req: GenScriptReq) -> dict:
    """用大模型现场生成一个本子。后台跑，前端轮询 /api/gen/script/{job_id}。"""
    try:
        jid = scriptgen.start_job(req.model_dump())
    except scriptgen.GenError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": jid, "offline": SETTINGS.mock}


@app.get("/api/gen/script/{job_id}")
def api_gen_script_state(job_id: str) -> dict:
    st = scriptgen.job_state(job_id)
    if st is None:
        raise HTTPException(404, "没有这个生成任务（服务重启后任务会丢）")
    return st


@app.get("/api/scripts")
def api_scripts() -> list[dict]:
    return list_scripts()


@app.get("/api/scripts/errors")
def api_script_errors() -> dict:
    """上架失败的本子和原因。开放投稿后库里难免混进格式不对的文件，
    一个坏文件不会拖垮选本页，但作者得有地方看到自己的本子为什么没出现。"""
    list_scripts()
    from . import library
    return dict(library.LOAD_ERRORS)


@app.get("/api/scripts/{script_id}")
def api_script(script_id: str) -> dict:
    try:
        s = load_script(script_id)
    except ScriptError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "script_id": s.script_id,
        "title": s.meta.title,
        "tagline": s.meta.tagline,
        "difficulty": s.meta.difficulty,
        "estimated_minutes": s.meta.estimated_minutes,
        "background": s.setting.public_background,
        "map": s.setting.map,
        "time_slots": s.setting.time_slots,
        "acts": s.meta.acts,
        "tags": s.meta.tags,
        "author": s.meta.author,
        "roles": [
            {"id": c.id, "name": c.name, "public_role": c.public_role, "is_npc": c.is_npc}
            for c in s.characters
        ],
    }


@app.post("/api/game")
def api_new_game(req: NewGameReq) -> dict:
    try:
        g = engine.new_game(req.script_id, req.player_char_id)
    except (ScriptError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    g.step()  # 立刻放出开场旁白
    g.save()
    return game_state(g, include_brief=True)


@app.get("/api/game/{game_id}")
def api_get_game(game_id: str) -> dict:
    try:
        g = engine.get_game(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return game_state(g, include_brief=True)


@app.post("/api/game/{game_id}/step")
def api_step(game_id: str) -> dict:
    try:
        g = engine.get_game(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        out = g.step()
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    g.save()
    out["state"] = game_state(g)
    return out


@app.post("/api/game/{game_id}/act")
def api_act(game_id: str, action: dict) -> dict:
    try:
        g = engine.get_game(game_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        out = g.act(action)
    except llm.LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    g.save()
    out["state"] = game_state(g)
    return out


# --------------------------------------------------------------------------
# 静态前端
# --------------------------------------------------------------------------

if WEB_DIR.exists():

    @app.middleware("http")
    async def _no_stale_frontend(request: Request, call_next):
        """前端文件一律走「每次回源问一句」。

        index.html 里的 <link>/<script> 没有版本号，浏览器默认会按启发式
        缓存它们。结果就是：改完 app.js / style.css 刷新页面，看到的还是旧的，
        非得 Ctrl+F5 才行——自己调的时候被这个坑了一次，用户更不该踩。

        no-cache 不等于不缓存：浏览器仍然存着，只是每次带 ETag 回来问一句
        「变了吗」，没变就是一个 304，几乎不花流量。
        """
        resp = await call_next(request)
        p = request.url.path
        if not p.startswith("/api/") and p.rsplit(".", 1)[-1] in (
            "js", "css", "html", "json", "mjs",
        ):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    # 前端是 ES module（app.js 里 import bgm.js），浏览器解析 import 时会按
    # 根路径去取兄弟模块，所以这些文件必须在 / 下也能拿到，不能只挂 /static。
    # 这个 mount 必须放在所有 /api 路由**之后**注册，否则会把它们全接管掉。
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
