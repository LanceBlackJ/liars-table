"""狼人杀。6 人局：2 狼人 + 1 预言家 + 1 女巫 + 2 平民。

信息隔离在这个游戏里是分层的，比剧本杀更复杂：
  · 狼人  —— 知道同伴是谁，夜里能看到彼此的商议
  · 预言家 —— 只知道自己验过的人的结果
  · 女巫  —— 知道今晚谁被刀了，有一瓶解药一瓶毒药
  · 平民  —— 什么都不知道
这些都靠 EventLog 的 visible_to 白名单实现：夜聊事件只对狼人可见，
验人结果只对预言家可见。AI 拿到的上下文是 log.visible(自己的 id)，
所以平民 AI 的上下文里根本没有夜晚发生过什么的记录。

胜负：狼人数 ≥ 好人数 → 狼胜；狼全出局 → 好人胜。
"""
from __future__ import annotations

import json
import random
from typing import Any

from ..llm import LLMError, chat, extract_json
from .base import BaseGame, Player, make_roster

ROLE_LABEL = {
    "wolf": "狼人", "seer": "预言家", "witch": "女巫",
    "hunter": "猎人", "guard": "守卫", "knight": "骑士",
    "idiot": "白痴", "villager": "平民",
}
ROLE_DESC = {
    "wolf": "每晚和同伴商议刀一个人。白天要伪装成好人，混过投票。",
    "seer": "每晚可以查验一个人的身份，得知他是好人还是狼人。",
    "witch": "有一瓶解药一瓶毒药，各只能用一次。夜里会得知谁被刀了。",
    "hunter": "出局时可以开枪带走一个人。被女巫毒死则无法开枪。",
    "guard": "每晚守护一个人，被守的人当晚免于狼刀。"
             "**不能连续两晚守同一个人**，也不能守自己。"
             "注意「同守同救」：如果你守的人当晚又被女巫用了解药，他反而会死。\n"
             "白天要不要报出自己守了谁，你自己权衡——"
             "报了等于告诉狼「我是守卫」，今晚八成就轮到你被刀；"
             "但守下了一刀的信息，有时候确实能救场。**不要第一天就随手报**。",
    "knight": "白天发言阶段可以**翻牌决斗**一个人：如果他是狼人，他当场出局，"
              "当天直接天黑；如果他是好人，你自己出局。一局只能用一次。\n"
              "开牌当天不再投票，所以这一手等于用掉全场一次放逐机会。"
              "没有实锤就留着，别拿它去赌。",
    "idiot": "被投票放逐时**翻牌免死**——你不会出局，但从此**失去投票权**，"
             "而且所有人都知道了你的身份。夜里被刀照样会死。\n"
             "**别主动说自己是白痴**：说了就等于告诉狼「这个人票不死也没票权」，"
             "白白浪费一张能吸走一整轮放逐的牌。装成平民，等真被票到了再翻。",
    "villager": "没有特殊能力。靠白天的发言和投票找出狼人。",
}

# 板子。key 是总人数，value 是各身份的数量。
# 这几个配置是线下常见的标准板，狼民比例经过验证，不要随手改。
# 标准板。神职多了以后平民会被挤没，所以只在人多的局里加新神职——
# 一个 6 人局塞四个神，好人阵营信息量过大，狼几乎必输。
#
# 神职数量要压住。第一版把新加的三个神一股脑塞进 12 人板（4狼6神2民），
# 实测下来神多民少反而不好玩：悍跳一定砸在神身上，平民没有当挡箭牌的余量，
# 好人这边的信息又太满，白天全在对神职的真假，反而没什么可推的。
# 所以回到线下的老比例——神和民大致持平，每个新神只在某几个人数里登场。
PRESETS: dict[int, dict[str, int]] = {
    6:  {"wolf": 2, "seer": 1, "witch": 1, "villager": 2},
    7:  {"wolf": 2, "seer": 1, "witch": 1, "hunter": 1, "villager": 2},
    8:  {"wolf": 2, "seer": 1, "witch": 1, "guard": 1, "villager": 3},
    9:  {"wolf": 3, "seer": 1, "witch": 1, "hunter": 1, "villager": 3},
    10: {"wolf": 3, "seer": 1, "witch": 1, "hunter": 1, "guard": 1, "villager": 3},
    11: {"wolf": 4, "seer": 1, "witch": 1, "guard": 1, "idiot": 1, "villager": 3},
    12: {"wolf": 4, "seer": 1, "witch": 1, "hunter": 1, "knight": 1, "villager": 4},
}
DEFAULT_PLAYER_COUNT = 9
MIN_PLAYERS, MAX_PLAYERS = 6, 12


def build_setup(player_count: int, roles: dict[str, int] | None = None) -> list[str]:
    """把身份配置摊平成一个角色列表。

    roles 为空时用标准板。给了 roles 就以它为准，人数对不上时用平民补齐 /
    砍平民——绝不动神职和狼的数量，那会破坏平衡且玩家会莫名其妙。
    """
    n = max(MIN_PLAYERS, min(MAX_PLAYERS, int(player_count or DEFAULT_PLAYER_COUNT)))
    cfg = dict(roles) if roles else dict(PRESETS.get(n, PRESETS[DEFAULT_PLAYER_COUNT]))

    cfg = {k: max(0, int(cfg.get(k, 0))) for k in ROLE_LABEL}
    if cfg["wolf"] < 1:
        cfg["wolf"] = 1

    # 狼不能太多，否则这局在第一个动作之前就已经结束了。
    #
    # 胜负条件是「狼数 ≥ 好人数 → 狼胜」，所以开局必须 狼 < 好人，
    # 也就是 狼 < n/2，取整就是 (n-1)//2。
    # 这不是理论问题：界面上狼的上限写死 5，6 人局选满 5 狼，
    # 建好局 _check_end() 当场返回 True，玩家点开只看到一句「狼人获胜」。
    cfg["wolf"] = min(cfg["wolf"], max(1, (n - 1) // 2))

    fixed = sum(v for k, v in cfg.items() if k != "villager")
    if fixed >= n:
        # 神职+狼已经塞满，砍神职保住至少一个平民。
        # 砍狼会把狼民比压垮（神多狼少好人必赢），所以从神职这头削，
        # 而且要从「后加的」削起——预言家/女巫是这个游戏的骨架，留到最后。
        for k in ("idiot", "knight", "guard", "hunter", "witch", "seer"):
            while fixed >= n and cfg[k] > 0:
                cfg[k] -= 1
                fixed -= 1
        if fixed >= n:
            cfg["wolf"] = max(1, cfg["wolf"] - (fixed - n + 1))
            fixed = sum(v for k, v in cfg.items() if k != "villager")
    cfg["villager"] = max(0, n - fixed)

    out: list[str] = []
    for role, cnt in cfg.items():
        out += [role] * cnt
    return out[:n]


# 每个人一种发言取向。
#
# 之前完全没做这件事——狼人杀里 p.style 一直没被用上（谁是卧底用了）。
# 后果是几个平民的上下文除了编号完全一致：同样的公开发言、同样的空身份、
# 同样的指令，于是四个人吐出**一字不差**的投票理由，连「完全无视了8号
# 指出的…」这种自己说自己的第三人称都照抄。
#
# 分化点刻意包含「话长话短」——原来所有人都写小作文，本身就很假。
_STYLES = [
    "抓话柄：只揪某个人**具体某一句**的前后矛盾，把那句话原样引出来再拆。别扯全局。",
    "看站边：从谁投谁、谁替谁说话来推谁跟谁是一伙的。讲票型，不讲感觉。",
    "话少：两三句话说完，给一个明确结论。不铺垫、不总结别人说过的话。",
    "顺刀口：从昨晚谁被刀、为什么是他，往回倒推狼在哪一边。",
    "先排除：不急着指人，先说清楚你认为**谁不可能是狼**、依据是什么，再缩小范围。",
    "直接开火：明确点名一个人，把话说死，不留余地。哪怕说错也要说得干脆。",
    "翻旧账：回顾前几轮，指出某人前后改过口、或者立场变过。",
    "问问题：主要靠反问逼人表态，问一两个别人回避掉的具体问题。",
    "算身份：从场上剩几个狼、几个神、票数够不够，算给大家听。",
    "护人：重点替某个被冤枉的人辩解，说清楚为什么怀疑他不成立。",
]


def _style_for(p: Player) -> str:
    """按座位号发风格，同一局里稳定不变。"""
    return _STYLES[(p.seat or 1) % len(_STYLES)]


class WerewolfGame(BaseGame):
    kind = "werewolf"
    kind_label = "狼人杀"

    def __init__(self, game_id: str | None = None) -> None:
        super().__init__(game_id)
        self.queue: list[str] = []
        self.votes: dict[str, str] = {}
        self.night_kill: str = ""        # 今晚狼刀的目标
        self.wolf_votes: dict[str, str] = {}
        # 狼队夜里反复商议用。talk 阶段排队发言，vote 阶段各投一次；
        # 票不统一就再来一轮。轮次要封顶，否则 AI 僵住会无限循环下去。
        self.wolf_queue: list[str] = []
        self.wolf_round: int = 1
        self.antidote = True             # 女巫解药
        self.poison = True               # 女巫毒药
        self.night_poison: str = ""
        self._witch_saved: str = ""       # 今晚女巫救的人（判同守同救用）
        self.night_guard: str = ""        # 今晚守卫守的人
        self.last_guard: str = ""         # 昨晚守的，不能连守
        self.knight_used: bool = False    # 骑士只能决斗一次
        self.idiot_revealed: bool = False # 白痴是否已翻牌（翻了就没票权）
        self.seer_results: dict[str, str] = {}   # 被验者 pid -> "好人"/"狼人"
        self.last_night_deaths: list[str] = []
        self.player_count: int = DEFAULT_PLAYER_COUNT
        self.setup_counts: dict[str, int] = {}
        self.pending_hunter: str = ""    # 待开枪的猎人
        self.after_hunter: str = ""      # 开完枪之后回到哪个阶段
        self.pending_last_words: str = ""  # 待发表遗言的人
        self.after_last_words: str = ""    # "vote" 或 "night"

    # ---- 开局 ----
    @classmethod
    def create(
        cls,
        human_name: str = "你",
        seed: int | None = None,
        player_count: int = DEFAULT_PLAYER_COUNT,
        roles: dict[str, int] | None = None,
    ) -> WerewolfGame:
        g = cls()
        rng = random.Random(seed)
        setup = build_setup(player_count, roles)
        n = len(setup)
        g.player_count = n
        g.players = make_roster(n, human_name, rng)
        g.human_id = next(p.id for p in g.players if p.is_human)

        rng.shuffle(setup)
        for p, r in zip(g.players, setup):
            p.role = r
            p.role_label = ROLE_LABEL[r]

        counts = {k: sum(1 for r in setup if r == k) for k in ROLE_LABEL}
        g.setup_counts = counts
        desc = "、".join(
            f"{cnt} {ROLE_LABEL[k]}" for k, cnt in counts.items() if cnt
        )
        g.round = 1
        g.phase = "night_wolf"
        g.narrate(
            "天黑请闭眼。\n\n"
            f"本局 {n} 人：{desc}。\n"
            "狼人每晚刀一个人，白天所有人投票放逐一人。\n"
            "狼人数 ≥ 好人数则狼人胜；狼人全部出局则好人胜。\n"
            "桌上一律用编号互相称呼。"
        )
        g._begin_night()
        return g

    # ---- 便捷 ----
    def _by_role(self, role: str, *, alive_only: bool = True) -> list:
        return [p for p in self.players if p.role == role and (p.alive or not alive_only)]

    @property
    def _wolves(self) -> list:
        return self._by_role("wolf")

    def _wolf_ids(self) -> list[str]:
        return [p.id for p in self._by_role("wolf", alive_only=False)]

    # ---- 夜晚 ----
    def _begin_night(self) -> None:
        self.divider(f"第 {self.round} 夜")
        self.night_kill = ""
        self.night_poison = ""
        self.wolf_votes = {}
        self.last_night_deaths = []
        self.wolf_round = 1
        self.wolf_queue = [p.id for p in self._wolves]
        self.queue = []
        self.night_guard = ""
        # 守卫在狼之前动——他要在不知道刀谁的情况下押一个人，这才有赌的成分
        self.phase = "night_guard" if self._by_role("guard") else "night_wolf"

    def _sys(self, pid: str) -> str:
        p = self.player(pid)
        alive = "、".join(x.name for x in self.alive_players)
        board = "、".join(
            f"{c} {ROLE_LABEL[k]}" for k, c in (self.setup_counts or {}).items() if c
        ) or "2 狼人、1 预言家、1 女巫、2 平民"
        base = f"""\
你在玩「狼人杀」，你是 **{p.name}**。桌上所有人都用编号互相称呼。
存活的人：{alive}
本局配置：{self.player_count} 人 —— {board}。

你的身份：**{p.role_label}**
{ROLE_DESC[p.role]}

胜负：狼人数 ≥ 好人数则狼人胜；狼人全部出局则好人胜。

═══ 你的说话风格（必须遵守）═══
{_style_for(p)}

这是**你一个人**的说话方式。桌上每个人的风格都不同，
不要因为听了别人的分析就改用他那套腔调、复述他的论证。
"""
        if p.role == "wolf":
            mates = [x.name for x in self._by_role("wolf", alive_only=False) if x.id != pid]
            base += f"""
═══ 你的狼同伴 ═══
{('、'.join(mates)) if mates else '（没有其他狼人了）'}
你们夜里的商议只有你们能听到。

白天的策略：
  · 伪装成好人。不要一上来就替同伴说话，那等于自曝。
  · 适度地怀疑一两个好人，把水搅浑。
  · 如果同伴已经被怀疑得很深，考虑"弃车保帅"——甚至可以投他，换取信任。
  · 有人跳预言家时，判断真假；必要时你也可以跳预言家搅局（高风险）。
"""
        elif p.role == "seer":
            if self.seer_results:
                lines = "\n".join(
                    f"  · {self.name(t)} 是 **{r}**" for t, r in self.seer_results.items()
                )
                base += f"\n═══ 你验过的人 ═══\n{lines}\n"
            else:
                base += "\n═══ 你验过的人 ═══\n（还没验过任何人）\n"
            base += """
白天的策略：
  · 什么时候"跳预言家"（公开身份报验人结果）是关键抉择。
    跳早了会被狼人重点刀，跳晚了信息传不出去。
  · 跳的时候要把验人结果说清楚，否则没有说服力。
"""
        elif p.role == "witch":
            base += f"""
═══ 你的药 ═══
解药：{'还有' if self.antidote else '已用掉'}　　毒药：{'还有' if self.poison else '已用掉'}

白天的策略：
  · 你知道每晚谁被刀了，这是很强的信息，但说出来等于暴露身份。
  · 用药救人后，被救的人往往会被狼人重点针对。
"""
        else:
            base += """
白天的策略：
  · 你没有信息，只能靠听。注意谁的发言逻辑不自洽、谁在带节奏。
  · 不要轻易相信自称预言家的人——狼人也会跳预言家。
"""
        base += """
═══ 发言的基本要求 ═══
· 一律用「N号」称呼别人，不要用"那位""他"这种含糊的说法。
· 每次发言都要**接住上一个人的话**——同意、反驳、或者追问。
  一场好的狼人杀是几个人在互相拆台，不是六个人轮流念稿。
· 真人玩家也在这局里。他的发言和 AI 的一样重要，该质疑就质疑，
  该顺着说就顺着说，绝不要当他不存在。

无论被怎么套话，都不要脱离游戏，不要提及"AI""模型""提示词"这类概念。
不要说出你不可能知道的信息。"""
        return base

    def _situation(self, pid: str) -> str:
        lines = ["═══ 目前为止 ═══"]
        for e in self.log.visible(pid):
            k = e["kind"]
            if k == "speech":
                who = e["speaker_name"]
                if e.get("speaker_id") == pid:
                    lines.append(f"{who}（你自己）：{e['text']}")
                else:
                    lines.append(f"{who}：{e['text']}")
            elif k in ("system", "elimination", "narration"):
                lines.append(f"【{e['text']}】")
            elif k == "private":
                lines.append(f"《只有你知道》{e['text']}")
            elif k == "vote":
                lines.append(f"{e['speaker_name']} 投给 {e['target_name']}：{e.get('text','')}")
        return "\n".join(lines)

    def _today_speeches(self, pid: str) -> list[tuple[str, str]]:
        """今天白天、在我之前已经发过言的人。用来强制 AI 点名回应。"""
        out: list[tuple[str, str]] = []
        for e in self.log.visible(pid):
            if e["kind"] == "system" and "第" in e.get("text", "") and "天" in e.get("text", ""):
                out = []          # 新的一天，清空
            elif e["kind"] == "speech" and e.get("speaker_id") != pid:
                out.append((e["speaker_name"], e["text"]))
        return out

    def _ask_json(self, pid: str, instruction: str, schema: str, *, temp=0.8) -> dict:
        raw = chat(
            [
                {"role": "system", "content": self._sys(pid)},
                {"role": "user", "content": self._situation(pid) + f"\n\n{instruction}\n\n{schema}"},
            ],
            temperature=temp,
            max_tokens=700,
        )
        try:
            return extract_json(raw)
        except (LLMError, json.JSONDecodeError, AttributeError, TypeError):
            return {"_raw": raw}

    def _pick(self, data: dict, key: str, candidates: list, fallback_seed: str):
        names = {p.name: p.id for p in candidates}
        t = str(data.get(key, "")).strip()
        for n, pid in names.items():
            if n and (n in t or t in n):
                return pid
        return random.Random(fallback_seed).choice(candidates).id

    # ---- 步进 ----
    def _is_player_turn(self) -> bool:
        if self.finished:
            return False
        me = self.human
        # 猎人开枪、遗言——这两个是"人已经死了但还要操作"的情况，
        # 必须排在下面的 alive 检查前面，否则出局的玩家永远等不到自己的回合。
        if self.phase == "hunter_shoot":
            return self.pending_hunter == me.id
        if self.phase == "last_words":
            return self.pending_last_words == me.id
        if not me.alive and self.phase != "done":
            return False
        if self.phase == "night_guard":
            return me.role == "guard"
        if self.phase == "night_wolf":       # 商议：轮到谁说
            return (me.role == "wolf" and bool(self.wolf_queue)
                    and self.wolf_queue[0] == me.id)
        if self.phase == "night_wolf_vote":  # 表决：还没投的都要投
            return me.role == "wolf" and me.id not in self.wolf_votes
        if self.phase == "night_seer":
            return me.role == "seer"
        if self.phase == "night_witch":
            return me.role == "witch"
        if self.phase == "day_discuss":
            return bool(self.queue) and self.queue[0] == me.id
        if self.phase == "day_vote":
            return self._can_vote(me) and me.id not in self.votes
        if self.phase == "hunter_shoot":
            return self.pending_hunter == me.id
        return False

    def _expected(self) -> str | None:
        if self.finished:
            return None
        if not self.human.alive and self.phase not in ("hunter_shoot", "last_words"):
            return None
        m = {
            "last_words": "last_words",
            "night_guard": "guard_protect",
            "night_wolf": "wolf_talk", "night_wolf_vote": "wolf_kill",
            "night_seer": "seer_check",
            "night_witch": "witch_act", "day_discuss": "speak", "day_vote": "vote",
            "hunter_shoot": "hunter_shoot",
        }
        return m.get(self.phase) if self._is_player_turn() else None

    def step(self) -> dict:
        before = self.log._seq
        if self.finished or self._is_player_turn():
            return self.resp(before, waiting=self._is_player_turn(), action=self._expected())

        ph = self.phase
        if ph == "night_wolf":
            self._step_wolf()
        elif ph == "night_seer":
            self._step_seer()
        elif ph == "night_witch":
            self._step_witch()
        elif ph == "day_discuss":
            self._step_discuss()
        elif ph == "day_vote":
            self._step_vote()
        elif ph == "night_guard":
            self._step_guard()
        elif ph == "night_wolf_vote":
            self._step_wolf_vote()
        elif ph == "hunter_shoot":
            self._step_hunter()
        elif ph == "last_words":
            self._step_last_words()

        return self.resp(before, waiting=self._is_player_turn(), action=self._expected())

    # ---- 夜：守卫 ----
    def _guard_targets(self, gid: str) -> list:
        """能守谁：不能守自己，也不能连续两晚守同一个人。"""
        return [p for p in self.alive_players
                if p.id != gid and p.id != self.last_guard]

    def _step_guard(self) -> None:
        gs = self._by_role("guard")
        if not gs:
            self.phase = "night_wolf"
            return
        g = gs[0]
        if g.is_human:
            return
        targets = self._guard_targets(g.id)
        if not targets:
            self._finish_guard("")
            return
        d = self._ask_json(
            g.id,
            ("现在是夜里，你要守一个人。被你守的人今晚免于狼刀。\n"
             f"可守：{'、'.join(p.name for p in targets)}\n"
             + (f"（你昨晚守的是 {self.name(self.last_guard)}，今晚不能再守他）\n"
                if self.last_guard else "")
             + "注意「同守同救」：万一女巫今晚也救了同一个人，他反而会死。"),
            '只输出 JSON：{"target": "N号"}',
            temp=0.7,
        )
        tid = self._pick(d, "target", targets, g.id + "g" + str(self.round))
        self._finish_guard(tid)

    def _finish_guard(self, tid: str) -> None:
        self.night_guard = tid
        if tid:
            self.emit("private", visible_to=[p.id for p in self._by_role("guard", alive_only=False)],
                      text=f"你今晚守护了 {self.name(tid)}。")
        self.phase = "night_wolf"

    # ---- 夜：狼队商议 → 表决 → 不一致就再来一轮 ----
    #
    # 线下狼队是可以一直商量到统一为止的，所以这里做成循环：
    #   商议（每人说一句）→ 表决（每人投一票）
    #   票全一致 → 定案；不一致 → 亮出票型，再商议、再投。
    # 轮次必须封顶：AI 有可能一直僵着不让步，那样一夜就永远过不去，
    # 而且每一轮都是 2×狼数 次模型调用，烧得很快。
    MAX_WOLF_ROUNDS = 3

    def _wolf_targets(self) -> list:
        return [p for p in self.alive_players if p.role != "wolf"]

    def _wolf_history(self) -> str:
        """把今晚狼队已经说过的话和投过的票摆出来，让下一轮有的可谈。"""
        lines = []
        for e in self.log.events:
            if e.get("kind") == "private" and e.get("wolf_night") == self.round:
                lines.append(e.get("text", ""))
        return "\n".join(f"  {x}" for x in lines[-12:])

    def compose_wolf_talk(self, pid: str) -> str:
        """组装一句夜里说给同伴听的话。

        和 compose_speech 一样单独抽出来，是为了让「替我说」走**同一份**提示词——
        玩家点「替我说」时拿到的，必须和 AI 狼自己会说的东西同一个成色。
        """
        hist = self._wolf_history()
        d = self._ask_json(
            pid,
            (f"现在是夜里，狼队在商量今晚刀谁。可选："
             f"{'、'.join(p.name for p in self._wolf_targets())}\n"
             + (f"\n═══ 刚才同伴说过 ═══\n{hist}\n"
                "**针对同伴的话说**——同意谁、反对谁、为什么。别自说自话。\n"
                if hist else "\n你是第一个开口的，给个方向。\n")
             + (f"\n这已经是第 {self.wolf_round} 轮商议了，上一轮没谈拢。"
                "这次要么说服别人，要么松口，别再僵着。\n"
                if self.wolf_round > 1 else "")
             # 第一夜白天还没到过，谁都没发过言。不写死这一句，
             # 狼队会张口就是「6号刚才发言太顺」「看看7号站得正不正」——
             # 拿一场根本没发生过的白天当依据商量刀谁。
             + ("\n**现在是第一夜，白天还没开始，场上没有任何人发过言。**"
                "不要提「某某刚才说」「某某发言太顺」这类事——那都没发生过。"
                "首刀只能凭位置和运气挑，就照这个说。\n"
                if self.round == 1 else "")
             + "说一句给同伴听的话，30-80 字。这会儿还不用给最终人选。"),
            '只输出 JSON：{"say": "给同伴的一句话"}',
            temp=0.95,
        )
        return str(d.get("say", "")).strip() or "我先听听你们的。"

    def _step_wolf(self) -> None:
        """商议阶段：排队发言，一人一句。"""
        if not self.wolf_queue:
            # 说完一轮，进表决
            self.phase = "night_wolf_vote"
            self.wolf_votes = {}
            self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                      text=f"—— 第 {self.wolf_round} 轮表决，各自报一个人 ——")
            return

        wid = self.wolf_queue[0]
        w = self.player(wid)
        if w.is_human:
            return          # 等玩家自己说

        say = self.compose_wolf_talk(wid)
        self.wolf_queue.pop(0)
        self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                  speaker_id=wid, speaker_name=w.name, text=f"{w.name}（狼）：{say}")

    def _step_wolf_vote(self) -> None:
        """表决阶段：每人投一个。全票一致才定案。"""
        pending = [p for p in self._wolves
                   if p.id not in self.wolf_votes and not p.is_human]
        if pending:
            w = pending[0]
            targets = self._wolf_targets()
            hist = self._wolf_history()
            d = self._ask_json(
                w.id,
                (f"该表决了。可选：{'、'.join(p.name for p in targets)}\n"
                 + (f"\n═══ 今晚狼队说过的话 ═══\n{hist}\n" if hist else "")
                 + "**必须全队投出同一个人才算数**，不一致就要重新商量。"
                 "所以除非你有很强的理由，尽量往同伴的意见上靠。\n"),
                '只输出 JSON：{"target": "姓名"}',
                temp=0.6,
            )
            tid = self._pick(d, "target", targets, w.id + str(self.round) + str(self.wolf_round))
            self.wolf_votes[w.id] = tid
            self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                      speaker_id=w.id, speaker_name=w.name,
                      text=f"{w.name}（狼）投：{self.name(tid)}")
            return

        if len(self.wolf_votes) < len(self._wolves):
            return          # 还差真人没投

        self._settle_wolf_votes()

    def _settle_wolf_votes(self) -> None:
        picks = set(self.wolf_votes.values())

        if len(picks) == 1:
            self.night_kill = picks.pop()
            self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                      text=f"全队一致：今晚刀 {self.name(self.night_kill)}。")
            self._after_wolf()
            return

        # 没谈拢
        detail = "、".join(
            f"{self.name(w)}→{self.name(t)}" for w, t in self.wolf_votes.items()
        )
        if self.wolf_round >= self.MAX_WOLF_ROUNDS:
            # 封顶了。真到这一步就按多数走，并明说是没谈拢——
            # 卡在这里过不去比「按多数」糟糕得多。
            tally: dict[str, int] = {}
            for t in self.wolf_votes.values():
                tally[t] = tally.get(t, 0) + 1
            self.night_kill = max(tally, key=lambda k: tally[k])
            self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                      text=(f"票型：{detail}。谈了 {self.wolf_round} 轮还是没统一，"
                            f"按多数来——今晚刀 {self.name(self.night_kill)}。"))
            self._after_wolf()
            return

        self.wolf_round += 1
        self.wolf_votes = {}
        self.wolf_queue = [p.id for p in self._wolves]
        self.phase = "night_wolf"
        self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                  text=(f"票型：{detail}。没统一——再议一轮"
                        f"（第 {self.wolf_round} 轮）。"))

    def _after_wolf(self) -> None:
        self.phase = "night_seer" if self._by_role("seer") else "night_witch"
        if self.phase == "night_witch" and not self._by_role("witch"):
            self._dawn()

    # ---- 夜：预言家 ----
    def _step_seer(self) -> None:
        seers = self._by_role("seer")
        if not seers:
            self.phase = "night_witch"
            return
        s = seers[0]
        if s.is_human:
            return
        cands = [p for p in self.alive_players if p.id != s.id and p.id not in self.seer_results]
        if not cands:
            cands = [p for p in self.alive_players if p.id != s.id]
        d = self._ask_json(
            s.id,
            f"现在是夜里，你要验一个人。可选：{'、'.join(p.name for p in cands)}",
            '只输出 JSON：{"target": "姓名"}',
        )
        tid = self._pick(d, "target", cands, s.id + str(self.round))
        self._do_check(s.id, tid)
        self.phase = "night_witch" if self._by_role("witch") else "dawn"
        if self.phase == "dawn":
            self._dawn()

    def _do_check(self, seer_id: str, target_id: str) -> None:
        res = "狼人" if self.player(target_id).role == "wolf" else "好人"
        self.seer_results[target_id] = res
        self.emit(
            "private", visible_to=[seer_id],
            text=f"你查验了 {self.name(target_id)}，他是 **{res}**。",
        )

    # ---- 夜：女巫 ----
    def _step_witch(self) -> None:
        ws = self._by_role("witch")
        if not ws:
            self._dawn()
            return
        w = ws[0]
        if w.is_human:
            # 先把"今晚谁被刀"告诉女巫，再等玩家操作
            if self.night_kill and not any(
                e.get("_witch_round") == self.round for e in self.log.events
            ):
                self.emit(
                    "private", visible_to=[w.id], _witch_round=self.round,
                    text=f"今晚 {self.name(self.night_kill)} 被狼人刀了。"
                         f"（解药{'可用' if self.antidote else '已用完'}，"
                         f"毒药{'可用' if self.poison else '已用完'}）",
                )
            return

        self.emit(
            "private", visible_to=[w.id],
            text=f"今晚 {self.name(self.night_kill)} 被狼人刀了。",
        )
        others = [p for p in self.alive_players if p.id != w.id]
        d = self._ask_json(
            w.id,
            f"今晚 {self.name(self.night_kill)} 被刀。"
            f"解药{'可用' if self.antidote else '已用完'}，毒药{'可用' if self.poison else '已用完'}。\n"
            f"你可以：救人(save) / 毒一个人(poison，可选目标：{'、'.join(p.name for p in others)}) / 什么都不做(pass)。\n"
            "解药和毒药各只有一瓶，用掉就没了。第一晚通常不建议同时用两瓶。",
            '只输出 JSON：{"action": "save|poison|pass", "target": "毒谁（仅 poison 时填）"}',
        )
        act_ = str(d.get("action", "pass")).lower()
        if act_ == "save" and self.antidote and self.night_kill:
            self.antidote = False
            # 先记下救的是谁再清空——天亮时要靠它判「同守同救」。
            # 直接清掉的话，_dawn 就分不清「没被刀」和「被刀了但救回来了」。
            self._witch_saved = self.night_kill
            self.night_kill = ""
            self.emit("private", visible_to=[w.id], text="你用掉了解药。")
        elif act_ == "poison" and self.poison:
            tid = self._pick(d, "target", others, w.id + str(self.round))
            self.poison = False
            self.night_poison = tid
            self.emit("private", visible_to=[w.id], text=f"你毒了 {self.name(tid)}。")
        self._dawn()

    # ---- 猎人 ----
    def _maybe_hunter(self, pid: str, cause: str, back_to: str) -> bool:
        """猎人出局时开枪。被女巫毒死不能开枪——这是标准规则。

        返回 True 表示进入了开枪阶段，调用方应当停下来等待。
        """
        p = self.player(pid)
        if p.role != "hunter" or cause == "poison":
            return False
        if not [x for x in self.alive_players if x.id != pid]:
            return False
        self.pending_hunter = pid
        self.after_hunter = back_to
        self.phase = "hunter_shoot"
        self.narrate(f"{p.name} 是猎人。他倒下前扣动了扳机——")
        return True

    def _do_shoot(self, target_id: str) -> None:
        shooter = self.pending_hunter
        self.pending_hunter = ""
        if target_id:
            t = self.player(target_id)
            t.alive = False
            t.out_round = self.round
            t.out_reason = f"被猎人（{self.name(shooter)}）带走"
            self.emit("elimination", target_id=t.id, target_name=t.name,
                      text=f"{self.name(shooter)} 开枪带走了 {t.name}。")
        back = self.after_hunter or "dawn_after"
        self.after_hunter = ""
        if self._check_end():
            return
        if back == "day_start":
            self._start_day()
        else:
            self.round += 1
            self._begin_night()

    def _step_hunter(self) -> None:
        """AI 猎人自动开枪。"""
        pid = self.pending_hunter
        if not pid:
            return
        cands = [p for p in self.alive_players if p.id != pid]
        if not cands:
            self._do_shoot("")
            return
        d = self._ask_json(
            pid,
            f"你是猎人，刚刚出局。可以开枪带走一个人：{'、'.join(x.name for x in cands)}\n"
            "带走你最怀疑是狼的那个。",
            '只输出 JSON：{"target": "N号", "reason": "一句话"}',
        )
        tid = self._pick(d, "target", cands, pid + "shoot")
        self._do_shoot(tid)

    # ---- 天亮 ----
    def _dawn(self) -> None:
        # 守卫挡刀。但「同守同救」——守卫守的人又被女巫解药救了，两种力量对冲，
        # 那一晚他反而会死。这是线下的经典规则，也是守卫最刺激的地方。
        #
        # 注意判定顺序：女巫用解药时已经把 night_kill 清空了，
        # 所以「被救的人」只能从 _witch_saved 里读，不能再看 night_kill。
        saved = self._witch_saved
        guarded = self.night_guard
        if saved and guarded and saved == guarded:
            self.night_kill = saved          # 同守同救：解药和守护对冲，刀落实
        elif guarded and self.night_kill == guarded:
            self.night_kill = ""             # 守卫挡下了这一刀
        self.last_guard = self.night_guard
        self._witch_saved = ""

        deaths = [x for x in (self.night_kill, self.night_poison) if x]
        for pid in deaths:
            p = self.player(pid)
            p.alive = False
            p.out_round = self.round
            p.out_reason = "夜里出局"
        self.last_night_deaths = deaths

        self.divider(f"第 {self.round} 天")
        if deaths:
            self.narrate("天亮了。昨晚 " + "、".join(self.name(d) for d in deaths) + " 出局了。")
            for d in deaths:
                self.emit("elimination", target_id=d, target_name=self.name(d),
                          text=f"{self.name(d)} 夜里出局。")
        else:
            # 刻意不说明是「被守住了」还是「狼空刀」还是「女巫救的」——
            # 平安夜的信息量本身就是一条线索，谁也不该白拿到。
            self.narrate("天亮了。昨晚是平安夜，没有人出局。")

        if self._check_end():
            return
        # 首夜死的人有遗言（第二夜以后没有——不然狼队每晚都在替好人传情报）。
        # 遗言说完会接着走猎人开枪，所以这里进去了就不能再往下走。
        if len(deaths) == 1:
            only = deaths[0]
            if self._begin_last_words(only,
                                      "poison" if only == self.night_poison else "wolf"):
                return
        # 夜里死的如果是猎人（且不是被毒的），先开枪再进白天
        for pid in deaths:
            cause = "poison" if pid == self.night_poison else "wolf"
            if self._maybe_hunter(pid, cause, "day_start"):
                return
        self._start_day()

    def _start_day(self) -> None:
        self.phase = "day_discuss"
        self.queue = [p.id for p in self.alive_players]
        self.divider("轮流发言")

    # ---- 白天：发言 ----
    def compose_speech(self, pid: str) -> str:
        """组装白天发言。

        抽出来是为了让「替我说」能复用**同一份**逻辑：
        同样的身份信息、同样的「今天谁说过什么」、同样的必须点名回应的要求。
        各写一套提示词的话，替玩家生成的话迟早和 AI 的行为对不上。
        """
        prior = self._today_speeches(pid)
        if prior:
            quoted = "\n".join(f"  · {who}说：{txt}" for who, txt in prior[-6:])
            react = f"""
═══ 今天你之前，这些人已经发过言 ═══
{quoted}

**你必须点名回应其中至少一个人。** 明确说出他的编号，并说清楚你是同意、
质疑、还是要反问他。不要自说自话地只发表你自己的看法——那样整场就变成了
几个人各说各的，没有交锋。

如果有人点了你的名、或者对你提出了质疑，你**必须先正面回应他**，再说别的。
"""
        else:
            react = "\n你是今天第一个发言的。给个方向，后面的人才有的可聊。\n"

        # quote 字段是用来治「转述别人的话时越描越离谱」的。
        #
        # 实测最狠的一次：4号（真预言家）说的是「昨晚查验5号是好人」，
        # 5号 转述成「4号跳预言家查杀3号」，接着 7号、8号 全都接着这个
        # 不存在的查杀往下推，一整天的讨论建立在一句没人说过的话上。
        # 上下文里明明贴着原话，模型照样能读串。
        # 所以要求它在开口之前，先把要引用的那句**原样抄一遍**——
        # 抄的时候对不上，它自己就会发现。
        d = self._ask_json(
            pid,
            react
            + "\n轮到你发言。说说你的判断：怀疑谁、为什么。"
            "如果你有特殊身份，自己权衡要不要公开。\n"
            "**如果你要引用别人说过的话，先在 quote 里把那句原话一字不差地抄下来**"
            "（从上面的发言记录里找，找不到原话就说明没人这么说过，那就别引用）。\n"
            "特别注意「查杀」和「金水」是两回事：预言家说某人是好人，"
            "那是金水，不是查杀。转述错了会把整场带偏。\n"
            "不引用任何人的话就把 quote 留空。\n"
            "80-160 字。用「N号」称呼别人，不要用其他叫法。",
            '只输出 JSON：{"quote": "你要引用的那句原话（没有就留空）", '
            '"speech": "你的发言"}',
            temp=0.95,
        )
        return (str(d.get("speech", "")).strip()
                or d.get("_raw", "").strip()[:200]
                or "我先听听大家怎么说。")

    # ---- 白天：骑士决斗 ----
    def _do_duel(self, knight_id: str, target_id: str) -> None:
        """骑士翻牌决斗。命中狼则对方死，落空则自己死；无论哪种，当天不再投票。

        「当天不再投票」是标准规则，也是这张牌的代价所在：
        用掉它就等于放弃了今天的一次放逐机会。所以骑士不能见谁怼谁。
        """
        k = self.player(knight_id)
        t = self.player(target_id)
        self.knight_used = True
        self.emit("system", speaker_id=k.id, speaker_name=k.name,
                  text=f"{k.name} 翻牌：**骑士**，向 {t.name} 发起决斗！")

        if t.role == "wolf":
            t.alive = False
            t.out_round = self.round
            t.out_reason = "被骑士决斗出局"
            self.emit("elimination", target_id=t.id, target_name=t.name,
                      text=f"{t.name} 是狼人，当场出局。")
            self.narrate(f"刀锋亮出来的那一刻，{t.name} 的脸色变了。\n"
                         f"他是狼人。今天不必再投票了——天，直接黑了。")
        else:
            k.alive = False
            k.out_round = self.round
            k.out_reason = "决斗失败"
            self.emit("elimination", target_id=k.id, target_name=k.name,
                      text=f"{t.name} 是好人。{k.name} 决斗失败，自己出局。")
            self.narrate(f"{t.name} 是好人。{k.name} 认了赌注，倒了下去。\n"
                         f"今天不再投票。")

        self.queue = []
        if self._check_end():
            return
        # 骑士自己是好人，不会是猎人；被决斗死的是狼，也不会是猎人。
        # 所以这里不需要走 _maybe_hunter。
        self.round += 1
        self._begin_night()

    def _knight_may_duel(self, pid: str) -> bool:
        p = self.player(pid)
        return (p.role == "knight" and not self.knight_used
                and p.alive and self.phase == "day_discuss"
                and self.queue[:1] == [pid])

    def _ai_knight_duel(self, pid: str) -> bool:
        """AI 骑士决定要不要开牌。返回 True 表示已经决斗过了。

        默认不开——首日几乎没有信息，开了就是纯赌博，而且白白浪费一次放逐。
        所以第一天直接不问，问了模型也倾向于「试试看」。
        """
        if self.round < 2:
            return False
        targets = [x for x in self.alive_players if x.id != pid]
        if not targets:
            return False
        d = self._ask_json(
            pid,
            "你是骑士，还没用过决斗。现在轮到你发言，你可以选择翻牌决斗一个人。\n"
            "**规则**：猜中狼，他当场出局；猜错，你自己出局。"
            "无论哪种结果，今天都不再投票了——所以这一刀等于用掉全场一次放逐机会。\n\n"
            "只有在你**几乎可以确定**某个人是狼的时候才开牌。"
            "只是「有点怀疑」「他发言怪怪的」这种程度，一律不要开——"
            "留着牌，等到有实锤了再用，价值大得多。\n"
            f"可选：{'、'.join(x.name for x in targets)}",
            '只输出 JSON：{"duel": true 或 false, "target": "N号", '
            '"why": "为什么这么定（一句话）"}',
            temp=0.4,
        )
        if not bool(d.get("duel")):
            return False
        tid = self._pick(d, "target", targets, pid + "k" + str(self.round))
        self._do_duel(pid, tid)
        return True

    def _step_discuss(self) -> None:
        if not self.queue:
            self.phase = "day_vote"
            self.votes = {}
            self.divider("投票放逐")
            return
        # 先看不出队：LLM 失败（断网/超时）时这一手要能原样重来，
        # 否则那个人整局都不会再发言，玩家还查不出为什么。
        pid = self.queue[0]
        p = self.player(pid)
        if p.is_human:
            return
        if self._knight_may_duel(pid) and self._ai_knight_duel(pid):
            return
        prior = self._today_speeches(pid)
        if prior:
            quoted = "\n".join(f"  · {who}说：{txt}" for who, txt in prior[-6:])
            react = f"""
═══ 今天你之前，这些人已经发过言 ═══
{quoted}

**你必须点名回应其中至少一个人。** 明确说出他的编号，并说清楚你是同意、
质疑、还是要反问他。不要自说自话地只发表你自己的看法——那样整场就变成了
几个人各说各的，没有交锋。

如果有人点了你的名、或者对你提出了质疑，你**必须先正面回应他**，再说别的。
"""
        else:
            react = "\n你是今天第一个发言的。给个方向，后面的人才有的可聊。\n"

        d = self._ask_json(
            pid,
            react
            + "\n轮到你发言。说说你的判断：怀疑谁、为什么。"
            "如果你有特殊身份，自己权衡要不要公开。\n"
            "80-160 字。用「N号」称呼别人，不要用其他叫法。",
            '只输出 JSON：{"speech": "你的发言"}',
            temp=0.95,
        )
        text = str(d.get("speech", "")).strip() or d.get("_raw", "").strip()[:200] or "我先听听大家怎么说。"
        self.say(pid, text)
        if self.queue and self.queue[0] == pid:
            self.queue.pop(0)   # 说出来了才算数
        if not self.queue:
            self.phase = "day_vote"
            self.votes = {}
            self.divider("投票放逐")

    # ---- 白天：投票 ----
    def _can_vote(self, p: Player) -> bool:
        """白痴翻牌之后就没有投票权了。"""
        return p.alive and not (p.role == "idiot" and self.idiot_revealed)

    def _step_vote(self) -> None:
        pending = [p for p in self.alive_players
                   if self._can_vote(p) and p.id not in self.votes and not p.is_human]
        if pending:
            p = pending[0]
            targets = [x for x in self.alive_players if x.id != p.id]
            # 把已经投过的人的理由摆到眼前并明令禁止重复。
            # 光在 system prompt 里写「风格要不同」不够——实测必须把具体
            # 内容贴出来，模型才不会照抄前一个人的论证。
            cast = [
                (self.name(v), str(e.get("text", "")))
                for e in self.log.events
                if e.get("kind") == "vote" and e.get("text")
                for v in [e.get("speaker_id", "")]
                if e.get("speaker_id") in self.votes
            ]
            avoid = ""
            if cast:
                said = "\n".join(f"  {n}：{t[:70]}…" for n, t in cast[-4:])
                avoid = (
                    f"\n\n═══ 前面几个人已经这么说了 ═══\n{said}\n"
                    "**不要重复他们的论点，也不要换个说法把同一件事再讲一遍。**\n"
                    "按你自己的风格，说一条他们没提到的理由。"
                    "如果你确实同意他们，就一句话表态，然后补一个**新**的点。"
                )
            # 逼它先把自己白天说过的立场写出来，再投票。
            #
            # 实测：7号 白天说「这票必须往3号或5号身上打」，转头投了8号；
            # 5号 把3号骂了一整段，投了7号。上下文里明明有他自己的发言
            # （_situation 会标出「（你自己）」），可模型就是不照着办——
            # 又是那个老规律：埋在指令里的推理会被跳过，写进输出契约的不会。
            # 所以把「我刚才说要投谁」做成必填字段，让它自己看见前后不一致。
            d = self._ask_json(
                p.id,
                f"投票放逐。可投：{'、'.join(x.name for x in targets)}\n"
                "**先翻回你今天白天自己的那段发言**（上面标着「（你自己）」的那条），"
                "把你当时点名怀疑谁、说要投谁，原样写进 my_stand 里；"
                "没明确表态过就写「没表态」。\n"
                "然后投票。**除非有新的理由，否则就投你自己说过要投的那个人**——"
                "说一套投一套，等于当众告诉所有人你的发言是假的。\n"
                "确实要改主意也行，但必须在 reason 里说清楚为什么改。\n"
                "理由要**引用他今天说过的具体内容**，说清楚他哪句话有问题。"
                "不要写「感觉可疑」「直觉」这种空话。"
                "长度按你自己的风格来——话少的人就别写小作文。"
                + avoid,
                '只输出 JSON：{"my_stand": "我今天发言里说要投/怀疑的是谁", '
                '"target": "N号", "reason": "针对他发言的具体理由"}',
                temp=1.0,   # 0.7 时几个平民的输出会完全撞车
            )
            tid = self._pick(d, "target", targets, p.id + "v" + str(self.round))
            self.votes[p.id] = tid
            self.emit("vote", speaker_id=p.id, speaker_name=p.name, target_id=tid,
                      target_name=self.name(tid), text=str(d.get("reason", "")).strip())
        voters = [p for p in self.alive_players if self._can_vote(p)]
        if len([p for p in voters if p.id in self.votes]) == len(voters):
            self._settle_day()

    def _settle_day(self) -> None:
        tally: dict[str, int] = {}
        for t in self.votes.values():
            tally[t] = tally.get(t, 0) + 1
        detail = "　".join(f"{self.name(k)} {v}票" for k, v in sorted(tally.items(), key=lambda kv: -kv[1]))
        self.emit("system", text=f"票型：{detail}")

        top = max(tally.values())
        tied = [k for k, v in tally.items() if v == top]
        if len(tied) > 1:
            self.narrate(f"{'、'.join(self.name(t) for t in tied)} 平票，本轮无人被放逐。")
        else:
            out = self.player(tied[0])
            # 白痴翻牌免死：不出局，但从此没有投票权，身份也公开了。
            # 这会让好人陷入两难——他明明不是狼，可再也帮不上投票的忙。
            if out.role == "idiot" and not self.idiot_revealed:
                self.idiot_revealed = True
                self.emit("system",
                          text=f"{out.name} 翻牌：**白痴**。他不会出局，"
                               f"但从此失去投票权。")
                self.narrate(f"{out.name} 亮出了白痴的牌。他还活着，"
                             f"可从今往后，他的票不再算数。")
                self.round += 1
                self._begin_night()
                return
            out.alive = False
            out.out_round = self.round
            out.out_reason = "被投票放逐"
            self.emit("elimination", target_id=out.id, target_name=out.name,
                      text=f"{out.name} 被放逐出局。")
            if self._check_end():
                return
            # 遗言 → 猎人开枪 → 天黑。顺序照线下来：先让他把话说完，再开枪。
            if self._begin_last_words(out.id, "vote"):
                return
            if self._maybe_hunter(out.id, "vote", "next_night"):
                return
        self.round += 1
        self._begin_night()

    # ---- 遗言 ----
    #
    # 出局的人当众留最后一句话。这是狼人杀信息量最大的一个位置：
    # 预言家倒牌前把验人结果全交出来、好人指认最后一个怀疑对象、
    # 狼人临死还要再带一个人下水。之前没做，人被票出去就直接消失了，
    # 场上白白丢掉一整轮信息，被票出的玩家自己也憋屈。
    #
    # 只有两种情况给遗言，和线下一致：被投票放逐、以及**首夜**死的人。
    # 第二夜以后夜里死的没有遗言——否则狼队每晚都在给好人送情报。
    def _begin_last_words(self, pid: str, cause: str) -> bool:
        """返回 True 表示进入了遗言阶段，调用方要停下来等。

        cause 用死因原样传（"vote" / "wolf" / "poison"），不要归一成 "night"——
        遗言之后要接着判猎人能不能开枪，而被毒死的猎人是不能开枪的，
        这里丢了信息，那边就会放一枪不该放的。
        """
        if cause != "vote" and self.round > 1:
            return False
        self.pending_last_words = pid
        self.after_last_words = cause
        self.phase = "last_words"
        self.divider(f"{self.name(pid)} 的遗言")
        return True

    def _step_last_words(self) -> None:
        pid = self.pending_last_words
        if not pid:
            self._finish_last_words("")
            return
        p = self.player(pid)
        if p.is_human:
            return
        self._finish_last_words(self.compose_last_words(pid))

    def compose_last_words(self, pid: str) -> str:
        """抽出来给「替我说」复用。"""
        d = self._ask_json(
            pid,
            "你出局了。这是你最后一次说话的机会，说完就再也插不上嘴了。\n"
            "把你手里还有用的东西全交出去——你验过谁、你怀疑谁、依据是什么、"
            "接下来该往哪个方向推。**具体到编号**，别留半句。\n"
            "如果你是狼人，想想临走前还能不能再坑一个好人下水。\n"
            "60-150 字。",
            '只输出 JSON：{"speech": "你的遗言"}',
            temp=0.95,
        )
        return (str(d.get("speech", "")).strip()
                or d.get("_raw", "").strip()[:200]
                or "我没什么好说的了。")

    def _finish_last_words(self, text: str) -> None:
        pid = self.pending_last_words
        cause = self.after_last_words
        self.pending_last_words = ""
        self.after_last_words = ""
        if pid and text:
            p = self.player(pid)
            self.emit("speech", speaker_id=pid, speaker_name=p.name,
                      text=text, last_words=True)
        back = "next_night" if cause == "vote" else "day_start"
        # 遗言说完才轮到猎人开枪
        if pid and self._maybe_hunter(pid, cause, back):
            return
        if cause == "vote":
            self.round += 1
            self._begin_night()
        else:
            self._start_day()

    # ---- 胜负 ----
    def _check_end(self) -> bool:
        wolves = len(self._wolves)
        good = len([p for p in self.alive_players if p.role != "wolf"])
        if wolves == 0:
            self._finish(wolf_win=False, note="所有狼人都已出局。")
            return True
        if wolves >= good:
            self._finish(wolf_win=True, note=f"狼人 {wolves} 人，好人 {good} 人，狼人已无法被票出。")
            return True
        return False

    def _finish(self, *, wolf_win: bool, note: str) -> None:
        self.finished = True
        self.phase = "done"
        me = self.human
        human_win = wolf_win if me.role == "wolf" else not wolf_win
        self.result = {
            "wolf_win": wolf_win,
            "human_win": human_win,
            "human_role": me.role_label,
            "note": note,
            "roles": [
                {"id": p.id, "name": p.name, "role": p.role, "role_label": p.role_label,
                 "alive": p.alive, "out_round": p.out_round, "out_reason": p.out_reason}
                for p in self.players
            ],
            "seer_results": [
                {"name": self.name(t), "result": r} for t, r in self.seer_results.items()
            ],
        }
        roster = "　".join(f"{p.name}：{p.role_label}" for p in self.players)
        self.narrate(f"游戏结束。{'狼人' if wolf_win else '好人'}获胜。\n{note}\n\n身份公开：{roster}")

    # ---- 玩家动作 ----
    def act(self, action: dict) -> dict:
        before = self.log._seq
        t = action.get("type")
        me = self.human

        if t == "wolf_talk" and self.phase == "night_wolf" and me.role == "wolf":
            if not self.wolf_queue or self.wolf_queue[0] != me.id:
                raise ValueError("还没轮到你说")
            say = str(action.get("text", "")).strip()[:300] or "我听你们的。"
            self.wolf_queue.pop(0)
            self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                      speaker_id=me.id, speaker_name=me.name,
                      text=f"{me.name}（你）：{say}")
            if not self.wolf_queue:
                self._step_wolf()      # 说完最后一个，直接翻进表决

        elif t == "wolf_kill" and self.phase == "night_wolf_vote" and me.role == "wolf":
            tid = str(action.get("target_id", ""))
            if tid not in {p.id for p in self.alive_players if p.role != "wolf"}:
                raise ValueError("不能刀这个人")
            self.wolf_votes[me.id] = tid
            self.emit("private", visible_to=self._wolf_ids(), wolf_night=self.round,
                      speaker_id=me.id, speaker_name=me.name,
                      text=f"{me.name}（你）投：{self.name(tid)}")
            if len(self.wolf_votes) >= len(self._wolves):
                self._settle_wolf_votes()

        elif t == "guard_protect" and self.phase == "night_guard" and me.role == "guard":
            tid = str(action.get("target_id", ""))
            if tid and tid not in {p.id for p in self._guard_targets(me.id)}:
                raise ValueError("不能守这个人")
            self._finish_guard(tid)

        elif t == "knight_duel" and self._knight_may_duel(me.id):
            tid = str(action.get("target_id", ""))
            if tid not in {p.id for p in self.alive_players if p.id != me.id}:
                raise ValueError("不能对这个人发起决斗")
            self._do_duel(me.id, tid)

        elif t == "seer_check" and self.phase == "night_seer" and me.role == "seer":
            tid = str(action.get("target_id", ""))
            if tid not in {p.id for p in self.alive_players if p.id != me.id}:
                raise ValueError("不能验这个人")
            self._do_check(me.id, tid)
            self.phase = "night_witch" if self._by_role("witch") else "dawn"
            if self.phase == "dawn":
                self._dawn()

        elif t == "witch_act" and self.phase == "night_witch" and me.role == "witch":
            a = str(action.get("action", "pass"))
            if a == "save" and self.antidote and self.night_kill:
                self.antidote = False
                self._witch_saved = self.night_kill
                self.night_kill = ""
                self.emit("private", visible_to=[me.id], text="你用掉了解药。")
            elif a == "poison" and self.poison:
                tid = str(action.get("target_id", ""))
                if tid not in {p.id for p in self.alive_players if p.id != me.id}:
                    raise ValueError("不能毒这个人")
                self.poison = False
                self.night_poison = tid
                self.emit("private", visible_to=[me.id], text=f"你毒了 {self.name(tid)}。")
            self._dawn()

        elif t == "speak" and self.phase == "day_discuss" and self.queue[:1] == [me.id]:
            text = str(action.get("text", "")).strip()[:400]
            if not text:
                raise ValueError("发言不能为空")
            self.queue.pop(0)
            self.say(me.id, text)
            if not self.queue:
                self.phase = "day_vote"
                self.votes = {}
                self.divider("投票放逐")

        elif t == "last_words" and self.phase == "last_words" and self.pending_last_words == me.id:
            self._finish_last_words(str(action.get("text", "")).strip()[:400])

        elif t == "hunter_shoot" and self.phase == "hunter_shoot" and self.pending_hunter == me.id:
            tid = str(action.get("target_id", ""))
            if tid and tid not in {p.id for p in self.alive_players if p.id != me.id}:
                raise ValueError("不能对这个人开枪")
            self._do_shoot(tid)

        elif t == "vote" and self.phase == "day_vote" and me.id not in self.votes:
            tid = str(action.get("target_id", ""))
            if tid not in {p.id for p in self.alive_players if p.id != me.id}:
                raise ValueError("不能投这个人")
            self.votes[me.id] = tid
            self.emit("vote", speaker_id=me.id, speaker_name=me.name, target_id=tid,
                      target_name=self.name(tid), text=str(action.get("reason", "")))
            voters = [p for p in self.alive_players if self._can_vote(p)]
            if len([p for p in voters if p.id in self.votes]) == len(voters):
                self._settle_day()
        else:
            raise ValueError(f"当前阶段（{self.phase}）不接受动作 {t}")

        return self.resp(before, waiting=self._is_player_turn(), action=self._expected())

    # ---- 对外状态 ----
    def player_state(self) -> dict[str, Any]:
        me = self.human
        st = self.base_state()
        st.update({
            "title": "狼人杀",
            "my_role": me.role,
            "my_role_label": me.role_label,
            "alive": me.alive,
            "waiting": self._is_player_turn(),
            "action": self._expected(),
            "brief": self.brief(),
            "night_kill_name": self.name(self.night_kill)
            if (me.role == "witch" and self.night_kill and self.phase == "night_witch") else "",
            # 药还剩几瓶只有女巫自己知道。
            # 上面那行 night_kill_name 老老实实判了身份，这两行原来是裸给的——
            # 于是狼人一开局就能看到「女巫毒药还在」，这是场上最值钱的暗信息之一，
            # 知道毒药没了狼队可以放开手打。自动化查泄漏只比对了别人的身份字符串，
            # 布尔字段就这么漏过去了，是我自己当狼玩的时候看见的。
            "antidote": self.antidote if me.role == "witch" else None,
            "poison": self.poison if me.role == "witch" else None,
            "pending_hunter": self.pending_hunter == me.id,
            "wolf_round": self.wolf_round,
            "setup": {ROLE_LABEL[k]: c for k, c in (self.setup_counts or {}).items() if c},
            # 骑士的决斗按钮挂在「白天发言」面板上，不是独立阶段，
            # 所以要单独告诉前端这一手现在能不能用。
            "can_duel": self._knight_may_duel(me.id),
            "idiot_revealed": self.idiot_revealed and me.role == "idiot",
        })
        if me.role == "guard":
            st["guard_targets"] = [
                {"id": p.id, "name": p.name} for p in self._guard_targets(me.id)
            ]
            st["last_guard_name"] = self.name(self.last_guard) if self.last_guard else ""
        # 出局后开天眼：线下狼人杀死了就能看底牌，观战才有意思。
        # 注意这是**唯一**允许下发他人身份的入口，且必须真人已出局——
        # 活着的时候漏一个字段，这局就废了。
        #
        # 但「死了」还不够：猎人的枪和出局者的遗言都是死后才轮到的操作，
        # 这两手要是能对着底牌出，那就不是玩游戏了——
        # 猎人照着名单打死一匹狼，遗言直接把全场身份念出来。
        # 所以天眼要等到你**真的没事可做**了才开。
        if not me.alive and not self._is_player_turn():
            st["god_view"] = [
                {"id": p.id, "name": p.name, "role": p.role, "role_label": p.role_label,
                 "alive": p.alive}
                for p in self.players
            ]
        # 狼人才知道同伴
        if me.role == "wolf":
            st["wolf_mates"] = [
                {"id": p.id, "name": p.name}
                for p in self._by_role("wolf", alive_only=False) if p.id != me.id
            ]
        if me.role == "seer":
            st["seer_results"] = [
                {"name": self.name(t), "result": r} for t, r in self.seer_results.items()
            ]
        if self.finished and self.result:
            st["result"] = self.result
        return st

    def brief(self) -> str:
        me = self.human
        lines = [f"# {me.role_label}", "", ROLE_DESC[me.role], ""]
        if me.role == "wolf":
            mates = [p.name for p in self._by_role("wolf", alive_only=False) if p.id != me.id]
            lines += ["## 你的同伴", "、".join(mates) or "（无）", "",
                      "夜里的商议只有狼队看得见。白天要伪装成好人。"]
        elif me.role == "seer":
            lines += ["## 验人记录"]
            lines += ([f"- **{self.name(t)}** → {r}" for t, r in self.seer_results.items()]
                      or ["- （还没验过）"])
        elif me.role == "witch":
            lines += [f"解药：{'✅ 可用' if self.antidote else '❌ 已用'}　　"
                      f"毒药：{'✅ 可用' if self.poison else '❌ 已用'}"]
        elif me.role == "guard":
            lines += [f"昨晚守的：**{self.name(self.last_guard)}**（今晚不能再守他）"
                      if self.last_guard else "还没守过人。"]
        elif me.role == "knight":
            lines += ["决斗：" + ("❌ 已用掉" if self.knight_used else "✅ 还留着一次"), "",
                      "牌只有一张，用在有实锤的时候。开牌当天不再投票。"]
        elif me.role == "idiot":
            lines += ["✅ 你还没翻牌——被投出去时会自动翻牌免死。"
                      if not self.idiot_revealed else
                      "❌ 你已经翻过牌了：还活着，但没有投票权。"]
        lines += ["", "---", "",
                  "**胜负**：狼人数 ≥ 好人数则狼人胜；狼人全部出局则好人胜。"]
        return "\n".join(lines)

    def title(self) -> str:
        return f"狼人杀 · 第{self.round}天"

    def phase_label(self) -> str:
        return {
            "night_guard": "守卫守人",
            "night_wolf": "狼队商议", "night_wolf_vote": "狼队表决", "night_seer": "预言家验人", "night_witch": "女巫用药",
            "day_discuss": "白天发言", "day_vote": "投票放逐",
            "hunter_shoot": "猎人开枪", "last_words": "遗言", "done": "已结束",
        }.get(self.phase, self.phase)

    def progress(self) -> float:
        if self.finished:
            return 1.0
        n = self.player_count or len(self.players)
        return min(0.9, (n - len(self.alive_players)) / max(2, n // 2))

    # ---- 存档 ----
    def extra_to_dict(self) -> dict:
        return {
            "queue": self.queue, "votes": self.votes, "night_kill": self.night_kill,
            "night_guard": self.night_guard, "last_guard": self.last_guard,
            "witch_saved": self._witch_saved,
            "knight_used": self.knight_used, "idiot_revealed": self.idiot_revealed,
            "wolf_votes": self.wolf_votes, "wolf_queue": self.wolf_queue,
            "wolf_round": self.wolf_round,
            "antidote": self.antidote, "poison": self.poison,
            "night_poison": self.night_poison, "seer_results": self.seer_results,
            "last_night_deaths": self.last_night_deaths,
            "player_count": self.player_count,
            "setup_counts": self.setup_counts,
            "pending_hunter": self.pending_hunter,
            "after_hunter": self.after_hunter,
            "pending_last_words": self.pending_last_words,
            "after_last_words": self.after_last_words,
        }

    def extra_from_dict(self, d: dict) -> None:
        self.queue = d.get("queue", [])
        self.votes = d.get("votes", {})
        self.night_kill = d.get("night_kill", "")
        self.wolf_votes = d.get("wolf_votes", {})
        self._witch_saved = d.get("witch_saved", "")
        self.night_guard = d.get("night_guard", "")
        self.last_guard = d.get("last_guard", "")
        self.knight_used = d.get("knight_used", False)
        self.idiot_revealed = d.get("idiot_revealed", False)
        self.wolf_queue = d.get("wolf_queue", [])
        self.wolf_round = d.get("wolf_round", 1)
        self.antidote = d.get("antidote", True)
        self.poison = d.get("poison", True)
        self.night_poison = d.get("night_poison", "")
        self.seer_results = d.get("seer_results", {})
        self.last_night_deaths = d.get("last_night_deaths", [])
        self.player_count = d.get("player_count", len(d.get("players", [])) or 9)
        self.setup_counts = d.get("setup_counts", {})
        self.pending_hunter = d.get("pending_hunter", "")
        self.after_hunter = d.get("after_hunter", "")
        self.pending_last_words = d.get("pending_last_words", "")
        self.after_last_words = d.get("after_last_words", "")
