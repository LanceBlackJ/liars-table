"""《探案》引擎。

和另外三款的根本区别：没有回合，没有队列。你想干什么就干什么，
时间不会自己往前走——推动剧情的只有你手里多出来的东西。

信息隔离在这里靠**证词层级**实现：
每个人物有 tier 0/1/2 三份「他愿意说什么」，
喂给模型的**永远只有当前那一层**。
下一层的内容模型自己都没见过，所以玩家再怎么套话也套不出来——
只有把对的证据拍在他面前，代码才会把他推到下一层。

这一点很要紧：如果把全部真相塞进 prompt 再叮嘱「先别说」，
模型迟早会漏；不给它，它就不可能漏。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..case_models import Case
from ..config import DATA_DIR, GAMES_DIR
from ..llm import LLMError, chat, extract_json

CASES_DIR = DATA_DIR / "cases"

_CACHE: dict[str, Case] = {}


def load_case(case_id: str) -> Case:
    if case_id not in _CACHE:
        p = CASES_DIR / f"{case_id}.json"
        if not p.exists():
            raise KeyError(f"找不到案件 {case_id}")
        _CACHE[case_id] = Case(**json.loads(p.read_text(encoding="utf-8")))
    return _CACHE[case_id]


def list_cases() -> list[dict]:
    out = []
    for p in sorted(CASES_DIR.glob("*.json")):
        try:
            c = load_case(p.stem)
        except Exception:
            continue
        out.append({
            "case_id": c.case_id, "title": c.title, "tagline": c.tagline,
            "victim": c.victim,
            "scene_count": len(c.scenes), "people_count": len(c.people),
            "evidence_count": len(c.evidence), "author": c.author,
        })
    return out


class DetectiveGame:
    kind = "detective"
    kind_label = "探案"

    def __init__(self) -> None:
        self.game_id: str = ""
        self.case_id: str = ""
        self.scene: str = ""
        self.evidence: list[str] = []          # 已取得的物证 id，有序
        self.tiers: dict[str, int] = {}        # 人物 → 当前证词层
        self.taken: list[str] = []             # 已拿过的热区 id
        self.met: list[str] = []               # 见过面的人。指认只能从这里挑
        self.found_at: dict[str, str] = {}     # 物证 id → 在哪个场景找到的
        self.derived: list[str] = []           # 靠组合推出来的物证（不是捡到的）
        self.log: list[dict] = []
        self.seq: int = 0
        self.finished: bool = False
        self.result: dict | None = None
        self.pad: str = ""                     # 通用记事本，玩家私有
        self.created_at: float = time.time()
        self.updated_at: float = time.time()

    # ---------------- 构造 ----------------

    @classmethod
    def create(cls, case_id: str = "case_paint", **_) -> DetectiveGame:
        c = load_case(case_id)
        g = cls()
        g.game_id = uuid.uuid4().hex[:12]
        g.case_id = case_id
        g.scene = c.start_scene
        g.tiers = {p.id: 0 for p in c.people}
        g._emit("brief", text=c.brief)
        g._enter_scene(c.start_scene, first=True)
        return g

    @property
    def case(self) -> Case:
        return load_case(self.case_id)

    def _emit(self, kind: str, **payload) -> dict:
        self.seq += 1
        e = {"seq": self.seq, "kind": kind, **payload}
        self.log.append(e)
        return e

    def _enter_scene(self, sid: str, *, first: bool = False) -> None:
        s = self.case.scene(sid)
        self.scene = sid
        self._emit("scene", scene_id=sid, name=s.name, text=s.intro)
        # 进过这个场景就算见过这里的人——指认名单据此生成。
        # 没去过的地方、没碰过面的人，不该出现在指认选项里。
        for pid in s.people:
            if pid not in self.met:
                self.met.append(pid)

    # ---------------- 动作 ----------------

    def step(self) -> dict:
        """探案没有 AI 回合要推——永远在等玩家。留这个方法只为对齐协议。"""
        return {"waiting": True, "action": "explore", "events": []}

    def act(self, action: dict) -> dict:
        before = self.seq
        t = action.get("type")

        if self.finished:
            return self._resp(before)

        if t == "move":
            self._move(action.get("scene_id", ""))
        elif t == "examine":
            self._examine(action.get("hotspot_id", ""))
        elif t == "talk":
            self._talk(action.get("person_id", ""), action.get("text", "").strip())
        elif t == "present":
            self._present(action.get("person_id", ""), action.get("evidence_id", ""))
        elif t == "combine":
            self._combine(action.get("a", ""), action.get("b", ""))
        elif t == "accuse":
            self._accuse(action)
        else:
            raise ValueError(f"未知动作 {t}")

        self.save()
        return self._resp(before)

    def _resp(self, before: int) -> dict:
        return {
            "waiting": True,
            "action": "explore",
            "events": [e for e in self.log if e["seq"] > before],
        }

    # ---- 移动 ----

    def _move(self, sid: str) -> None:
        c = self.case
        s = c.scene(sid)
        if s.requires and s.requires not in self.evidence:
            self._emit("blocked", text=s.locked_hint or "过不去。")
            return
        if sid == self.scene:
            return
        self._enter_scene(sid)

    # ---- 检查热区 ----

    def _examine(self, hid: str) -> None:
        c = self.case
        s = c.scene(self.scene)
        hs = next((h for h in s.hotspots if h.id == hid), None)
        if hs is None:
            raise ValueError(f"这个场景里没有 {hid}")

        if hs.requires and hs.requires not in self.evidence:
            self._emit("examine", label=hs.label, text=hs.locked_text or "打不开。")
            return

        self._emit("examine", label=hs.label, text=hs.describe)

        if hs.yields and hs.yields not in self.evidence:
            if hs.once and hs.id in self.taken:
                return
            self.taken.append(hs.id)
            self.evidence.append(hs.yields)
            self.found_at[hs.yields] = f"{s.name} · {hs.label}"
            ev = c.ev(hs.yields)
            self._emit("gain", evidence_id=ev.id, name=ev.name, detail=ev.detail)
            self._check_combines()

    # ---- 物证组合 ----

    def _check_combines(self) -> None:
        """有些结论要两样东西凑一起才看得出来。凑齐了就自动点破——
        让玩家在证物栏里手动试组合只是徒增摩擦，线索本身已经够难了。"""
        c = self.case
        for e in c.evidence:
            if not e.combines_into or e.combines_into in self.evidence:
                continue
            if e.id in self.evidence and e.combines_with in self.evidence:
                self.evidence.append(e.combines_into)
                self.derived.append(e.combines_into)
                self.found_at[e.combines_into] = (
                    f"由「{c.ev(e.id).name}」和「{c.ev(e.combines_with).name}」对出来的"
                )
                got = c.ev(e.combines_into)
                self._emit("deduce", text=e.combine_note,
                           evidence_id=got.id, name=got.name, detail=got.detail)

    def _combine(self, a: str, b: str) -> None:
        c = self.case
        for e in c.evidence:
            if e.combines_into and {e.id, e.combines_with} == {a, b}:
                if e.combines_into in self.evidence:
                    self._emit("note", text="这两样你已经对过了。")
                    return
                if e.id in self.evidence and e.combines_with in self.evidence:
                    self.evidence.append(e.combines_into)
                    self.derived.append(e.combines_into)
                    self.found_at[e.combines_into] = (
                        f"由「{c.ev(e.id).name}」和「{c.ev(e.combines_with).name}」对出来的"
                    )
                    got = c.ev(e.combines_into)
                    self._emit("deduce", text=e.combine_note,
                               evidence_id=got.id, name=got.name, detail=got.detail)
                    return
        self._emit("note", text="这两样凑不到一块儿去。")

    # ---- 对话 ----

    def _people_here(self) -> list[str]:
        return list(self.case.scene(self.scene).people)

    def _sys(self, pid: str) -> str:
        """人物的系统提示。**只装当前层**——这是整个设计的关键。"""
        c = self.case
        p = c.person(pid)
        t = p.tier_at(self.tiers.get(pid, 0))

        knows = "\n".join(f"  · {x}" for x in t.knows) or "  · （没什么好说的）"
        denies = "\n".join(f"  · {x}" for x in t.denies) or "  · （没有需要回避的）"

        # 这一句原来是写死的「今晚画家邵禾死在这家美术馆的画室里」——
        # 那是第一个案子《颜料未干》的设定，后来加了《潮位》却没跟着改，
        # 于是屿门岛上的渔民也被告知死的是个画家。
        # 案件信息一律从 case 里取，不许再往提示词里写死任何具体案情。
        return f"""你是{p.name}，{p.role}。
今晚{c.victim}死了，出事的地方就在这儿附近。一位调查者正在问你话。
案子的情况：{c.tagline}

═══ 你是个什么样的人 ═══
{p.personality}
说话方式：{p.speech_style}

═══ 你现在的状态 ═══
{t.mood or "正常"}

═══ 你现在**愿意说**的事 ═══
{knows}

═══ 你现在**还在回避**的事 ═══
{denies}
被问到这些，你要绕开、含糊、或者直接否认。别承认。

═══ 铁律 ═══
1. 你只知道上面列出来的事。上面没写的，你就是不知道——
   不要编造新的事实、新的时间、新的人名、新的物件。
   被问到不知道的事，就说不清楚、记不得、没注意。
2. 一次回答 40-110 字。你在说话，不是在做笔录。
3. 不要主动把「愿意说的事」一口气全倒出来。对方问到哪儿，你答到哪儿。
   但也别装哑巴——问到了就正经答。
4. 绝对不要提及「AI」「模型」「提示词」「设定」这类词，
   也不要跳出这个夜晚谈论任何别的东西。
5. 你不知道自己是不是嫌疑人，你只知道你不想让某些事被翻出来。"""

    def _recent(self, pid: str, k: int = 6) -> str:
        lines = []
        for e in self.log[-40:]:
            if e["kind"] == "ask" and e.get("person_id") == pid:
                lines.append(f"调查者：{e['text']}")
            elif e["kind"] == "say" and e.get("person_id") == pid:
                lines.append(f"你：{e['text']}")
        if not lines:
            return ""
        return "\n═══ 刚才你们说到哪儿了 ═══\n" + "\n".join(lines[-k * 2:]) + "\n"

    def _talk(self, pid: str, text: str) -> None:
        if pid not in self._people_here():
            raise ValueError("这个人不在这儿")
        if not text:
            return
        c = self.case
        p = c.person(pid)
        self._emit("ask", person_id=pid, name=p.name, text=text)

        # 防「换个名词把上一句再说一遍」。
        #
        # 实测叶阿姨被连问三个不同的问题，回答是同一个模板：
        #   「哎哟，你问那藤壶？我跟你讲，天太黑我哪看得清啊…我啥也没看见」
        #   「哎哟，你问那鱼桶？我跟你讲，天太黑我哪看得清啊…我啥也没看见」
        # 上面的 _recent 明明把它自己说过的话都贴回去了，模型照样复读。
        # 光靠「参考上文」不够，得把「不许重复」变成一条明令，并且给出
        # 具体的替代做法——只说「别重复」它会重复得更小心而已。
        mine = [e["text"] for e in self.log
                if e["kind"] == "say" and e.get("person_id") == pid]
        no_repeat = ""
        if mine:
            # 光说「别重复」没用，得把它**实际重复过的那几个短语**挑出来点名。
            # 做法很土：在已经说过的话里找长度 ≥5、出现过两次以上的片段。
            # 实测叶阿姨会把「我跟你讲」「反正天都黑透啦」当口头禅无限循环，
            # 挑出来明令禁用之后才真的换说法。
            recent = mine[-4:]
            joined = "".join(recent)
            seen: dict[str, int] = {}
            for txt in recent:
                for n in (5, 6, 7):
                    for i in range(len(txt) - n + 1):
                        frag = txt[i:i + n]
                        if "，" in frag or "。" in frag:
                            continue
                        seen[frag] = joined.count(frag)
            hot = sorted({f for f, c in seen.items() if c >= 2},
                         key=len, reverse=True)[:6]
            banned = "、".join(f"「{f}」" for f in hot)
            no_repeat = (
                f"\n**你已经回答过 {len(mine)} 次了。**\n"
                + (f"你反复在用这几个说法：{banned}。"
                   "**这一次一个都不许再用**，换别的词。\n" if hot else "")
                + "也不要把上面说过的整句话换个语序再讲一遍——"
                "同一套话说三遍，对方立刻知道你在背稿。\n"
                "换一种应对：给一个之前没提过的细节、反问回去、"
                "把话头引到别人身上、或者干脆表现出不耐烦。"
                "你要藏的东西继续藏，但**藏的方式要换**。\n"
            )

        user = (
            self._recent(pid)
            + no_repeat
            + f"\n调查者问你：「{text}」\n\n"
            "按你现在的状态和立场回答。只输出你说的话，不要加引号，不要写动作描述。"
        )
        try:
            raw = chat(
                [{"role": "system", "content": self._sys(pid)},
                 {"role": "user", "content": user}],
                temperature=0.85, max_tokens=500,
            )
            said = _clean(raw)
        except LLMError as exc:
            said = f"（{p.name}走神了一下，没接上话。）"
            self._emit("error", text=str(exc)[:200])
        self._emit("say", person_id=pid, name=p.name, text=said)

    # ---- 摆证据 ----

    def _present(self, pid: str, eid: str) -> None:
        if pid not in self._people_here():
            raise ValueError("这个人不在这儿")
        if eid not in self.evidence:
            raise ValueError("你手里没有这件东西")

        c = self.case
        p = c.person(pid)
        ev = c.ev(eid)
        cur = self.tiers.get(pid, 0)
        self._emit("show", person_id=pid, name=p.name,
                   evidence_id=eid, ev_name=ev.name)

        nxt = next((t for t in p.testimonies if t.tier == cur + 1), None)
        if nxt is not None and nxt.unlocked_by == eid:
            # 撞对了。脚本化的第一反应必须原样出现——
            # 交给模型即兴，最关键的那一下会被写飞。
            self.tiers[pid] = cur + 1
            self._emit("crack", person_id=pid, name=p.name, text=nxt.reaction)
            self._followup(pid, ev)
            return

        # 撞错了：还停在当前层，让他把这东西挡回去
        user = (
            self._recent(pid)
            + f"\n调查者把一样东西摆到你面前：{ev.name}——{ev.detail}\n\n"
            "这东西戳不到你的痛处。按你现在的立场回应它：\n"
            "可以承认这东西存在，可以说这和你没关系，可以反问他想说明什么。\n"
            "但**不要**因此松口任何你正在回避的事。\n"
            "只输出你说的话，40-90 字。"
        )
        try:
            raw = chat(
                [{"role": "system", "content": self._sys(pid)},
                 {"role": "user", "content": user}],
                temperature=0.85, max_tokens=460,
            )
            said = _clean(raw)
        except LLMError:
            said = "（他看了一眼，没接话。）"
        self._emit("say", person_id=pid, name=p.name, text=said)

    def _followup(self, pid: str, ev) -> None:
        """破防之后接着说下去。此时 _sys 已经是新的一层了。"""
        c = self.case
        p = c.person(pid)
        # 必须把证据的**具体内容**也给它。只给名字的话，模型会顺着名字瞎联想——
        # 实测秦棠被「后补的签名」戳穿后，开口谈的却是画室里那幅画。
        user = (
            f"\n调查者把这样东西摆在了你面前：\n"
            f"　　{ev.name}——{ev.detail}\n\n"
            "你已经绷不住了。**针对这件东西**往下说，别扯到别的事情上去。\n"
            "把你现在愿意承认的事说出来一部分，语气接着刚才那句，别重新开场。\n"
            "只输出你说的话，60-140 字。"
        )
        try:
            raw = chat(
                [{"role": "system", "content": self._sys(pid)},
                 {"role": "user", "content": user}],
                temperature=0.85, max_tokens=600,
            )
            said = _clean(raw)
        except LLMError:
            said = "（他停了很久，没有再说下去。）"
        self._emit("say", person_id=pid, name=p.name, text=said)

    # ---- 指认 ----

    def _accuse(self, action: dict) -> None:
        c = self.case
        sol = c.solution
        who = action.get("culprit", "")
        how = str(action.get("method", "")).strip()[:600]
        why = str(action.get("motive", "")).strip()[:600]

        if who not in self.met:
            raise ValueError("你还没见过这个人，没法指认。")

        ok_who = who == sol.culprit
        truth_how = _opt(sol.method_options, sol.method)
        truth_why = _opt(sol.motive_options, sol.motive)

        # 手法和动机是玩家自己写的，只能交给裁判判。
        from ..case_judge import judge as judge_text

        graded = judge_text(
            method_truth=truth_how, method_mine=how,
            motive_truth=truth_why, motive_mine=why,
            context=f"{c.title}：{c.tagline}\n死者：{c.victim}",
        )
        m_score = graded["method"]["score"]
        v_score = graded["motive"]["score"]
        ok_how, ok_why = m_score == 2, v_score == 2
        # 满分 5：人 1 分（对错分明），手法动机各 0-2
        points = (1 if ok_who else 0) + m_score + v_score
        hits = sum((ok_who, ok_how, ok_why))

        if not ok_who:
            verdict, note = "错案", "你指错了人。真凶今晚就这么走出去了。"
        elif points == 5:
            verdict, note = "完美结案", "人、手法、动机，你一样没差。整桩案子被你原样拼了回来。"
        elif points >= 4:
            verdict, note = "结案", "人抓对了，经过也基本还原——差的那点无伤大雅。"
        elif points >= 2:
            verdict, note = "抓对了人", "人是对的，但你对当晚经过的还原还差着一截。"
        else:
            verdict, note = "抓对了人", "人指对了，可手法和动机你都没说到点上——这案子你是蒙对的。"

        self.finished = True
        self.result = {
            "verdict": verdict, "note": note, "hits": hits,
            "points": points, "max_points": 5,
            "culprit_ok": ok_who, "method_ok": ok_how, "motive_ok": ok_why,
            "method_score": m_score, "motive_score": v_score,
            "method_comment": graded["method"]["comment"],
            "motive_comment": graded["motive"]["comment"],
            "your": {
                "culprit": c.person(who).name if who in {p.id for p in c.people} else "？",
                "method": how or "（没写）",
                "motive": why or "（没写）",
            },
            "truth": {
                "culprit": c.person(sol.culprit).name,
                "method": truth_how,
                "motive": truth_why,
            },
            "explanation": sol.explanation,
            "key_evidence": [
                {"id": k, "name": c.ev(k).name, "had": k in self.evidence}
                for k in sol.key_evidence
            ],
            "evidence_found": len(self.evidence),
            "evidence_total": len(c.evidence),
            "tiers": {p.id: {"name": p.name, "tier": self.tiers.get(p.id, 0),
                             "max": max(t.tier for t in p.testimonies)}
                      for p in c.people},
        }
        self._emit("verdict", **self.result)

    # ---------------- 对外状态 ----------------

    def player_state(self) -> dict[str, Any]:
        c = self.case
        s = c.scene(self.scene)
        st: dict[str, Any] = {
            "kind": "detective", "kind_label": "探案",
            "game_id": self.game_id, "case_id": self.case_id,
            "title": c.title, "tagline": c.tagline,
            "phase": "explore", "phase_label": "调查中", "pad": self.pad,
            "waiting": True, "action": "explore",
            "finished": self.finished,
            "scene": {
                "id": s.id, "name": s.name,
                # 没有场景图了，这段「进门第一眼」就是场景本身，
                # 要常驻在左栏，不能只在进门时打一条日志然后滚走。
                "desc": s.intro,
                "hotspots": [
                    {"id": h.id, "label": h.label, "done": h.id in self.taken}
                    for h in s.hotspots
                ],
                "people": [
                    {"id": p, "name": c.person(p).name, "role": c.person(p).role}
                    for p in s.people
                ],
            },
            "scenes": [
                {"id": x.id, "name": x.name,
                 "locked": bool(x.requires) and x.requires not in self.evidence,
                 "here": x.id == self.scene}
                for x in c.scenes
            ],
            # 刻意**不**下发证物总数：显示「3/14」等于告诉玩家还差 11 件、
            # 找满就完事了，把查案变成清单打勾。找到多少是多少。
            "evidence": [
                {"id": e, "name": c.ev(e).name, "detail": c.ev(e).detail,
                 "found_at": self.found_at.get(e, ""),
                 "combined": e in self.derived}
                for e in self.evidence
            ],
            "log": self.log,
            # 只能指认**见过的人**。把全部角色列出来等于泄露了还没见过谁，
            # 而且指认一个素未谋面的名字本身就不成立。
            # 手法和动机不再给选项——四选一本质是猜，改成自己写。
            "accuse": {
                "people": [
                    {"id": i, "name": c.person(i).name, "role": c.person(i).role}
                    for i in self.met
                ],
            },
        }
        if self.finished and self.result:
            st["result"] = self.result
        return st

    def summary(self) -> dict:
        c = self.case
        return {
            "game_id": self.game_id, "kind": "detective", "kind_label": "探案",
            "title": c.title,
            "phase": "已结案" if self.finished else f"调查中 · {c.scene(self.scene).name}",
            "round": 0, "finished": self.finished, "human_name": "侦探",
            "created_at": self.created_at, "updated_at": self.updated_at,
            "progress": 1.0 if self.finished else min(0.95, len(self.evidence) / max(1, len(c.evidence))),
        }

    # ---------------- 存档 ----------------

    def to_dict(self) -> dict:
        self.updated_at = time.time()
        return {
            "kind": "detective", "game_id": self.game_id, "case_id": self.case_id,
            "scene": self.scene, "evidence": self.evidence, "tiers": self.tiers,
            "taken": self.taken, "log": self.log, "seq": self.seq, "pad": self.pad,
            "met": self.met, "found_at": self.found_at, "derived": self.derived,
            "finished": self.finished, "result": self.result,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def load_dict(self, d: dict) -> None:
        for k in ("game_id", "case_id", "scene", "evidence", "tiers", "taken",
                  "log", "seq", "finished", "result", "created_at", "updated_at",
                  "met", "found_at", "derived", "pad"):
            if k in d:
                setattr(self, k, d[k])

    def save(self) -> None:
        GAMES_DIR.mkdir(parents=True, exist_ok=True)
        p: Path = GAMES_DIR / f"{self.game_id}.json"
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def _opt(options, oid: str) -> str:
    for o in options:
        if o.id == oid:
            return o.text
    return "？"


def _clean(raw: str) -> str:
    """模型偶尔会裹一层 JSON 或引号，剥掉。"""
    t = raw.strip()
    if t.startswith("{"):
        try:
            d = extract_json(t)
            for k in ("say", "speech", "text", "reply"):
                if isinstance(d, dict) and d.get(k):
                    t = str(d[k]).strip()
                    break
        except (LLMError, json.JSONDecodeError, TypeError, AttributeError):
            pass
    t = t.strip().strip("「」\"'").strip()
    return t or "（沉默）"
