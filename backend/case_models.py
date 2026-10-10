"""《探案》的数据模型。

和剧本杀同一条原则：**真相全部写死在数据里，代码控制机制，LLM 只负责把话说出来**。
区别在于这里是单人探案——只有你一个侦探，其余全是 NPC，
所以「谁知道什么」不再靠视角切分，而靠**证词层级**：

    tier 0  初次见面，他愿意说的
    tier 1  你摆出了某样证据，他改口后愿意说的
    tier 2  再被戳穿一次，他兜不住了

每一层都是独立的一份「他知道什么 / 他还在否认什么」，
喂给模型的永远只有**当前层**。他兜里还揣着什么，模型自己都不知道，
所以套不出来——这是这套设计最要紧的地方。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import AvatarSpec


class Hotspot(BaseModel):
    """场景里可以点的东西。"""

    id: str
    label: str                      # 「书桌抽屉」——直接当选项文字显示
    describe: str                   # 点下去看到什么——永远能看
    yields: str = ""                # 拿到哪件物证（可空）
    requires: str = ""              # 需要先持有哪件物证才能打开
    locked_text: str = ""           # 条件没满足时看到的
    once: bool = True               # 只能拿一次


class Scene(BaseModel):
    id: str
    name: str
    intro: str                      # 进门第一眼
    hotspots: list[Hotspot] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    requires: str = ""              # 需要哪件物证才能进
    locked_hint: str = ""


class Evidence(BaseModel):
    id: str
    name: str
    detail: str                     # 证物栏里的说明
    # 物证组合：两样凑一起才看得出问题
    combines_with: str = ""
    combines_into: str = ""
    combine_note: str = ""          # 组合成功时的旁白


class Testimony(BaseModel):
    """一个人在某一层愿意说的话。"""

    tier: int
    unlocked_by: str = ""           # 摆出这件证据才会进这一层（tier 0 不需要）
    reaction: str = ""              # 被摆证据的当场第一反应，脚本写死，保证一定出现
    knows: list[str] = Field(default_factory=list)     # 这一层他肯说的事实
    denies: list[str] = Field(default_factory=list)    # 这一层他还在硬撑的说法
    mood: str = ""                                     # 这一层的情绪，影响说话方式


class Person(BaseModel):
    id: str
    name: str
    role: str
    avatar: AvatarSpec | None = None
    personality: str
    speech_style: str
    is_culprit: bool = False
    testimonies: list[Testimony] = Field(default_factory=list)

    def tier_at(self, t: int) -> Testimony:
        best = None
        for x in self.testimonies:
            if x.tier <= t and (best is None or x.tier > best.tier):
                best = x
        if best is None:
            raise KeyError(f"{self.name} 没有 tier<={t} 的证词")
        return best


class AccuseOption(BaseModel):
    id: str
    text: str


class CaseSolution(BaseModel):
    culprit: str
    method: str                     # 正确的手法选项 id
    motive: str                     # 正确的动机选项 id
    method_options: list[AccuseOption]
    motive_options: list[AccuseOption]
    explanation: str                # 全对时的完整还原
    key_evidence: list[str] = Field(default_factory=list)   # 支撑判断的关键物证


class Case(BaseModel):
    case_id: str
    title: str
    tagline: str
    brief: str                      # 开场：你是谁、发生了什么
    victim: str
    detective_name: str = "你"
    author: str = ""
    scenes: list[Scene]
    people: list[Person]
    evidence: list[Evidence]
    solution: CaseSolution
    start_scene: str

    def scene(self, sid: str) -> Scene:
        for s in self.scenes:
            if s.id == sid:
                return s
        raise KeyError(f"未知场景 {sid}")

    def person(self, pid: str) -> Person:
        for p in self.people:
            if p.id == pid:
                return p
        raise KeyError(f"未知人物 {pid}")

    def ev(self, eid: str) -> Evidence:
        for e in self.evidence:
            if e.id == eid:
                return e
        raise KeyError(f"未知物证 {eid}")


CaseDifficulty = Literal["easy", "medium", "hard"]
