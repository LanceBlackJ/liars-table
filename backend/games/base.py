"""多游戏通用底座。

三款游戏（剧本杀 / 狼人杀 / 谁是卧底）共享的东西其实很少但很关键：

  1. **信息隔离**：每个 AI 只能看到自己该看到的。这是三款游戏的共同灵魂——
     凶手不知道别人的秘密、狼人知道同伴但预言家不知道、卧底不知道自己是卧底。
  2. **公开日志**：所有人可见的发言流，是 AI 每轮的输入。
  3. **私密日志**：只有特定玩家可见（狼人夜聊、预言家验人结果）。
  4. **回合调度**：谁该行动，纯代码决定，绝不交给 LLM。
  5. **存档**：任何时刻可存可续。

各游戏自己实现 step / act / state，底座只管这些公共设施。
"""
from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import GAMES_DIR


# --------------------------------------------------------------------------
# 玩家
# --------------------------------------------------------------------------


@dataclass
class Player:
    id: str
    name: str                    # 对外称呼。狼人杀/卧底里是「3号」这种
    display_name: str = ""       # 真实姓名，只在头像下方做次要显示
    seat: int = 0                # 座位号。真人的座位是随机的，不固定 1 号
    is_human: bool = False
    alive: bool = True
    # 阵营/身份。**绝不下发给前端**，除非游戏已结束
    role: str = ""
    role_label: str = ""
    avatar: dict[str, str] = field(default_factory=dict)
    # 描述/发言风格。谁是卧底里靠它拉开差异——同一个模型 + 同一个词，
    # 没有性格区分的话六个人会输出几乎一模一样的句子。
    style: str = ""
    # 出局信息
    out_round: int | None = None
    out_reason: str = ""


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------

# kind 取值：
#   narration     旁白（主持人）
#   system        分隔条 / 阶段提示
#   speech        角色发言
#   vote          投票
#   elimination   出局公告
#   clue_revealed 线索公开（剧本杀）
#   private       仅特定玩家可见（狼人夜聊 / 验人结果）


class EventLog:
    """公开日志 + 私密日志。私密事件带 visible_to 白名单。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._seq = 0

    def emit(self, kind: str, *, visible_to: list[str] | None = None, **payload) -> dict:
        self._seq += 1
        ev = {"seq": self._seq, "kind": kind, **payload}
        if visible_to is not None:
            ev["visible_to"] = visible_to
        self.events.append(ev)
        return ev

    def visible(self, viewer_id: str | None) -> list[dict]:
        """某个视角能看到的事件。viewer_id=None 表示上帝视角（仅复盘用）。"""
        if viewer_id is None:
            return self.events
        return [
            e for e in self.events
            if "visible_to" not in e or viewer_id in e["visible_to"]
        ]

    def public(self) -> list[dict]:
        return [e for e in self.events if "visible_to" not in e]

    def to_dict(self) -> dict:
        return {"events": self.events, "seq": self._seq}

    @classmethod
    def from_dict(cls, d: dict) -> EventLog:
        log = cls()
        log.events = d.get("events", [])
        log._seq = d.get("seq", len(log.events))
        return log


# --------------------------------------------------------------------------
# 游戏基类
# --------------------------------------------------------------------------


class BaseGame(ABC):
    kind: str = ""          # "mystery" | "werewolf" | "spy"
    kind_label: str = ""

    def __init__(self, game_id: str | None = None) -> None:
        self.game_id = game_id or uuid.uuid4().hex[:12]
        self.log = EventLog()
        self.players: list[Player] = []
        self.human_id: str = ""
        self.phase: str = ""
        self.round: int = 0
        self.finished: bool = False
        self.result: dict | None = None
        # 通用记事本。玩家随手记的东西，四款游戏都有；
        # 纯私有，不参与任何判定，也绝不喂给 AI。
        self.pad: str = ""
        self.created_at: float = time.time()
        self.updated_at: float = time.time()

    # ---- 查询 ----
    def player(self, pid: str) -> Player:
        for p in self.players:
            if p.id == pid:
                return p
        raise KeyError(f"未知玩家 {pid}")

    def name(self, pid: str) -> str:
        return self.player(pid).name

    @property
    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    @property
    def human(self) -> Player:
        return self.player(self.human_id)

    def others(self, pid: str, *, alive_only: bool = True) -> list[Player]:
        return [
            p for p in self.players
            if p.id != pid and (p.alive or not alive_only)
        ]

    # ---- 各游戏必须实现 ----
    @abstractmethod
    def step(self) -> dict:
        """推进一步（通常是一个 AI 行动）。返回 {events, waiting, action}。"""

    @abstractmethod
    def act(self, action: dict) -> dict:
        """处理玩家动作。"""

    @abstractmethod
    def player_state(self) -> dict[str, Any]:
        """玩家可见状态。**这是最后一道泄漏防线**，实现时逐字段过一遍。"""

    @abstractmethod
    def brief(self) -> str:
        """给玩家看的身份说明（markdown）。"""

    def brief_title(self) -> str:
        """左栏面板的标题。各游戏自己说了算——卧底那边是「你的词」，
        写死成「你的身份」会和 brief 正文里的标题打架。"""
        return "你的身份"

    def _is_player_turn(self) -> bool:
        """现在是不是在等真人操作。子类覆盖。

        基类给 False 而不是抛异常：没实现这个的游戏，base_state 里的
        spectating 就退回「死了就是观战」的老行为，不会因为少一个方法炸掉。
        """
        return False

    # ---- 通用状态封装 ----
    def base_state(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "phase": self.phase,
            # 各游戏自己把内部阶段名翻译成人话。忘了带这个的话，
            # 前端徽章会直接显示 night_witch 这种内部标识。
            "phase_label": self.phase_label(),
            "round": self.round,
            "finished": self.finished,
            # 人死了但局还没完 = 观战。前端据此改成一步一停地播，
            # 否则它那个 while(!waiting) 循环会把剩下整局一口气跑完，
            # 玩家眼里就是「一死就直接跳到结局」。
            #
            # 但「死了还有一手要出」不算观战——猎人的枪、出局者的遗言都是
            # 人已经死了才轮到的操作。这里不排掉的话，前端的观战分支会抢在
            # 动作面板前面返回，那一手玩家永远点不到，整局就卡在这儿。
            "spectating": (not self.finished and not self.human.alive
                           and not self._is_player_turn()),
            "brief_title": self.brief_title(),
            "pad": self.pad,
            "human_id": self.human_id,
            "log": self.log.visible(self.human_id),
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "display_name": p.display_name,
                    "seat": p.seat,
                    "alive": p.alive,
                    "is_human": p.is_human,
                    "avatar": p.avatar,
                    "out_reason": p.out_reason if not p.alive else "",
                }
                for p in self.players
            ],
        }

    def emit(self, kind: str, **kw) -> dict:
        return self.log.emit(kind, **kw)

    def say(self, pid: str, text: str, **extra) -> dict:
        return self.emit(
            "speech", speaker_id=pid, speaker_name=self.name(pid), text=text, **extra
        )

    def narrate(self, text: str) -> dict:
        return self.emit("narration", text=text)

    def divider(self, text: str) -> dict:
        return self.emit("system", text=f"—— {text} ——")

    def since(self, seq: int) -> list[dict]:
        """给前端做增量渲染：返回 seq 之后、本玩家可见的事件。"""
        return [e for e in self.log.visible(self.human_id) if e["seq"] > seq]

    def resp(self, before: int, *, waiting: bool, action: str | None) -> dict:
        return {
            "events": self.since(before),
            "waiting": waiting,
            "action": action,
            "finished": self.finished,
        }

    # ---- 存档 ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "kind": self.kind,
            "log": self.log.to_dict(),
            "players": [asdict(p) for p in self.players],
            "human_id": self.human_id,
            "phase": self.phase,
            "round": self.round,
            "finished": self.finished,
            "result": self.result,
            "pad": self.pad,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": self.extra_to_dict(),
        }

    def load_base(self, d: dict) -> None:
        self.game_id = d["game_id"]
        self.log = EventLog.from_dict(d["log"])
        self.players = [Player(**p) for p in d["players"]]
        self.human_id = d["human_id"]
        self.phase = d["phase"]
        self.round = d.get("round", 0)
        self.finished = d.get("finished", False)
        self.result = d.get("result")
        self.pad = d.get("pad", "")
        self.created_at = d.get("created_at", time.time())
        self.updated_at = d.get("updated_at", time.time())
        self.extra_from_dict(d.get("extra", {}))

    def extra_to_dict(self) -> dict:
        return {}

    def extra_from_dict(self, d: dict) -> None:
        pass

    def save(self) -> None:
        self.updated_at = time.time()
        path = GAMES_DIR / f"{self.game_id}.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ---- 存档摘要（列表页用，绝不含身份）----
    def summary(self) -> dict:
        return {
            "game_id": self.game_id,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "title": self.title(),
            "phase": self.phase_label(),
            "round": self.round,
            "finished": self.finished,
            "human_name": self.human.name if self.human_id else "",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress(),
        }

    def title(self) -> str:
        return self.kind_label

    def phase_label(self) -> str:
        return self.phase

    def progress(self) -> float:
        """0~1，存档列表画进度条用。"""
        return 1.0 if self.finished else 0.0


# --------------------------------------------------------------------------
# 通用花名册：狼人杀 / 谁是卧底 没有剧本 JSON，需要现生成人物
# --------------------------------------------------------------------------

_NAMES = [
    "苏茜", "陆昭", "顾宁", "程野", "沈流", "白桦", "江晓", "秦朗",
    "温言", "许迟", "叶棠", "方屿", "祝行", "宋辞", "阮清", "戚风",
]

_AVATARS = [
    {"hair": "short", "hair_color": "black", "skin": "mid", "garment": "indigo", "accessory": "glasses"},
    {"hair": "long", "hair_color": "darkbrown", "skin": "light", "garment": "plum", "accessory": "earrings"},
    {"hair": "buzz", "hair_color": "black", "skin": "tan", "garment": "rust", "accessory": "none"},
    {"hair": "bun", "hair_color": "black", "skin": "light", "garment": "teal", "accessory": "none"},
    {"hair": "short", "hair_color": "darkbrown", "skin": "mid", "garment": "moss", "accessory": "none"},
    {"hair": "updo", "hair_color": "auburn", "skin": "light", "garment": "wine", "accessory": "earrings"},
    {"hair": "cap", "hair_color": "black", "skin": "tan", "garment": "olive", "accessory": "beard"},
    {"hair": "long", "hair_color": "black", "skin": "mid", "garment": "slate", "accessory": "none"},
    {"hair": "balding", "hair_color": "gray", "skin": "tan", "garment": "ochre", "accessory": "mustache"},
    {"hair": "short", "hair_color": "black", "skin": "light", "garment": "crimson", "accessory": "round_glasses"},
    {"hair": "bun", "hair_color": "darkbrown", "skin": "mid", "garment": "teal", "accessory": "glasses"},
    {"hair": "buzz", "hair_color": "darkbrown", "skin": "deep", "garment": "indigo", "accessory": "none"},
]


# 描述角度。给每个 AI 分配一个不同的切入口，这是防止输出雷同的主要手段。
#
# 全部是**具体角度**，刻意不含"比喻派""情绪派"这类——试过，它们会产出
# "像是把夕阳剪下的一角缝在眼皮上"这种诗，好听但没法玩。
# 要拉开的是"从哪个方面说"，不是"说得多虚"。
DESCRIBE_STYLES = [
    "用途派：只说拿它来做什么事，不说长相、不说场合。",
    "场合派：只说什么时候、在什么地方会用到它，不说它本身什么样。",
    "外形派：只说看得见的特征——大小、形状、有没有把手之类，不说用途。",
    "触感派：只说拿在手里、碰上去是什么感觉，不说用途和外形。",
    "人群派：只说什么样的人会用它、谁用得最多，不描述东西本身。",
    "搭配派：只说它常和什么东西一起出现、放在一起，不直接说它。",
    "缺点派：只说它不方便、容易坏、让人嫌弃的那一面。",
    "损耗派：只说它多久要换一次、贵不贵、在哪买，不说它长什么样。",
    "数量派：只说一般一个家里有几个、一次用几个、多久用一次。",
    "动作派：只说用它的时候人要做什么动作，不说东西本身。",
]


def make_roster(count: int, human_name: str, rng, *, numbered: bool = True) -> list[Player]:
    """生成 count 名玩家，按座位号排序。

    numbered=True（狼人杀/谁是卧底）：对外一律称「N号」，这是线下的实际叫法，
    而且让 AI 指认某个人时不会因为名字相近而指错。
    真人的座位号是**随机**的，不固定在 1 号——固定 1 号会让 AI 学会
    "1号是真人"这种元信息。
    """
    idx = list(range(len(_NAMES)))
    rng.shuffle(idx)
    av = list(range(len(_AVATARS)))
    rng.shuffle(av)
    styles = DESCRIBE_STYLES[:]
    rng.shuffle(styles)

    human_seat = rng.randrange(count)     # 真人坐第几号，随机

    players: list[Player] = []
    ai_i = 0
    for seat in range(count):
        is_human = seat == human_seat
        num = seat + 1
        if is_human:
            label = f"{num}号"
            real = human_name or "你"
        else:
            label = f"{num}号"
            real = _NAMES[idx[ai_i % len(_NAMES)]]
        players.append(
            Player(
                id=f"p{seat}",
                name=label if numbered else (real if not is_human else (human_name or "你")),
                display_name=real,
                seat=num,
                is_human=is_human,
                avatar=_AVATARS[av[seat % len(av)] % len(_AVATARS)],
                style="" if is_human else styles[ai_i % len(styles)],
            )
        )
        if not is_human:
            ai_i += 1
    return players
