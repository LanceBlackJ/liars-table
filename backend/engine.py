"""游戏流程状态机。

刻意全部用纯代码：谁该发言、票数够不够、线索归谁、胜负如何判定——
这些必须 100% 确定，不能交给 LLM。LLM 只负责"说什么"，不负责"轮到谁"。

驱动方式：前端反复调 step()，每次推进一个原子动作（一个 AI 说一句话 / 搜一次证）。
轮到玩家时 step() 返回 waiting_for_player，前端调 act() 提交玩家动作。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import agents
from .config import GAMES_DIR
from .library import build_view, load_script
from .models import Script

SEARCH_BUDGET = {"search_1": 2, "search_2": 1, "search_3": 1}
PLAYER_QUESTIONS = 2
AI_QUESTIONS = 2

# 私聊阶段：玩家能单独拉几个人说话，AI 之间也会私下碰头几次。
# 次数刻意给得紧——密谈的价值来自「只能挑一个人问」，给多了就退化成群聊。
PLAYER_PRIVATE_TALKS = 2
AI_PRIVATE_PAIRS = 2
_PAIR = "*pair*"   # queue 里的哨兵：一对 AI 私下碰头


class Phase(str, Enum):
    PROLOGUE = "prologue"
    READING = "reading"
    INTRO = "intro"
    SEARCH_1 = "search_1"
    ROUND_TABLE_1 = "round_table_1"
    INTERROGATE_1 = "interrogate_1"
    SEARCH_2 = "search_2"
    ROUND_TABLE_2 = "round_table_2"
    PRIVATE_1 = "private_1"
    INTERLUDE = "interlude"
    INTERROGATE_2 = "interrogate_2"
    PRIVATE_2 = "private_2"
    FINAL_WORDS = "final_words"
    VOTE = "vote"
    REVEAL = "reveal"
    DONE = "done"
    # ↓↓ 三幕本新增。只许往后追加，不许改名、不许重排 ↓↓
    SEARCH_3 = "search_3"
    ROUND_TABLE_3 = "round_table_3"
    INTERROGATE_3 = "interrogate_3"
    PRIVATE_3 = "private_3"
    INTERLUDE_2 = "interlude_2"
    DEDUCTION = "deduction"


PHASE_ORDER = [
    Phase.PROLOGUE,
    Phase.READING,
    Phase.INTRO,
    Phase.SEARCH_1,
    Phase.ROUND_TABLE_1,
    Phase.INTERROGATE_1,
    Phase.PRIVATE_1,
    Phase.INTERLUDE,
    Phase.SEARCH_2,
    Phase.ROUND_TABLE_2,
    Phase.INTERROGATE_2,
    Phase.PRIVATE_2,
    Phase.FINAL_WORDS,
    Phase.VOTE,
    Phase.REVEAL,
    Phase.DONE,
]

PHASE_LABEL = {
    Phase.PROLOGUE: "开场",
    Phase.READING: "读本",
    Phase.INTRO: "自我介绍",
    Phase.SEARCH_1: "第一轮搜证",
    Phase.ROUND_TABLE_1: "第一轮圆桌",
    Phase.INTERROGATE_1: "自由质询",
    Phase.SEARCH_2: "第二轮搜证",
    Phase.ROUND_TABLE_2: "第二轮圆桌",
    Phase.PRIVATE_1: "私下密谈",
    Phase.INTERLUDE: "幕间",
    Phase.INTERROGATE_2: "最后质询",
    Phase.PRIVATE_2: "最后密谈",
    Phase.FINAL_WORDS: "最终陈述",
    Phase.VOTE: "投票",
    Phase.REVEAL: "复盘",
    Phase.DONE: "结束",
    Phase.SEARCH_3: "第三轮搜证",
    Phase.ROUND_TABLE_3: "第三轮圆桌",
    Phase.INTERROGATE_3: "终局质询",
    Phase.PRIVATE_3: "终局密谈",
    Phase.INTERLUDE_2: "第三幕",
    Phase.DEDUCTION: "推理答卷",
}

# 阶段归类。以前这些都是散落在六个地方的硬编码元组，加一个 SEARCH_3
# 就要改六处，漏一处的后果是 _expected() 返回 None、搜证面板不出现、整局死锁。
# 所有「这是不是搜证/质询/私聊阶段」的判断都走这几个常量。
SEARCH_PHASES = (Phase.SEARCH_1, Phase.SEARCH_2, Phase.SEARCH_3)
INTERROGATE_PHASES = (Phase.INTERROGATE_1, Phase.INTERROGATE_2, Phase.INTERROGATE_3)
PRIVATE_PHASES = (Phase.PRIVATE_1, Phase.PRIVATE_2, Phase.PRIVATE_3)
INTERLUDE_PHASES = (Phase.INTERLUDE, Phase.INTERLUDE_2)

SPEECH_PHASES = {
    Phase.INTRO: "intro",
    Phase.ROUND_TABLE_1: "round_table",
    Phase.ROUND_TABLE_2: "round_table",
    Phase.ROUND_TABLE_3: "round_table",
    Phase.FINAL_WORDS: "final_words",
}


def build_plan(script: Script) -> list[str]:
    """这个本子实际要走的阶段序列（存成字符串，写进存档）。

    为什么要存进 Game 而不是每次现算：存档里的局是「跑到一半」的，
    之后哪怕我们改了阶段顺序，这一局也该按开局时的顺序走完。
    只依赖一个全局常量的话，改一次顺序，所有进行中的存档都会在半路换了流程；
    删一个阶段，`PHASE_ORDER.index(self.phase)` 直接 ValueError，
    存档列表里的局会悄无声息地消失。

    两幕本、没有答卷的本子 → 和原来的 PHASE_ORDER **一模一样**，这是回归底线。
    """
    P = Phase
    plan = [P.PROLOGUE, P.READING, P.INTRO,
            P.SEARCH_1, P.ROUND_TABLE_1, P.INTERROGATE_1, P.PRIVATE_1, P.INTERLUDE,
            P.SEARCH_2, P.ROUND_TABLE_2, P.INTERROGATE_2, P.PRIVATE_2]
    if script.meta.acts >= 3:
        plan += [P.INTERLUDE_2, P.SEARCH_3, P.ROUND_TABLE_3, P.INTERROGATE_3, P.PRIVATE_3]
    plan.append(P.FINAL_WORDS)
    if script.solution.questions:
        plan.append(P.DEDUCTION)
    plan += [P.VOTE, P.REVEAL, P.DONE]
    return [x.value for x in plan]


@dataclass
class Game:
    game_id: str
    script_id: str
    player_char_id: str
    phase: Phase = Phase.PROLOGUE
    seq: int = 0
    public_log: list[dict] = field(default_factory=list)
    private_holdings: dict[str, list[str]] = field(default_factory=dict)
    search_counts: dict[str, int] = field(default_factory=dict)
    queue: list[str] = field(default_factory=list)
    questions_left: dict[str, int] = field(default_factory=dict)
    pending_question: dict | None = None
    votes: dict[str, dict] = field(default_factory=dict)
    extra_locations: list[str] = field(default_factory=list)   # 幕间解锁的新地点
    fabricated: dict | None = None                             # 凶手栽赃的那一条
    # 玩家自己记的时间线笔记，key 是 "角色id|时段"。
    # 八人本五个时段就是四十格，全靠翻聊天流理不清——
    # 这是给真人用的纸和笔，不参与任何判定，也不喂给 AI。
    notes: dict[str, str] = field(default_factory=dict)
    pad: str = ""                              # 通用记事本，随手记
    # 发言/搜证的起始位次，每换一个阶段往后挪一格。必须是正式字段——
    # to_dict 是把 __dict__ 整个倒出去、from_dict 是 cls(**d)，
    # 挂成类属性的话存档一读回来就是 TypeError。
    rotation: int = 0
    # 当前第几幕（不能叫 act——Game 上已经有个 act() 方法，同名会把字段默认值顶掉）。进入 INTERLUDE → 2，INTERLUDE_2 → 3。视角切分靠它决定给多少。
    cur_act: int = 1
    # 开局时算好的阶段序列。空 = 老存档，回退到 PHASE_ORDER。见 build_plan。
    plan: list[str] = field(default_factory=list)
    # 玩家在「推理答卷」里填的答案：{问题id: 选项id}
    truth_answers: dict[str, str] = field(default_factory=dict)
    result: dict | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---------------- 基础设施 ----------------

    @property
    def script(self) -> Script:
        return load_script(self.script_id)

    def _emit(self, kind: str, *, visible_to: list[str] | None = None, **payload) -> dict:
        """写一条事件。

        visible_to=None 表示所有人可见（绝大多数事件）。给了名单就只有名单里的
        角色看得见——私聊靠这个隔离。注意这是**唯一**的可见性收口：
        任何绕过 _log_for 直接读 public_log 的地方都会漏私聊内容。
        """
        self.seq += 1
        event = {"seq": self.seq, "kind": kind, "ts": time.time(), **payload}
        if visible_to is not None:
            event["visible_to"] = list(visible_to)
        self.public_log.append(event)
        return event

    @staticmethod
    def _can_see(event: dict, cid: str) -> bool:
        vt = event.get("visible_to")
        return vt is None or cid in vt

    def _log_for(self, cid: str) -> list[dict]:
        """某个角色视角下的日志。喂给 AI、发给前端都必须过这一层。"""
        return [e for e in self.public_log if self._can_see(e, cid)]

    def log_for_player(self) -> list[dict]:
        return self._log_for(self.player_char_id)

    # 每换一个阶段，起始位置往后挪一格。
    #
    # 原来这里是死顺序，从头到尾都按剧本里角色列出来的次序走。
    # 后果是玩家选了名单最后那个角色，就**整局都排在最后**：
    # 每轮发言最后一个说，每轮搜证最后一个搜——等轮到你，地点早被翻遍了，
    # 一句「已经被翻遍了，一无所获」，两次搜证白费一次。
    # 这不是策略差异，是选角色时就定死的先后亏，玩家还看不出来。
    def _char_order(self) -> list[str]:
        ids = [c.id for c in self.script.characters]
        if not ids:
            return ids
        k = self.rotation % len(ids)
        return ids[k:] + ids[:k]

    def _suspects(self) -> list[str]:
        """会被投票、要交个人目标、参与推理博弈的那些人——NPC 不在其中。

        NPC（证人/工作人员）只用来被问话，不competed for输赢，
        所以搜证、圆桌发言、最终陈述、投票、私聊结盟，一概不该轮到他们。
        `_char_order()` 仍然对全体角色轮转（保证公平），这里只是按需过滤。
        """
        npc = {c.id for c in self.script.characters if c.is_npc}
        return [cid for cid in self._char_order() if cid not in npc]

    def _ai_ids(self) -> list[str]:
        """会主动行动的 AI——同样不含 NPC。"""
        return [c for c in self._suspects() if c != self.player_char_id]

    def _name(self, cid: str) -> str:
        return self.script.char(cid).name

    def _view(self, cid: str):
        """某角色**此刻**能看到的视角——所有「给谁看什么」都从这一个口子出去。

        以前引擎里散着六七处 `build_view(self.script, cid)`，现在要带上当前幕，
        漏改一处就是「这个人的视角停在第一幕」或者更糟——开到不该开的幕。
        收口成一处。
        """
        return build_view(self.script, cid, act=self.cur_act)

    def _search_n(self, phase: Phase) -> int:
        """这一幕每人几次搜证。剧本可以自己指定（厚本线索多，需要拉高）。"""
        idx = {"search_1": 0, "search_2": 1, "search_3": 2}[phase.value]
        budget = self.script.meta.search_budget
        if idx < len(budget):
            return budget[idx]
        return SEARCH_BUDGET.get(phase.value, 1)

    def _grant(self, cid: str, clue_id: str) -> bool:
        """把一条线索交到某人手里。返回 True 表示他之前没有、这次是新拿到的。

        原来四个地方各写各的：搜证（`_do_search`）**不去重**，
        幕间公开、私聊出示、私聊回赠三处**去重**。
        后果是同一条线索既被幕间公开又被自己搜到时，会在他手里出现两次——
        实测 sc_dog 一局下来「线索持有 18 条 / 全本只有 13 条」。
        重复的那几条会在右栏列两遍，也会被重复喂进 AI 上下文。
        收口成一个函数，顺便为后面的线索合成留一个统一的触发点。
        """
        held = self.private_holdings.setdefault(cid, [])
        if clue_id in held:
            return False
        held.append(clue_id)
        self._synthesize(cid)
        return True

    def _synthesize(self, cid: str) -> None:
        """谁同时握有一条合成线索的全部 from_clues，谁就自动拼出它。

        **按人算，不是全局**：A 手里有一半、B 手里有另一半，谁都拼不出来，
        得在私聊里交换。这是整个合成机制最有意思的地方，而且不花一次 LLM 调用。
        合成产物还能再当别的合成线索的原料，所以循环到不再有新的为止。
        """
        s = self.script
        held = self.private_holdings.setdefault(cid, [])
        changed = True
        while changed:
            changed = False
            for k in s.clues:
                if not k.synth or k.id in held:
                    continue
                if all(f in held for f in k.from_clues):
                    held.append(k.id)
                    changed = True
                    # 只有拼出来的这个人看得见这条——对面的人不知道你刚发现了什么
                    self._emit(
                        "deduce", visible_to=[cid], clue_id=k.id,
                        text=k.combine_note, clue_text=k.text,
                        parents=[s.clue(f).text for f in k.from_clues],
                    )

    def _my_clues(self, cid: str) -> list[dict]:
        s = self.script
        return [
            {"id": k,
             "location": s.clue(k).location or ("推论" if s.clue(k).synth else "随身"),
             "text": s.clue(k).text, "synth": s.clue(k).synth}
            for k in self.private_holdings.get(cid, [])
        ]

    # ---------------- 阶段推进 ----------------

    def _enter(self, phase: Phase) -> None:
        self.phase = phase
        self.pending_question = None
        self.rotation += 1           # 换阶段就轮一格，别让谁一直吃头位/末位
        order = self._char_order()      # 全体，含 NPC
        suspects = self._suspects()     # 不含 NPC——搜证/圆桌/投票/私聊都该用这个

        if phase in SPEECH_PHASES:
            # 自我介绍 NPC 也要露面（管家、法医报个身份很自然）；
            # 圆桌讨论和最终陈述是嫌疑人之间的推理博弈，NPC 不该在场发言，
            # 他们的信息要靠玩家主动去问（INTERROGATE 阶段）。
            self.queue = list(order) if phase == Phase.INTRO else list(suspects)
            self._emit("system", text=f"—— {PHASE_LABEL[phase]} ——")
        elif phase in SEARCH_PHASES:
            n = self._search_n(phase)
            # 一圈一圈发，而不是让每个人连着把 n 次搜完。
            # 连着搜的话，排在前面的人一口气把两个好地方都翻了，
            # 后面的人两次全撞在「已经被翻遍了」上。
            self.queue = [cid for _ in range(n) for cid in suspects]
            self._emit("system", text=f"—— {PHASE_LABEL[phase]}（每人 {n} 次）——")
        elif phase in INTERROGATE_PHASES:
            self.queue = []
            self.questions_left = {self.player_char_id: PLAYER_QUESTIONS}
            # 优先让凶手有机会主动引导方向——这是体验的关键
            ais = sorted(
                self._ai_ids(), key=lambda c: 0 if c == self.script.murderer_id else 1
            )
            self.queue = [self.player_char_id] + ais[:AI_QUESTIONS]
            self._emit(
                "system",
                text=f"—— {PHASE_LABEL[phase]}（你可提问 {PLAYER_QUESTIONS} 次）——",
            )
        elif phase in PRIVATE_PHASES:
            # 玩家先谈（可以谈满也可以随时跳过），然后 AI 之间私下碰头。
            # 哨兵 _PAIR 代表"一对 AI 私聊"，由 _run_ai 展开成具体两个人。
            self.queue = [self.player_char_id] * PLAYER_PRIVATE_TALKS
            self.queue += [_PAIR] * AI_PRIVATE_PAIRS
            self._emit(
                "system",
                text=f"—— {PHASE_LABEL[phase]}（你可以单独找 {PLAYER_PRIVATE_TALKS} 个人说话）——",
            )
        elif phase in INTERLUDE_PHASES:
            self.queue = []
            # 进入第 2 / 3 幕：先把这一幕的追加剧本发下去，再放突发事件
            self.cur_act = 2 if phase == Phase.INTERLUDE else 3
            self._open_act()
            self._fire_interlude()
        elif phase == Phase.DEDUCTION:
            # 答卷只给玩家。AI 不答——它们的输赢账只看票型和个人目标。
            # 玩家本人是凶手时也不答：他知道真相，考他等于白给分。
            me = self.script.char(self.player_char_id)
            self.queue = [] if me.is_murderer else [self.player_char_id]
            self._emit("system", text="—— 推理答卷 ——")
        elif phase == Phase.VOTE:
            self.queue = list(suspects)
            self._emit("system", text="—— 投票 ——")
        elif phase == Phase.REVEAL:
            self.queue = []
            self._settle()
        else:
            self.queue = []

    def _advance_phase(self) -> None:
        # 老存档没有 plan（空列表），回退到全局的两幕顺序——和改造前一字不差。
        order = self.plan or [x.value for x in PHASE_ORDER]
        idx = order.index(self.phase.value)
        if idx + 1 < len(order):
            self._enter(Phase(order[idx + 1]))

    # ---------------- 单步驱动 ----------------

    def step(self) -> dict:
        """推进一个原子动作。返回 {"waiting": bool, "action": str|None, "events": [...]}"""
        before = self.seq

        if self.phase == Phase.PROLOGUE:
            s = self.script
            self._emit("narration", text=s.setting.public_background)
            self._emit(
                "narration",
                text=(
                    f"{s.case.body_found_slot}，{self._name(s.case.body_found_by)}"
                    f"在{s.case.death_location}发现了{s.case.victim}的尸体。"
                    f"死因是{s.case.cause}。凶器是{s.case.weapon}。"
                    f"门窗俱锁，凶手就在你们{len(s.characters)}人之中。"
                ),
            )
            self._enter(Phase.READING)
            return self._resp(before, waiting=True, action="continue")

        if self.phase == Phase.READING:
            return self._resp(before, waiting=True, action="continue")

        if self.phase == Phase.DONE:
            return self._resp(before, waiting=False, action=None)

        if not self.queue:
            self._advance_phase()
            if self.phase == Phase.DONE:
                return self._resp(before, waiting=False, action=None)
            return self._resp(before, waiting=self._is_player_turn(), action=self._expected())

        if self._is_player_turn():
            return self._resp(before, waiting=True, action=self._expected())

        # 先取不出队，跑成功了才真正消费掉这一手。
        # 否则 LLM 一失败（断网/超时），这个角色就被永久跳过——
        # 玩家会看到某个人整局一句话都没说，而且完全不知道为什么。
        cid = self.queue[0]
        self._run_ai(cid)
        if self.queue and self.queue[0] == cid:
            self.queue.pop(0)
        return self._resp(before, waiting=self._is_player_turn(), action=self._expected())

    def _is_player_turn(self) -> bool:
        # READING 要等玩家读完剧本点继续；DONE 之后不再需要任何输入
        if self.phase == Phase.READING:
            return True
        if self.phase == Phase.DONE:
            return False
        if self.pending_question:
            return self.pending_question["target_id"] == self.player_char_id
        return bool(self.queue) and self.queue[0] == self.player_char_id

    def _expected(self) -> str | None:
        if self.phase == Phase.READING:
            return "continue"
        if self.phase == Phase.DONE:
            return None
        if self.pending_question and self.pending_question["target_id"] == self.player_char_id:
            return "answer"
        if not self.queue or self.queue[0] != self.player_char_id:
            return None
        if self.phase in SPEECH_PHASES:
            return "speak"
        if self.phase in SEARCH_PHASES:
            return "search"
        if self.phase in INTERROGATE_PHASES:
            return "ask"
        if self.phase in PRIVATE_PHASES:
            return "private"
        if self.phase == Phase.DEDUCTION:
            return "deduce"
        if self.phase == Phase.VOTE:
            return "vote"
        return None

    def _resp(self, before: int, *, waiting: bool, action: str | None) -> dict:
        return {
            "waiting": waiting,
            "action": action,
            "events": [e for e in self.log_for_player() if e["seq"] > before],
            "phase": self.phase.value,
        }

    # ---------------- AI 行动 ----------------

    def _run_ai(self, cid: str) -> None:
        if cid == _PAIR:
            self._ai_pair_talk()
            return

        s = self.script
        view = self._view(cid)
        my_clues = self._my_clues(cid)

        if self.phase in SPEECH_PHASES:
            out = agents.act(view, SPEECH_PHASES[self.phase], self._log_for(cid), my_clues)
            self._say(cid, out)

        elif self.phase in SEARCH_PHASES:
            if self._should_fabricate(cid):
                if self._fabricate(cid):
                    return
            loc = agents.choose_search(view, self._log_for(cid), my_clues, self.searchable_map())
            self._do_search(cid, loc, announce=True)

        elif self.phase in INTERROGATE_PHASES:
            targets = [
                {"id": c.id, "name": c.name} for c in s.characters if c.id != cid
            ]
            q = agents.choose_question(view, self._log_for(cid), my_clues, targets)
            self._ask(cid, q["target_id"], q["question"])

        elif self.phase == Phase.VOTE:
            # NPC 不能被投——只从嫌疑人里选，否则 AI 会把票投给一个
            # 根本不在投票队列里的证人，写进 votes 却永远不会被结算。
            targets = [
                {"id": c.id, "name": c.name}
                for c in s.characters if c.id != cid and not c.is_npc
            ]
            v = agents.choose_vote(view, self._log_for(cid), my_clues, targets)
            self.votes[cid] = v
            self._emit(
                "vote",
                speaker_id=cid,
                speaker_name=self._name(cid),
                target_name=self._name(v["target_id"]),
                text=v.get("reason", ""),
            )

    # ---------------- 幕间 ----------------

    _ACT_NAME = {2: "第二幕", 3: "第三幕"}

    def _open_act(self) -> None:
        """进入新的一幕：公开宣布，并把每个人**各自**的追加剧本私下发下去。

        追加剧本走 `visible_to=[cid]` 的 `act_open` 事件——
        玩家在日志里看到自己那一页，同时 `_view()` 之后的视角里也带上了它，
        AI 的系统提示因此自动更新。别人的那一页，谁也看不见。
        """
        s = self.script
        name = self._ACT_NAME.get(self.cur_act, f"第{self.cur_act}幕")
        self._emit("system", text=f"══ {name} ══")
        for c in s.characters:
            for b in c.acts:
                if b.act == self.cur_act:
                    self._emit(
                        "act_open", visible_to=[c.id], act=self.cur_act,
                        title=b.title or f"{name}·你的剧本多了一页", text=b.text,
                    )
                    # 随身线索：这一页剧本里提到的东西，此刻真的塞进他手里（也能参与合成）
                    for gk in b.grant_clues:
                        self._grant(c.id, gk)

    def _fire_interlude(self) -> None:
        """每一幕开场砸下来的突发事件。

        存在的理由只有一个：让玩家刚刚建立起来的结论**站不住**。
        没有它，整局就是搜证→发言→投票一条直线，第一轮想通了后面就没悬念。

        剧本里的幕间用 `at` 标明属于哪一幕（act2 / act3）。同一幕可以写多条，
        依次放出。第二幕没写的话会临时造一条兜底；第三幕没写就什么都不发生。
        """
        s = self.script
        key = "act2" if self.phase == Phase.INTERLUDE else "act3"
        mine = [x for x in s.interludes if x.at == key]

        if not mine:
            if key != "act2":
                return
            # 剧本没写幕间就临时造一个：把还没人搜到的最难那条线索直接摆出来。
            # 比什么都不发生强——至少能推翻一部分既有判断。
            found = {k for ks in self.private_holdings.values() for k in ks}
            rest = sorted(
                (c for c in s.clues
                 if c.id not in found and not c.is_red_herring
                 and not c.synth and not c.granted),
                key=lambda c: -c.difficulty,
            )
            if not rest:
                return
            c = rest[0]
            self._emit("interlude", title="新的发现",
                       text=f"就在众人各自散开的时候，{c.location}又翻出了一样东西。")
            self._emit("clue_revealed", speaker_id="", speaker_name="（幕间）",
                       clue_id=c.id, text=c.text, location=c.location)
            for cid in self._char_order():
                self._grant(cid, c.id)
            return

        for it in mine:
            self._emit("interlude", title=it.title, text=it.text)

            if it.effect == "reveal_clue" and it.clue_id:
                c = s.clue(it.clue_id)
                self._emit("clue_revealed", speaker_id="", speaker_name="（幕间）",
                           clue_id=c.id, text=c.text, location=c.location or "通报")
                # 幕间公开的线索，所有人从此都持有——大家是同时听见的
                for cid in self._char_order():
                    self._grant(cid, c.id)

            elif it.effect == "unlock_location" and it.location:
                if it.location not in self.extra_locations:
                    self.extra_locations.append(it.location)
                self._emit("system", text=f"　现在可以搜「{it.location}」了。")

    def question_options(self, q) -> list[dict]:
        """一道题实际能选什么。character / location 型不写 options 时按全局名单推。"""
        s = self.script
        if q.kind == "character":
            if q.options:
                return [{"id": o.id, "text": self._name(o.id)} for o in q.options]
            return [{"id": c.id, "text": c.name} for c in s.characters if not c.is_npc]
        if q.kind == "location":
            if q.options:
                return [{"id": o.id, "text": o.text or o.id} for o in q.options]
            return [{"id": m, "text": m} for m in s.setting.map]
        return [{"id": o.id, "text": o.text} for o in q.options]

    def searchable_map(self) -> list[str]:
        return list(self.script.setting.map) + list(self.extra_locations)

    # ---------------- 私聊 ----------------

    def _private_exchange(self, a: str, b: str, opening: dict, shown: list[dict]) -> None:
        """把一次一对一的往返记进日志。

        三条事件：
          · 公开的「谁和谁走开说话了」——所有人都看得见两个人凑一块儿，
            但听不见内容。这份「知道有事发生却不知道是什么」正是私聊的价值。
          · a 说的话、b 的回应——只有 a、b 看得见。
        """
        pair = [a, b]
        self._emit(
            "private_meta",
            speaker_id=a, speaker_name=self._name(a),
            target_id=b, target_name=self._name(b),
            text=f"{self._name(a)}把{self._name(b)}拉到一边，两个人低声说了几句。",
        )
        self._emit(
            "private", visible_to=pair,
            speaker_id=a, speaker_name=self._name(a),
            target_id=b, target_name=self._name(b),
            text=opening["speech"],
            shown_clues=[c["id"] for c in shown],
        )
        for c in shown:
            # 私下出示的线索，对方从此也持有——情报可以交易
            self._grant(b, c["id"])

        reply = agents.private_reply(
            self._view(b), self._name(a), opening["speech"],
            self._log_for(b), self._my_clues(b), shown,
        )
        self._emit(
            "private", visible_to=pair,
            speaker_id=b, speaker_name=self._name(b),
            target_id=a, target_name=self._name(a),
            text=reply["speech"],
        )
        # AI 也可以私下亮线索作为交换
        for kid in reply.get("reveal_clues", []):
            if kid in self.private_holdings.get(b, []):
                self._grant(a, kid)
                self._emit(
                    "clue_revealed", visible_to=pair,
                    speaker_id=b, speaker_name=self._name(b),
                    clue_id=kid, text=self.script.clue(kid).text,
                    location=self.script.clue(kid).location,
                )

    def _ai_pair_talk(self) -> None:
        """两个 AI 私下碰头。凶手优先——他最有动机私下拉人。"""
        ais = self._ai_ids()
        if len(ais) < 2:
            return
        murderer = self.script.murderer_id
        # 已经私聊过的组合尽量不重复，免得同样两个人反复咬耳朵
        talked = {
            frozenset((e["speaker_id"], e["target_id"]))
            for e in self.public_log if e["kind"] == "private_meta"
        }
        pairs = [
            (a, b) for i, a in enumerate(ais) for b in ais[i + 1:]
            if frozenset((a, b)) not in talked
        ]
        if not pairs:
            return
        pairs.sort(key=lambda ab: 0 if murderer in ab and murderer != self.player_char_id else 1)
        a, b = pairs[0]
        opening = agents.private_open(
            self._view(a), self._name(b), self._log_for(a), self._my_clues(a)
        )
        self._private_exchange(a, b, opening, shown=[])

    # ---------------- 共用动作 ----------------

    def _say(self, cid: str, out: dict) -> None:
        self._emit("speech", speaker_id=cid, speaker_name=self._name(cid), text=out["speech"])
        for kid in out.get("reveal_clues", []):
            if kid in self.private_holdings.get(cid, []):
                self._emit(
                    "clue_revealed",
                    speaker_id=cid,
                    speaker_name=self._name(cid),
                    clue_id=kid,
                    text=self.script.clue(kid).text,
                    location=self.script.clue(kid).location,
                )

    def _should_fabricate(self, cid: str) -> bool:
        """只有 AI 凶手、只在第二轮搜证、只来一次。

        放在第二轮是刻意的：第一轮先让大家把真线索铺开、把怀疑方向立起来，
        这时候插一条假的才有杀伤力。第一轮就造假，无根无据反而显眼。
        """
        return (
            self.phase == Phase.SEARCH_2
            and cid == self.script.murderer_id
            and cid != self.player_char_id
            and not self.fabricated
        )

    def _fabricate(self, cid: str) -> bool:
        s = self.script
        targets = [
            {"id": c.id, "name": c.name} for c in s.characters if c.id != cid
        ]
        out = agents.fabricate(
            self._view(cid), self._log_for(cid), self._my_clues(cid),
            targets, self.searchable_map(),
        )
        if not out:
            return False

        self._emit("system", text=f"{self._name(cid)} 搜查了{out['location']}。", speaker_id=cid)
        # 对所有人（包括玩家）来说，这条和真线索长得一模一样。
        # fabricated 标记只进存档，复盘时才亮出来——这就是这一手的全部意义。
        self._emit(
            "clue_revealed",
            speaker_id=cid, speaker_name=self._name(cid),
            clue_id="", text=out["text"], location=out["location"],
        )
        self.fabricated = {
            "by": cid, "by_name": self._name(cid),
            "location": out["location"], "text": out["text"], "frame": out.get("frame", ""),
        }
        return True

    def _do_search(self, cid: str, location: str, *, announce: bool) -> dict | None:
        """同一地点第 N 次被搜返回第 N 条线索——确定性，不随机。"""
        s = self.script
        n = self.search_counts.get(location, 0)
        pool = s.clues_at(location)
        self.search_counts[location] = n + 1

        if announce:
            self._emit(
                "system", text=f"{self._name(cid)} 搜查了{location}。", speaker_id=cid
            )

        if n >= len(pool):
            if cid == self.player_char_id:
                self._emit("system", text=f"{location}已经被翻遍了，一无所获。")
            return None

        clue = pool[n]
        self._grant(cid, clue.id)
        return {"id": clue.id, "location": clue.location, "text": clue.text}

    def _ask(self, asker: str, target: str, question: str) -> None:
        self._emit(
            "speech",
            speaker_id=asker,
            speaker_name=self._name(asker),
            text=f"（问{self._name(target)}）{question}",
        )
        if target == self.player_char_id:
            self.pending_question = {
                "asker_id": asker,
                "asker_name": self._name(asker),
                "target_id": target,
                "question": question,
            }
        else:
            view = self._view(target)
            out = agents.answer(
                view, question, self._name(asker), self._log_for(target), self._my_clues(target)
            )
            self._say(target, out)

    # ---------------- 玩家动作 ----------------

    def act(self, action: dict) -> dict:
        before = self.seq
        t = action.get("type")
        pid = self.player_char_id

        if t == "continue":
            if self.phase == Phase.READING:
                self._enter(Phase.INTRO)
            return self._resp(before, waiting=self._is_player_turn(), action=self._expected())

        if t == "answer" and self.pending_question:
            self._say(pid, {"speech": action.get("text", ""), "reveal_clues": action.get("reveal_clues", [])})
            self.pending_question = None
            return self._resp(before, waiting=self._is_player_turn(), action=self._expected())

        if not self.queue or self.queue[0] != pid:
            return self._resp(before, waiting=False, action=self._expected())

        if t == "speak":
            self._say(pid, {"speech": action.get("text", ""), "reveal_clues": action.get("reveal_clues", [])})
            self.queue.pop(0)

        elif t == "search":
            loc = action.get("location", "")
            if loc not in self.searchable_map():
                raise ValueError(f"未知地点: {loc}")
            found = self._do_search(pid, loc, announce=True)
            self.queue.pop(0)
            resp = self._resp(before, waiting=self._is_player_turn(), action=self._expected())
            resp["found"] = found
            return resp

        elif t == "ask":
            left = self.questions_left.get(pid, 0)
            if left <= 0 or action.get("skip"):
                self.queue.pop(0)
            else:
                self.questions_left[pid] = left - 1
                self._ask(pid, action["target_id"], action.get("question", ""))
                if self.questions_left[pid] <= 0:
                    self.queue.pop(0)

        elif t == "private":
            if action.get("skip"):
                # 跳过就把剩下的私聊名额全丢掉，直接进下一阶段
                self.queue = [c for c in self.queue if c != pid]
            else:
                tid = action["target_id"]
                if tid == pid or tid not in self._char_order():
                    raise ValueError(f"不能私聊这个对象: {tid}")
                held = set(self.private_holdings.get(pid, []))
                shown = [
                    {"id": k, "text": self.script.clue(k).text,
                     "location": self.script.clue(k).location}
                    for k in action.get("show_clues", []) if k in held
                ]
                self._private_exchange(
                    pid, tid,
                    {"speech": action.get("text", "").strip() or "……你有什么想说的吗？"},
                    shown,
                )
                self.queue.pop(0)

        elif t == "deduce":
            if self.phase != Phase.DEDUCTION:
                raise ValueError("现在不是答卷阶段")
            qs = {q.id: q for q in self.script.solution.questions}
            clean: dict[str, str] = {}
            for qid, val in (action.get("answers") or {}).items():
                if qid not in qs:
                    raise ValueError(f"没有这道题: {qid}")
                if not val:
                    continue                  # 留空 = 不答，按 0 分算，不报错
                if val not in {o["id"] for o in self.question_options(qs[qid])}:
                    raise ValueError(f"题 {qid} 没有这个选项: {val}")
                clean[qid] = val
            self.truth_answers = clean
            self._emit("system", visible_to=[pid], text="你交上了答卷。")
            self.queue.pop(0)

        elif t == "vote":
            tid = action.get("target_id", "")
            # 原来这里完全没校验 target_id——传谁都能记进 votes。
            # 现在有 NPC 之后必须挡住：NPC 不该被投，投给不存在的 id 也不该悄悄收下。
            if tid == pid or tid not in self._suspects():
                raise ValueError(f"不能投这个对象: {tid}")
            self.votes[pid] = {
                "target_id": tid,
                "reason": action.get("reason", ""),
            }
            self._emit(
                "vote",
                speaker_id=pid,
                speaker_name=self._name(pid),
                target_name=self._name(tid),
                text=action.get("reason", ""),
            )
            self.queue.pop(0)

        return self._resp(before, waiting=self._is_player_turn(), action=self._expected())

    # ---------------- 结算 ----------------

    def _score_truth(self, me) -> dict | None:
        """逐题判分。纯代码精确比对，不调 LLM。没有答卷的本子返回 None。

        玩家本人是凶手时不考（他知道真相，考他等于白给分），按及格处理。
        """
        qs = self.script.solution.questions
        if not qs:
            return None
        detail = []
        got = total = 0
        for q in qs:
            opts = {o["id"]: o["text"] for o in self.question_options(q)}
            mine = self.truth_answers.get(q.id, "")
            right = (mine == q.answer)
            total += q.points
            got += q.points if right else 0
            detail.append({
                "id": q.id, "prompt": q.prompt, "points": q.points,
                "yours": opts.get(mine, "（没答）"),
                "correct": opts.get(q.answer, q.answer),
                "right": right, "explain": q.explain,
            })
        if me.is_murderer:
            return {"score": got, "max": total, "detail": detail,
                    "pass": True, "skipped": True}
        need = self.script.solution.question_pass
        return {"score": got, "max": total, "detail": detail,
                "pass": got >= need * total - 1e-9, "skipped": False}

    def _settle(self) -> None:
        s = self.script
        tally: dict[str, int] = {}
        for v in self.votes.values():
            tally[v["target_id"]] = tally.get(v["target_id"], 0) + 1

        top = max(tally.values()) if tally else 0
        elected = [cid for cid, n in tally.items() if n == top]
        caught = len(elected) == 1 and elected[0] == s.murderer_id

        # ---- 个人目标判定 ----
        # 阵营输赢只是一半。个人目标没达成，就算票对了也不算真赢——
        # 这正是剧本杀和"猜凶手小游戏"的区别。
        from . import goals as goals_mod
        from .library import build_view

        me = s.char(self.player_char_id)

        # 每个人都按自己的目标算，不只是玩家。
        #
        # 「阵营赢了」只是一半：凶手逃脱了但秘密被扒光，不算他赢；
        # 好人票对了凶手却把自己的丑事抖了个干净，也不算他赢。
        # 复盘时把八个人各自的账都摊开，才看得出这一局到底谁真的赢了。
        everyone: list[dict] = []
        for c in s.characters:
            if c.is_npc:
                # NPC 不参与这场博弈——没有阵营、没有个人目标，
                # 复盘里跟其他人一起算「谁赢了」没有意义，直接跳过。
                continue
            # 结算按**最终幕**的视角判：目标可能在后面几幕被追加、被退休
            v = build_view(s, c.id, act=s.meta.acts)
            res = goals_mod.judge(v, self._log_for(c.id))
            done = all(g["achieved"] for g in res) if res else True
            fac = (not caught) if c.is_murderer else caught
            everyone.append({
                "id": c.id,
                "name": c.name,
                "is_murderer": c.is_murderer,
                "is_me": c.id == self.player_char_id,
                "faction_win": fac,
                "goals": res,
                "goals_all_done": done,
                "won": fac and done,          # 两样都占才算这个人赢
            })

        mine = next(x for x in everyone if x["is_me"])
        goal_results = mine["goals"]
        goals_all_done = mine["goals_all_done"]

        if me.is_murderer:
            faction_win = not caught          # 凶手：没被票出就算逃脱
            faction_note = "你逃脱了指认。" if faction_win else "你被票出去了。"
        else:
            faction_win = caught
            faction_note = "真凶被票了出来。" if faction_win else "真凶逃脱了。"

        # ---- 分层真相 ----
        # 票对了人只是第一问。「谁先动的手」「他怎么死的」「为什么」各自判分，
        # 低于及格线，说明你是蒙对的，或者只看懂了一半——不算把案子还原了。
        # 没有答卷的老本子 truth_pass 恒为 True，四档判定和改造前逐字一致。
        truth = self._score_truth(me)
        truth_pass = truth["pass"] if truth else True

        human_win = faction_win and goals_all_done and truth_pass
        if human_win:
            verdict = "完全胜利"
        elif faction_win:
            verdict = "惨胜"                  # 人抓对了，但个人目标崩了 / 案情没还原
        elif goals_all_done and truth_pass:
            verdict = "虽败犹荣"
        else:
            verdict = "彻底失败"

        self.result = {
            "human_win": human_win,
            "verdict": verdict,
            "fabricated": self.fabricated,
            "faction_win": faction_win,
            "faction_note": faction_note,
            "player_is_murderer": me.is_murderer,
            "goals": goal_results,
            "goals_all_done": goals_all_done,
            "truth": truth,            # 分层真相的逐题得分；老本子为 None
            "everyone": everyone,      # 全场每个人各自的输赢账
            "note": (
                f"{faction_note}"
                + ("你的个人目标全部达成。" if goals_all_done else "但你的个人目标没有全部达成。")
                + ("" if truth is None else
                   f"答卷 {truth['score']}/{truth['max']} 分"
                   + ("，把案子还原了。" if truth["pass"] else "，案情没有还原。"))
            ),
            "murderer_id": s.murderer_id,
            "murderer_name": self._name(s.murderer_id),
            "tally": [
                {"name": self._name(cid), "votes": n}
                for cid, n in sorted(tally.items(), key=lambda kv: -kv[1])
            ],
            "caught": caught,
            "player_voted": self.votes.get(self.player_char_id, {}).get("target_id"),
            "player_correct": self.votes.get(self.player_char_id, {}).get("target_id")
            == s.murderer_id,
            "reasoning_chain": s.solution.reasoning_chain,
            "timeline": [
                {
                    "slot": t.slot,
                    "name": self._name(t.char),
                    "location": t.location,
                    "action": t.action,
                    "is_crime": t.is_crime_action,
                }
                for t in sorted(
                    s.timeline,
                    key=lambda t: (s.setting.time_slots.index(t.slot), t.char),
                )
            ],
            "secrets": [
                {
                    "name": c.name,
                    "role": c.public_role,
                    "secret": c.private_secret + "".join(
                        f"\n\n【第{b.act}幕】{b.secret_add}" for b in c.acts if b.secret_add),
                    "motive": c.motive,
                    "is_murderer": c.is_murderer,
                    "goals": [g.text for g in c.personal_goals],
                }
                for c in s.characters
            ],
            "missed_clues": [
                {"id": k.id, "location": k.location, "text": k.text}
                for k in s.clues
                if not k.synth and not k.granted
                and not any(k.id in held for held in self.private_holdings.values())
                and not any(
                    e.get("clue_id") == k.id
                    for e in self.public_log
                    if e["kind"] == "clue_revealed"
                )
            ],
            # 没人拼出来的推论——复盘里最值得看的一段：差一步就能想通的东西
            "missed_deductions": [
                {"id": k.id, "text": k.text,
                 "from": [s.clue(f).text for f in k.from_clues]}
                for k in s.clues
                if k.synth
                and not any(k.id in held for held in self.private_holdings.values())
            ],
        }
        self._emit("system", text="—— 真相 ——")
        self.phase = Phase.DONE

    # ---------------- 持久化 ----------------

    def to_dict(self) -> dict[str, Any]:
        self.updated_at = time.time()
        d = {k: v for k, v in self.__dict__.items()}
        d["phase"] = self.phase.value
        # kind 供存档列表分派用；from_dict 会把它剥掉
        d["kind"] = "mystery"
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Game:
        d = dict(d)
        d.pop("kind", None)          # 只用于存档分派，不是字段
        d["phase"] = Phase(d["phase"])
        return cls(**d)

    def save(self) -> None:
        (GAMES_DIR / f"{self.game_id}.json").write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
        )


# --------------------------------------------------------------------------
# 会话仓库
# --------------------------------------------------------------------------

_GAMES: dict[str, Game] = {}


def new_game(script_id: str, player_char_id: str) -> Game:
    script = load_script(script_id)
    if player_char_id not in [c.id for c in script.characters]:
        raise ValueError(f"未知角色 {player_char_id}")
    if script.char(player_char_id).is_npc:
        # NPC 没有个人目标、不投票、不被投票——玩家选了它，
        # 一局下来除了被问话什么都做不了，等于选了个旁观席。
        raise ValueError(f"{player_char_id} 是 NPC（证人），不能扮演")
    g = Game(
        game_id=uuid.uuid4().hex[:12],
        script_id=script_id,
        player_char_id=player_char_id,
        plan=build_plan(script),
    )
    _GAMES[g.game_id] = g
    return g


def get_game(game_id: str) -> Game:
    if game_id in _GAMES:
        return _GAMES[game_id]
    path = GAMES_DIR / f"{game_id}.json"
    if path.exists():
        g = Game.from_dict(json.loads(path.read_text(encoding="utf-8")))
        _GAMES[game_id] = g
        return g
    raise KeyError(f"对局不存在: {game_id}")
