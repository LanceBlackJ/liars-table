"""真相 JSON 的数据模型。

分层原则（全项目最重要的不变式）：
  Script      = 上帝视角，包含 solution / is_murderer / points_to，**永不进入任何角色 Agent**
  CharacterView = 单角色视角，由 Script 纯代码切分而来，是角色 Agent 唯一能看到的东西
  PublicClue  = 线索的对外形态，只有 text 和 location，剥掉了所有元信息
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Meta(BaseModel):
    title: str
    player_count: int
    difficulty: Literal["easy", "medium", "hard"]
    estimated_minutes: int
    template: str
    tagline: str = ""
    # 幕数。默认 2 不是 1：现在的流程本来就是两幕（第一幕 → 幕间 → 第二幕 → 终局），
    # 21 个老本子都不写这个字段，默认成 1 会让它们的第二幕直接被跳过。
    # 写 3 才会多出第三幕（二次幕间 + 第三轮搜证/圆桌/质询/密谈）。
    acts: int = 2
    # 每幕每人几次搜证。不写就用引擎默认（第一幕 2 次、其余 1 次）。
    # 厚本线索多，需要拉高，不然 60 条线索一局只能搜到一半。
    search_budget: list[int] = Field(default_factory=list)
    # 标签，如「失忆本」「硬核」。仅用于选本时展示和筛选。
    tags: list[str] = Field(default_factory=list)
    # 作者署名。开放投稿之后需要知道本子是谁写的。
    author: str = ""
    # 校验格式版本。以后改格式时，老投稿靠它做迁移。
    spec_version: int = 1


class Setting(BaseModel):
    public_background: str
    map: list[str]
    time_slots: list[str]


class Case(BaseModel):
    victim: str
    death_slot: str
    death_location: str
    weapon: str
    cause: str
    body_found_by: str
    body_found_slot: str


class PersonalGoal(BaseModel):
    id: str
    text: str
    type: Literal["conceal", "acquire", "protect", "expose"]


class ActBlock(BaseModel):
    """某个角色在第 N 幕开场时多收到的一页剧本。

    三幕本的核心：第二幕不是「再搜一轮」，而是每个人收到一段**只有自己知道**的
    新信息，而且它通常会推翻他第一幕里说过的话——所有人的证词要重新对一遍。
    线下本能撑几个小时，靠的正是同一批信息被反复推翻重估，不是内容多。
    """

    act: int                                   # 从 2 起
    title: str = ""                            # 「你想起来了」「保险柜里的东西」
    text: str                                  # 追加正文，原样展示给玩家、喂给 AI
    observations: list[str] = Field(default_factory=list)   # 并入「你注意到的事」
    goals: list[PersonalGoal] = Field(default_factory=list)  # 这一幕新增的目标
    secret_add: str = ""                       # 这一幕才揭晓的、关于自己的秘密
    # 不再适用的旧目标 id。第三幕追加目标而不退休旧的，目标越堆越多，
    # 「全部达成」的概率会塌到接近零，四档判定就剩两档了。
    retract_goals: list[str] = Field(default_factory=list)
    # 这一幕起「想起来了」的时段（对应 Character.amnesia_slots）。
    restore_slots: list[str] = Field(default_factory=list)
    # 这一幕起，这个人手里多出来的随身线索（Clue.granted=true 的那些）。
    # 拿到之后他可以公开、私下给人看，或者烂在肚子里。
    grant_clues: list[str] = Field(default_factory=list)


class TruthOption(BaseModel):
    id: str
    text: str


class TruthQuestion(BaseModel):
    """分层真相里的一问。

    原来整局只有一个问题：谁是凶手。票对了就算赢。
    现在可以问：谁先动的手、谁在替他掩护、他是怎么死的……各自判分。
    全部是选择题（人 / 地点 / 自定义选项），**纯代码判分，不调 LLM**——
    胜负口径必须是确定的，这条在 engine.py 文件头就写明了。
    """

    id: str
    prompt: str
    kind: Literal["character", "location", "choice"]
    # character 型不写 options 时，默认列出所有非 NPC 角色；
    # location 型默认列出基础地图；choice 型必须自己写。
    options: list[TruthOption] = Field(default_factory=list)
    answer: str                    # 选项 id / 角色 id / 地点名
    points: int = 1
    explain: str = ""              # 复盘时展示：为什么是这个答案


class CoverStoryWeakPoint(BaseModel):
    clue_id: str
    risk: str
    fallback: str


class FakeTimelineEntry(BaseModel):
    slot: str
    claimed_location: str
    claimed_action: str


class CoverStory(BaseModel):
    """只有凶手有。预生成的假口供——不让 AI 现编谎话，是一致性的关键。"""

    # 一句话总口供。21 个本子里有 14 个的 JSON 早就写了这个字段，
    # 但模型没有声明它，pydantic 默认 extra="ignore" 就把它静静吃掉了——
    # 也就是说这 14 份精心写好的口供从来没进过 AI 的上下文。
    claim: str = ""
    fake_timeline: list[FakeTimelineEntry]
    weak_points: list[CoverStoryWeakPoint] = Field(default_factory=list)
    redirect_targets: list[str] = Field(default_factory=list)
    redirect_hook: str = ""


class AvatarSpec(BaseModel):
    """头像特征。显式声明而非随机生成——老管家必须看着像老人。

    hair:       short buzz bun long updo balding bald cap hood
    hair_color: black darkbrown gray white auburn
    skin:       light mid tan deep
    garment:    indigo crimson olive plum teal ochre slate rust moss wine
    accessory:  none glasses round_glasses mustache beard scarf goggles earrings
    """

    hair: str = "short"
    hair_color: str = "black"
    skin: str = "mid"
    garment: str = "slate"
    accessory: str = "none"


class Character(BaseModel):
    id: str
    name: str
    public_role: str
    avatar: AvatarSpec | None = None
    private_secret: str
    motive: str
    is_murderer: bool = False
    # NPC：证人/工作人员（管家、法医那一类）。不参与投票、不被投票、
    # 没有个人目标要判定，只用来被问话、扩充信息面——大侦探节目里
    # 常有的角色，但六个玩家各演各的剧本，从来没有这个位置。
    # 复用 private_secret/motive 就够：对 NPC 而言，前者是「问对了才说的关键信息」，
    # 后者可以留空。
    is_npc: bool = False
    personality: str
    speech_style: str
    # 朗读挑音色用："f" / "m"。留空则回退到按发型猜——
    # 但短发不等于男性，猜错过（女秘书被配了男声），所以能写就写明。
    voice: str = ""
    sample_lines: list[str] = Field(default_factory=list)
    personal_goals: list[PersonalGoal] = Field(default_factory=list)
    cover_story: CoverStory | None = None
    # 第二幕起每幕追加的剧本。不写 = 这个角色整局只有第一幕那一页。
    acts: list[ActBlock] = Field(default_factory=list)
    # 「断片」的时段：这个角色对这些时段**没有记忆**——自己的行踪表里这几格是空的，
    # 要靠别人的目击、现场的物证去倒推自己那会儿干了什么。
    # 对 AI 同样生效（它也不记得），所以它会老实说「我不记得」，而不是照着真相演。
    # 想起来的时段在 ActBlock.restore_slots 里写。凶手不允许有断片（要演假口供）。
    amnesia_slots: list[str] = Field(default_factory=list)


class TimelineEntry(BaseModel):
    slot: str
    char: str
    location: str
    action: str
    witnessed_by: list[str] = Field(default_factory=list)
    observed: list[str] = Field(default_factory=list)
    is_crime_action: bool = False


class Clue(BaseModel):
    id: str
    location: str
    text: str
    reveals: str = ""
    points_to: list[str] = Field(default_factory=list)
    is_red_herring: bool = False
    difficulty: int = 1
    # ---- 合成线索（把探案里最爽的那一招搬过来）----
    # 单看毫无意义，两条凑在一起才产生第三个结论。
    # 合成产物也写在 clues 里（location 留空、synth=True），
    # 不另开存储——`s.clue(id)` 到处在用，另开一套 id 空间会让六处查找全部 KeyError。
    # 谁**同时握有**所有 from_clues，谁就自动得到它——按人算，不是全局。
    # 所以最深的线索只能靠私聊交换拼出来，这正是私聊阶段该有的价值。
    synth: bool = False
    from_clues: list[str] = Field(default_factory=list)
    # 「随身线索」：不是搜出来的，也不是拼出来的，而是某个人本来就带在身上的东西——
    # 蒋医生拿走的死者手机、岑晚包里一直在录音的手机。由 ActBlock.grant_clues 发给他，
    # 或由幕间 reveal_clue 公开给所有人。location 留空，不进搜证池。
    granted: bool = False
    combine_note: str = ""         # 拼成的当场旁白：「你把 A 和 B 对到一块儿……」


class Interlude(BaseModel):
    """幕间突发事件。

    在第一幕结束后砸下来，作用是**推翻玩家刚建立起来的结论**——
    尸检改了死亡时间、有人翻供、新证物送到。没有这一下，
    整局就是「搜证→发言→投票」一条直线，太好走。

    effect：
      none           只是一段旁白
      reveal_clue    把 clue_id 那条线索直接公开给所有人
      unlock_location 地图上多出一个可搜的新地点
    """
    # 在第几幕开始时砸下来。14/21 个本子的 JSON 里早就写了 "at": "act2"，
    # 同样是模型没声明、被 pydantic 静默丢掉。声明出来是零迁移成本，
    # 而且三幕改造要靠它区分「第二幕开场」和「第三幕开场」的幕间。
    at: str = "act2"
    title: str
    text: str
    effect: Literal["none", "reveal_clue", "unlock_location"] = "none"
    clue_id: str = ""
    location: str = ""


class Solution(BaseModel):
    murderer: str
    reasoning_chain: list[str]
    minimum_clues_required: list[str]
    # 分层真相。不写 = 老样子，只有投票这一问。
    questions: list[TruthQuestion] = Field(default_factory=list)
    # 答对多少分数算「把案子还原了」。低于这条线，就算票投对了也只是惨胜。
    question_pass: float = 0.6


class Script(BaseModel):
    script_id: str
    meta: Meta
    setting: Setting
    case: Case
    characters: list[Character]
    timeline: list[TimelineEntry]
    clues: list[Clue]
    solution: Solution
    interludes: list[Interlude] = Field(default_factory=list)

    # ---- 便捷查询 ----
    def char(self, cid: str) -> Character:
        for c in self.characters:
            if c.id == cid:
                return c
        raise KeyError(f"未知角色 {cid}")

    def clue(self, kid: str) -> Clue:
        for k in self.clues:
            if k.id == kid:
                return k
        raise KeyError(f"未知线索 {kid}")

    def clues_at(self, location: str) -> list[Clue]:
        """同一地点的线索按 difficulty 升序——第 N 次搜索固定返回第 N 条，确定性。"""
        return sorted(
            [k for k in self.clues
             if k.location == location and not k.synth and not k.granted],
            key=lambda k: (k.difficulty, k.id),
        )

    @property
    def murderer_id(self) -> str:
        return self.solution.murderer


# --------------------------------------------------------------------------
# 切分产物 —— 这些才是能喂给角色 Agent 的东西
# --------------------------------------------------------------------------


class MyTimelineEntry(BaseModel):
    slot: str
    location: str
    action: str


class ActText(BaseModel):
    act: int
    title: str = ""
    text: str


class KnownOther(BaseModel):
    char_name: str
    slot: str
    fact: str


class CharacterView(BaseModel):
    """单个角色所知的全部。刻意不含 solution / 他人秘密 / 线索元信息。"""

    char_id: str
    name: str
    public_role: str
    public_background: str
    map: list[str]
    time_slots: list[str]
    case_public: dict[str, str]
    roster: list[dict[str, str]]  # 其他角色的公开身份

    private_secret: str
    motive: str
    personality: str
    speech_style: str
    sample_lines: list[str]
    personal_goals: list[PersonalGoal]

    my_timeline: list[MyTimelineEntry]
    my_observations: list[str]
    known_others: list[KnownOther]

    is_murderer: bool
    is_npc: bool = False
    cover_story: CoverStory | None = None
    # 已经解锁的追加剧本（只含 act <= 当前幕的）。未解锁的不在这里——
    # 这是分幕不泄漏的全部保证：`build_view` 切不出来的东西，哪儿都拿不到。
    acts_text: list[ActText] = Field(default_factory=list)
    # 此刻仍然「断片」的时段（已经想起来的不算）。
    gaps: list[str] = Field(default_factory=list)
    act: int = 1


class PublicClue(BaseModel):
    """线索对外形态：剥掉 reveals / points_to / is_red_herring。"""

    id: str
    location: str
    text: str
