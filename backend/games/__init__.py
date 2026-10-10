"""游戏注册表。

剧本杀（engine.py）是先写的，有自己的存档格式。这里用一个薄适配层把它接进
统一协议，而不是推倒重写——已经跑通并验证过的代码不值得为了整齐去动。
新游戏（狼人杀 / 谁是卧底）直接继承 BaseGame。
"""
from __future__ import annotations

import json
from typing import Any

from ..config import GAMES_DIR
from .base import BaseGame
from .detective import DetectiveGame
from .spy import SpyGame
from .werewolf import WerewolfGame

# 每款游戏的门面信息，前端首页用
CATALOG = [
    {
        "kind": "mystery",
        "label": "剧本杀",
        "tagline": "读本、搜证、圆桌对质。AI 扮演的凶手会撒谎，而且圆得住。",
        "players": "1 人 + 5 AI",
        "minutes": "60–110 分钟",
        "needs_script": True,
    },
    {
        "kind": "werewolf",
        "label": "狼人杀",
        "tagline": "狼人、预言家、女巫、猎人。人数和阵营都能自己配。",
        "players": "6–12 人（可配阵营）",
        "minutes": "25–45 分钟",
        "needs_script": False,
    },
    {
        "kind": "detective",
        "label": "探案",
        "tagline": "一个人的案子。看现场、翻东西、逼人改口——指认只有一次机会。",
        "players": "1 人 + 5 NPC",
        "minutes": "40–70 分钟",
        "needs_script": False,
        "needs_case": True,
    },
    {
        "kind": "spy",
        "label": "谁是卧底",
        "tagline": "你拿到一个词。没人告诉你自己是不是那个例外。",
        "players": "5–10 人",
        "minutes": "15–30 分钟",
        "needs_script": False,
    },
]


# --------------------------------------------------------------------------
# 剧本杀适配层
# --------------------------------------------------------------------------


class MysteryAdapter:
    """把 engine.Game 包装成注册表能统一调度的形态。"""

    kind = "mystery"
    kind_label = "剧本杀"

    def __init__(self, inner) -> None:
        self.inner = inner

    # 直通
    def step(self) -> dict:
        return self.inner.step()

    def act(self, action: dict) -> dict:
        return self.inner.act(action)

    def save(self) -> None:
        self.inner.save()

    @property
    def game_id(self) -> str:
        return self.inner.game_id

    @property
    def finished(self) -> bool:
        from ..engine import Phase

        return self.inner.phase == Phase.DONE

    def player_state(self) -> dict[str, Any]:
        from ..main import game_state  # 延迟导入，避免循环依赖

        st = game_state(self.inner, include_brief=True)
        st["kind"] = "mystery"
        st["kind_label"] = "剧本杀"
        return st

    def summary(self) -> dict:
        from ..engine import PHASE_LABEL, PHASE_ORDER, Phase

        g = self.inner
        # 进度必须按 PHASE_ORDER（实际流程顺序）算，不能用 list(Phase)（枚举声明顺序）。
        # 两者本来就不一致：枚举里 SEARCH_2 排在 PRIVATE_1 前面，实际流程里反过来。
        # 结果是从「幕间」走到「第二轮搜证」时，存档列表里的进度条**往回退**
        # （53% → 40%）。加了新阶段之后只会更歪。
        # 三幕本比两幕本长，进度要按**这一局自己的** plan 算；老存档没有 plan，回退全局顺序。
        order = g.plan or [x.value for x in PHASE_ORDER]
        try:
            prog = order.index(g.phase.value) / (len(order) - 1)
        except ValueError:
            prog = 0.0
        return {
            "game_id": g.game_id,
            "kind": "mystery",
            "kind_label": "剧本杀",
            "title": f"{g.script.meta.title}",
            "phase": PHASE_LABEL.get(g.phase, str(g.phase)),
            "round": 0,
            "finished": g.phase == Phase.DONE,
            "human_name": g.script.char(g.player_char_id).name,
            "created_at": getattr(g, "created_at", 0.0),
            "updated_at": getattr(g, "updated_at", 0.0),
            "progress": 1.0 if g.phase == Phase.DONE else prog,
        }


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def create(kind: str, **opts) -> Any:
    if kind == "mystery":
        from .. import engine

        g = engine.new_game(opts["script_id"], opts["player_char_id"])
        g.step()  # 立刻放出开场旁白
        w = MysteryAdapter(g)
        w.save()
        return w
    if kind == "werewolf":
        g = WerewolfGame.create(
            opts.get("human_name") or "你",
            player_count=opts.get("player_count") or 9,
            roles=opts.get("roles"),
        )
        g.save()
        return g
    if kind == "detective":
        g = DetectiveGame.create(opts.get("case_id") or "case_paint")
        g.save()
        return g
    if kind == "spy":
        g = SpyGame.create(
            opts.get("human_name") or "你",
            player_count=opts.get("player_count") or 8,
        )
        g.save()
        return g
    raise ValueError(f"未知游戏类型 {kind}")


_CLASSES: dict[str, type[BaseGame]] = {"werewolf": WerewolfGame, "spy": SpyGame}


def load(game_id: str) -> Any:
    path = GAMES_DIR / f"{game_id}.json"
    if not path.exists():
        raise KeyError(f"存档不存在: {game_id}")
    d = json.loads(path.read_text(encoding="utf-8"))
    kind = d.get("kind", "mystery")

    if kind == "detective":
        g = DetectiveGame()
        g.load_dict(d)
        return g

    if kind in _CLASSES:
        g = _CLASSES[kind]()
        g.load_base(d)
        return g

    from .. import engine

    return MysteryAdapter(engine.get_game(game_id))


def list_saves(limit: int = 60) -> list[dict]:
    """存档列表。读取失败的单个存档跳过，不能让一个坏文件拖垮整个列表。"""
    out: list[dict] = []
    for path in GAMES_DIR.glob("*.json"):
        try:
            out.append(load(path.stem).summary())
        except Exception:
            continue
    out.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return out[:limit]


def delete(game_id: str) -> bool:
    path = GAMES_DIR / f"{game_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False
