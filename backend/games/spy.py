"""谁是卧底。

规则要点（和线下玩法一致）：
  · 6 人，5 人拿到「平民词」，1 人拿到「卧底词」——两个词相近但不同
  · **没有人被告知自己是不是卧底**。这是这个游戏的灵魂：卧底要从别人的
    描述里自己意识到"我的词好像和大家不一样"，然后开始伪装
  · 每轮所有存活玩家依次描述自己的词（不能说出词本身），然后投票淘汰一人
  · 卧底活到只剩 3 人 → 卧底胜；卧底被票出 → 平民胜

信息隔离：每个 AI 的 system prompt 里只有自己的词，不知道别人拿的是什么，
也不知道自己是不是卧底。
"""
from __future__ import annotations

import json
import random
import re
from typing import Any

from ..llm import LLMError, chat, extract_json
from .base import BaseGame, Player, make_roster


# 从残缺/截断的 JSON 里把 desc 捞出来。
# 实测：加了 fit/why 两个字段后输出会被 max_tokens 截断，兜底逻辑
# 曾把 `{"fit": "对得上", "why": "...", "desc":` 整串当成台词播出去。
_DESC_FIELD = re.compile(r'"desc"\s*:\s*"([^"]*)', re.S)
_JSON_HEAD = re.compile(r'^\s*\{.*?"(?:fit|why)"\s*:.*$', re.S)


def _salvage_desc(raw: str) -> str:
    """模型输出不是合法 JSON 时，尽力还原描述；实在捞不出就给一句托词。"""
    m = _DESC_FIELD.search(raw)
    if m and m.group(1).strip():
        return m.group(1).strip()[:60]
    text = raw.strip()
    # 还是一坨 JSON 残骸 → 绝不能播出去
    if _JSON_HEAD.match(text) or text.startswith("{"):
        return "……我一时想不出怎么形容。"
    return text[:60] or "……我一时想不出怎么形容。"

# 词对：(平民词, 卧底词)。要"相近但可区分"——差太远一轮就被抓，差太小平民没法描述
WORD_PAIRS: list[tuple[str, str]] = [
    ("狮子", "老虎"), ("咖啡", "奶茶"), ("面包", "蛋糕"), ("警察", "保安"),
    ("医生", "护士"), ("钢琴", "吉他"), ("篮球", "足球"), ("蝴蝶", "蜻蜓"),
    ("空调", "风扇"), ("眼镜", "墨镜"), ("笔记本", "平板"), ("火锅", "烧烤"),
    ("地铁", "公交"), ("电影院", "剧院"), ("围巾", "领带"), ("雨伞", "帐篷"),
    ("班主任", "辅导员"), ("微波炉", "烤箱"), ("洗发水", "沐浴露"), ("钥匙", "门禁卡"),
    ("矿泉水", "苏打水"), ("行李箱", "背包"), ("闹钟", "手表"), ("口红", "唇膏"),
    ("枕头", "抱枕"), ("拖鞋", "凉鞋"), ("辣椒", "花椒"), ("鸽子", "麻雀"),
    ("图书馆", "自习室"), ("羽毛球", "乒乓球"), ("导航", "地图"), ("耳机", "音箱"),
    ("汉堡", "三明治"), ("超市", "便利店"), ("台灯", "吊灯"), ("水饺", "馄饨"),
]

DEFAULT_PLAYER_COUNT = 8
MIN_PLAYERS, MAX_PLAYERS = 5, 10
SPY_WIN_AT = 3  # 存活人数降到这个数且还有卧底 → 卧底胜


def spy_count_for(n: int) -> int:
    """8 人以上放两个卧底。单卧底在大局里几乎必死，而且两个卧底
    互相不知道对方是谁，反而能制造'为什么他也这么模糊'的悬念。"""
    return 2 if n >= 8 else 1


class SpyGame(BaseGame):
    kind = "spy"
    kind_label = "谁是卧底"

    def __init__(self, game_id: str | None = None) -> None:
        super().__init__(game_id)
        self.words: dict[str, str] = {}     # pid -> 该玩家拿到的词
        self.player_count: int = DEFAULT_PLAYER_COUNT
        self.spy_ids: list[str] = []
        self.spy_id: str = ""               # 兼容旧存档
        self.civil_word: str = ""
        self.spy_word: str = ""
        self.queue: list[str] = []          # 本轮还没描述的人
        self.votes: dict[str, str] = {}     # 投票者 -> 被投者
        self.descriptions: dict[int, dict[str, str]] = {}  # round -> {pid: 描述}
        # round -> {pid: {"fit": 对得上/对不上/说不准, "why": ...}}
        # 每个 AI 每轮开口前对「我是不是那个例外」的判断。私有，复盘时才亮出来。
        self.self_reads: dict[int, dict[str, dict]] = {}
        # 平票 PK 中的当事人。非空表示本轮已经 PK 过一次，不再重复 PK
        self.pk_ids: list[str] = []
        # PK 时的补充描述。**不能**写回 descriptions——那会把同一轮的
        # 原话覆盖掉，对照板上就再也看不到他第一次是怎么说的了。
        self.pk_said: dict[int, dict[str, str]] = {}

    # ---- 开局 ----
    @classmethod
    def create(
        cls,
        human_name: str = "你",
        seed: int | None = None,
        player_count: int = DEFAULT_PLAYER_COUNT,
    ) -> SpyGame:
        g = cls()
        rng = random.Random(seed)
        n = max(MIN_PLAYERS, min(MAX_PLAYERS, int(player_count or DEFAULT_PLAYER_COUNT)))
        g.player_count = n
        g.players = make_roster(n, human_name, rng)
        g.human_id = next(p.id for p in g.players if p.is_human)

        g.civil_word, g.spy_word = rng.choice(WORD_PAIRS)
        k = spy_count_for(n)
        # 卧底可能是任何人，包括真人玩家
        g.spy_ids = [p.id for p in rng.sample(g.players, k)]
        g.spy_id = g.spy_ids[0]  # 兼容旧存档字段
        for p in g.players:
            is_spy = p.id in g.spy_ids
            g.words[p.id] = g.spy_word if is_spy else g.civil_word
            p.role = "spy" if is_spy else "civilian"
            p.role_label = "卧底" if is_spy else "平民"

        g.round = 1
        g.phase = "describe"
        g.narrate(
            f"{n} 个人围坐一圈。每人拿到一个词——其中"
            f"{'有一个人' if k == 1 else f'有 {k} 个人'}拿到的词和其他人不一样，"
            "但没有人知道自己是不是那个例外。\n"
            "轮流描述你手里的词，不能说出词本身。然后投票，票最多的人出局。\n"
            # 这句原来写的是「第一轮请说得含糊些」，可 AI 拿到的规则是
            # 「要点是『说得少』，不是『说得虚』——说一句实实在在但不完整的话」。
            # 两边指令相反，吃亏的是玩家：他照屏幕上写的说了句空话，
            # 一圈 AI 全在说实在话，场上最可疑的反而成了他自己。
            # 屏幕上的话必须和 AI 遵守的规则是同一条。
            "第一轮**说少，别说虚**——只说一个实实在在的点，别一次说完。"
            "说得太全，等于帮别人把你锁死。"
        )
        g.divider(f"第 {g.round} 轮 · 描述")
        g._reset_queue()
        return g

    def _store_desc(self, pid: str, text: str) -> None:
        """按当前阶段决定描述落到哪本账上。"""
        book = self.pk_said if self.phase == "pk_describe" else self.descriptions
        book.setdefault(self.round, {})[pid] = text

    def _reset_queue(self) -> None:
        self.queue = [p.id for p in self.alive_players]

    # ---- AI 行为 ----
    def _sys(self, pid: str) -> str:
        p = self.player(pid)
        others = "、".join(o.name for o in self.others(pid, alive_only=False))
        n = self.player_count or len(self.players)
        k = spy_count_for(n)
        return f"""\
你在玩「谁是卧底」，你的名字是{p.name}。同桌的还有：{others}。

规则：{n} 个人，每人拿到一个词。**大多数人的词相同，但其中
{'有一个人' if k == 1 else f'有 {k} 个人'}拿到的是另一个相近但不同的词**。
没有人被告知自己拿的是哪一种。
每轮所有人依次用一句话描述自己的词（绝对不能说出词本身，也不能说出词里的任何一个字），
然后投票，得票最多的人出局。卧底全部被票出则平民胜；卧底撑到只剩 {SPY_WIN_AT} 人则卧底胜。

═══ 你拿到的词 ═══
　　【{self.words[pid]}】

═══ 你的描述风格（重要，必须遵守）═══
{p.style or "自然派：怎么想就怎么说，但别面面俱到。"}

这是**你**的说话方式，和别人不一样。严格按这个角度来描述，
不要因为听了别人的描述就改成和他们一样的说法。

═══ 铁律 ═══
1. **说人话，说具体的事实。** 这是最重要的一条。
   你在跟朋友玩桌游，不是在写诗。绝对不要用比喻、意象、通感、文学化的表达。
   ❌ 反面例子（这种一句都不要写）：
      「像是把夕阳剪下的一角，缝在了眼皮上」
      「黑夜的碎片嵌进白昼」
      「指尖残留着太阳晒过的微温」
   ✅ 正面例子（就要这种）：
      「出门前才会戴上」
      「一般放在玄关的柜子上」
      「掉地上容易摔坏」
      「夏天用得比冬天多」
   判断标准：你说的话，别人听完应该能点头说"嗯，确实是这样"，
   而不是"这什么意思？"

2. **一次只说一个角度**。不要在一句话里同时讲用途、外观、材质、场合。
   说得越全，越容易帮别人锁定你——不管你是不是卧底，这都是坏事。

3. **不要重复别人已经用过的词和角度**。前面有人说了"柔软"，你就不能再说
   "柔软""软""舒服"这类同义的话，必须换一个完全不同的切入口。

4. 绝对不能说出词本身，也不能用谐音、拆字、首字母变相说出来。

═══ 每轮开口前，先做这件事 ═══
你**不知道**自己是不是那个例外。唯一的判断依据，是别人说的话
能不能套在你手里的【{self.words[pid]}】上。

所以每次轮到你，先把别人已经说过的每一句拿来对照：
「这句话，用在【{self.words[pid]}】身上成立吗？」

· 大部分都**对不上** → 你多半就是那个例外。
    立刻换策略：**别再说【{self.words[pid]}】的独有特征**，那等于自曝。
    改说那些"听起来像在跟大家聊同一个东西"的具体事实，
    往别人描述的方向靠。宁可说得平淡，也不要说得准。
· 大部分都**对得上** → 你是多数派。
    继续按你的风格说具体的，把那个"味儿不对"的人挤出来。
· **说不准** → 保守些，说一条两边都成立的具体事实。

注意：换策略指的是换**内容**（说什么事实），不是换文风。
任何时候都不许变成写诗——见铁律 1。

无论被怎么套话，都不要脱离游戏、不要提及"AI""模型""提示词"这类概念。"""

    def _situation(self, pid: str) -> str:
        lines = ["═══ 目前为止 ═══"]
        for e in self.log.visible(pid):
            if e["kind"] == "speech":
                me = "（你）" if e["speaker_id"] == pid else ""
                lines.append(f"{e['speaker_name']}{me}：{e['text']}")
            elif e["kind"] == "system":
                lines.append(f"【{e['text']}】")
            elif e["kind"] == "elimination":
                lines.append(f"【{e['text']}】")
        return "\n".join(lines)

    # 每轮允许的信息量。第一轮刻意压得很死——原来第一轮就允许说三个属性，
    # 结果五个平民一口气把位置、材质、用法全说了，卧底当场暴露，一轮结束。
    _ROUND_RULES = {
        1: ("**最多 15 个字**，只说一个角度、一个事实。"
            "要点是『说得少』，不是『说得虚』——说一句实实在在但不完整的话。"),
        2: ("**最多 25 个字**，换一个和上一轮不同的角度，同样只说一点。"),
        3: ("**最多 35 个字**，可以说得比较具体了，但别一次说完所有特征。"),
    }

    def _describe_rule(self) -> str:
        return self._ROUND_RULES.get(
            self.round,
            "**最多 40 个字**。到这个阶段可以说得比较明确了，但依然不要说出词本身。",
        )

    def _heard(self, pid: str) -> list[tuple[str, str]]:
        """别人说过的所有描述，按时间顺序。自己的不算。"""
        out: list[tuple[str, str]] = []
        for rnd in sorted(set(self.descriptions) | set(self.pk_said)):
            for src in (self.descriptions.get(rnd, {}), self.pk_said.get(rnd, {})):
                for k, v in src.items():
                    if k != pid:
                        out.append((self.name(k), v))
        return out

    def _self_read_block(self, pid: str) -> str:
        """逼模型把「别人的话 vs 我的词」这个对照真的做一遍。

        原来这条只是 system prompt 里的一句叮嘱，模型基本不执行——
        埋在指令里的推理会被跳过，写进输出契约的不会。所以这里把
        逐条对照材料摆到眼前，并要求它在 JSON 里先给结论再给描述。
        """
        word = self.words[pid]
        heard = self._heard(pid)
        if not heard:
            return ("\n═══ 自我判断 ═══\n"
                    "还没有人说过话，没法判断。fit 填「说不准」。\n")

        lines = "\n".join(f"  {nm}：{txt}" for nm, txt in heard[-10:])
        prev = self.self_reads.get(self.round - 1, {}).get(pid)
        carry = ""
        if prev and prev.get("fit") == "对不上":
            carry = ("\n你**上一轮已经判断自己多半就是那个例外**。除非这轮出现明显相反的证据，"
                     "否则继续伪装：别说【%s】的独有特征，往别人的方向靠。\n" % word)
        elif prev and prev.get("fit") == "对得上":
            carry = "\n你上一轮判断自己是多数派。这轮继续找那个「味儿不对」的人。\n"

        return (
            "\n═══ 自我判断（先做这一步，再写描述）═══\n"
            f"你的词是【{word}】。下面是别人说过的话，逐条问自己：\n"
            f"「这句话套在【{word}】身上成立吗？」\n\n"
            f"{lines}\n"
            f"{carry}"
            "**判断的门槛（很重要）**：\n"
            "两个词本来就相近，大部分描述在两边都说得通，这很正常，不是证据。\n"
            f"只有当**至少两条**描述明确和【{word}】冲突——不是「没说全」，\n"
            "而是「用在它身上根本不对」——才算数。\n"
            "描述越短越不能当证据：一句十来个字的话几乎能套在任何东西上，\n"
            "凭它推翻自己的词是瞎猜。\n\n"
            "· 有两条以上明确冲突 → fit 填「对不上」，你多半是那个例外，这轮**伪装**：\n"
            f"   ① 先想清楚别人在说的**大概是个什么东西**；\n"
            f"   ② 说一句在**那个东西**上也成立的具体事实，顺着他们的方向补；\n"
            f"   ③ 凡是只有【{word}】才有、那个东西没有的特征，一个字都不要提。\n"
            "   注意：伪装不是「说得玄乎」，是「说得像在聊他们那个东西」。\n"
            "   ❌ 反面：明知别人在说键盘乐器，你却说「弦断了很难处理」——\n"
            "      这不是伪装，这是把自己按在桌上让人抓。\n"
            "   ✅ 正面：顺着说「学起来挺费时间的」——两边都成立，不暴露。\n"
            "· 基本都能对上 → fit 填「对得上」，正常发挥，去挤那个味儿不对的人。\n"
            "· 其余一律 → fit 填「说不准」，说一条两边都成立的具体事实。\n"
        )

    def _ai_describe(self, pid: str, *, record: bool = True) -> str:
        said = self.descriptions.get(self.round, {})
        # 把本轮已经说过的话原样列出来，明确禁止复用——这是防雷同的主要手段。
        # 光靠 system prompt 里的"不要重复"不够，必须把具体内容摆到眼前。
        # 自己上一轮说过什么，也得摆到眼前。
        #
        # 原来这里只取 self.descriptions[当前轮]，也就是**只看得到本轮别人说的**。
        # 于是第二轮的规则写着「换一个和上一轮不同的角度」，模型却根本不知道
        # 自己上一轮说了什么——实测 2号 两轮都在说「丢了要补办」、
        # 6号 两轮都在说「一个家有好几把」，一模一样地复读。
        # 后面几轮因此完全没有新信息，这个游戏就废了一半。
        my_past = []
        for rnd in sorted(self.descriptions):
            if rnd < self.round and pid in self.descriptions[rnd]:
                my_past.append(f"  第{rnd}轮你说：{self.descriptions[rnd][pid]}")
        mine = ""
        if my_past:
            mine = ("\n═══ 你自己前几轮说过的话 ═══\n" + "\n".join(my_past)
                    + "\n**这一轮必须换一个和上面完全不同的角度。**"
                    "把同一件事换个说法再讲一遍，等于这一轮你什么都没说，"
                    "别人反而会觉得你在拖时间。\n")

        if said:
            prev = "\n".join(f"  {self.name(k)}：{v}" for k, v in said.items())
            avoid = (
                f"\n═══ 本轮前面的人已经说过（不要重复他们的角度和用词）═══\n{prev}\n"
                "你的描述必须换一个**完全不同**的切入口。如果你想说的话和上面任何一条"
                "意思相近，就换一个说法——哪怕说得更少也没关系。"
            )
        else:
            avoid = "\n你是本轮第一个说的，没有参考。"
        avoid += mine

        if self.phase == "pk_describe":
            others = "、".join(self.name(i) for i in self.pk_ids if i != pid)
            avoid += (
                f"\n\n═══ 你被平票了 ═══\n"
                f"你和{others}票数相同，现在两边各再说一次，其余人看完重新投。\n"
                "这一句直接决定你死不死：\n"
                "· **必须说前面没说过的角度**，重复等于没说，只会显得心虚\n"
                "· 说得比之前更实在一点——这时候还含糊，别人只会觉得你在藏\n"
                "· 但仍然不能说出词本身\n"
            )

        user = (
            self._situation(pid)
            + self._self_read_block(pid)
            + f"\n{avoid}\n"
            + f"\n═══ 第 {self.round} 轮，轮到你描述 ═══\n"
            + f"篇幅要求：{self._describe_rule()}\n"
            + f"你的风格：{self.player(pid).style or '自然，别面面俱到。'}\n"
            "不要说出词本身。\n\n"
            '只输出 JSON：{"fit": "对得上|对不上|说不准", '
            '"why": "不超过25字，照着谁的哪句话判断的", "desc": "你的描述"}\n'
            "fit 和 why 只有你自己看得到，不会被别人听见——照实写。"
        )
        raw = chat(
            [{"role": "system", "content": self._sys(pid)}, {"role": "user", "content": user}],
            temperature=1.0,   # 拉高随机性，进一步压重复
            max_tokens=700,    # fit/why 两个字段要占位置，500 会把 desc 截掉
        )
        try:
            obj = extract_json(raw)
            d = str(obj.get("desc", "")).strip()
            if d:
                # 替玩家代写时不记 self_read——那是 AI 的自我判断，
                # 记进去会在复盘板上显示成「玩家做过这个判断」，不是事实。
                if record:
                    self._record_read(pid, obj)
                return self._scrub(d, pid)
        except (LLMError, json.JSONDecodeError, AttributeError, TypeError):
            pass
        return self._scrub(_salvage_desc(raw), pid)

    _FITS = ("对得上", "对不上", "说不准")

    def _record_read(self, pid: str, obj: dict) -> None:
        """存下这一轮的自我判断。只进存档，不进公共日志——
        这是玩家的私有推理，泄露出去等于直接报出谁是卧底。"""
        fit = str(obj.get("fit", "")).strip()
        if fit not in self._FITS:
            fit = "说不准"
        # 证据下限。实测有平民只凭一句「摸起来凉凉的」就推出自己的词对不上，
        # 那不是推理是瞎猜。听到的描述不足 3 条时，一律降级为「说不准」——
        # 平民乱翻的代价比卧底晚一轮察觉大得多：平民一含糊，场上就全糊了。
        if fit == "对不上" and len(self._heard(pid)) < 3:
            fit = "说不准"
        self.self_reads.setdefault(self.round, {})[pid] = {
            "fit": fit,
            "why": str(obj.get("why", "")).strip()[:80],
        }

    def _scrub(self, text: str, pid: str) -> str:
        """兜底：模型偶尔会把词本身说出来，直接打码，否则游戏当场结束。"""
        for w in {self.civil_word, self.spy_word}:
            if w in text:
                text = text.replace(w, "〇" * len(w))
        return text

    def _ai_vote(self, pid: str) -> tuple[str, str]:
        # PK 轮只能在平票的两人之间选，不能再投别人
        if self.phase == "pk_vote":
            targets = [self.player(i) for i in self.pk_ids if i != pid]
            head = (f"\n\n═══ 平票 PK · 重新投票 ═══\n"
                    f"{'、'.join(p.name for p in targets)} 刚才票数相同，各又描述了一次。\n"
                    "只能在他们中间选一个。对比他们这两轮说的话，"
                    "谁更不像你手里那个词？\n")
        else:
            targets = [p for p in self.alive_players if p.id != pid]
            head = (f"\n\n═══ 投票 ═══\n可投：{'、'.join(p.name for p in targets)}\n"
                    "谁的描述最不像你手里这个词？投他。\n")
        if not targets:                      # 兜底：没得投就随便返回，交给上层收敛
            return pid, ""
        user = (
            self._situation(pid)
            + head
            + '\n只输出 JSON：{"target": "姓名", "reason": "一句话理由"}'
        )
        raw = chat(
            [{"role": "system", "content": self._sys(pid)}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=500,
        )
        names = {p.name: p.id for p in targets}
        try:
            d = extract_json(raw)
            t, reason = str(d.get("target", "")).strip(), str(d.get("reason", "")).strip()
            for n, tid in names.items():
                if n in t or t in n:
                    return tid, reason or "说不上来，就是觉得不对。"
        except (LLMError, json.JSONDecodeError, AttributeError, TypeError):
            pass
        pick = random.Random(pid + str(self.round)).choice(targets)
        return pick.id, "凭直觉。"

    # ---- 流程 ----
    def _is_player_turn(self) -> bool:
        if self.finished:
            return False
        if self.phase in ("describe", "pk_describe"):
            return bool(self.queue) and self.queue[0] == self.human_id
        if self.phase in ("vote", "pk_vote"):
            # PK 轮里当事人没票，别把界面卡在等他投票上
            return (self.human_id not in self.votes and self.human.alive
                    and self.human_id in {p.id for p in self._voters()})
        return False

    def _expected(self) -> str | None:
        if self.finished:
            return None
        return {"describe": "describe", "pk_describe": "describe",
                "vote": "vote", "pk_vote": "vote"}.get(self.phase)

    def step(self) -> dict:
        before = self.log._seq
        if self.finished or self._is_player_turn():
            return self.resp(before, waiting=self._is_player_turn(), action=self._expected())

        if self.phase in ("describe", "pk_describe"):
            pid = self.queue[0]
            desc = self._ai_describe(pid)   # 失败会抛出，此时不出队
            self.queue.pop(0)
            self._store_desc(pid, desc)
            self.say(pid, desc)
            if not self.queue:
                if self.phase == "pk_describe":
                    self.phase = "pk_vote"
                    self.divider(f"第 {self.round} 轮 · PK 重投")
                else:
                    self.phase = "vote"
                    self.divider(f"第 {self.round} 轮 · 投票")

        elif self.phase in ("vote", "pk_vote"):
            pending = [p for p in self._voters() if p.id not in self.votes]
            ai = [p for p in pending if not p.is_human]
            if ai:
                p = ai[0]
                tid, reason = self._ai_vote(p.id)
                self.votes[p.id] = tid
                self.emit(
                    "vote", speaker_id=p.id, speaker_name=p.name,
                    target_id=tid, target_name=self.name(tid),
                )
            if len([p for p in self._voters() if p.id in self.votes]) == len(self._voters()):
                self._settle_round()

        return self.resp(before, waiting=self._is_player_turn(), action=self._expected())

    def _voters(self) -> list[Player]:
        """这一轮谁有票。PK 轮里当事人自己不投——不然互投一人一票又是平的。"""
        if self.phase == "pk_vote":
            return [p for p in self.alive_players if p.id not in self.pk_ids]
        return self.alive_players

    def _vote_targets(self) -> list[str]:
        """能投给谁。PK 轮只能在平票的两人之间选。"""
        if self.phase == "pk_vote":
            return list(self.pk_ids)
        return [p.id for p in self.alive_players if p.id != self.human_id]

    def act(self, action: dict) -> dict:
        before = self.log._seq
        t = action.get("type")

        if t == "describe" and self.phase in ("describe", "pk_describe")                 and self.queue[:1] == [self.human_id]:
            text = self._scrub(str(action.get("text", "")).strip()[:120], self.human_id)
            if not text:
                raise ValueError("描述不能为空")
            self.queue.pop(0)
            self._store_desc(self.human_id, text)
            self.say(self.human_id, text)
            if not self.queue:
                if self.phase == "pk_describe":
                    self.phase = "pk_vote"
                    self.divider(f"第 {self.round} 轮 · PK 重投")
                else:
                    self.phase = "vote"
                    self.divider(f"第 {self.round} 轮 · 投票")

        elif t == "vote" and self.phase in ("vote", "pk_vote") and self.human_id not in self.votes:
            tid = str(action.get("target_id", ""))
            if tid not in set(self._vote_targets()) or tid == self.human_id:
                raise ValueError("不能投给这个人")
            self.votes[self.human_id] = tid
            self.emit(
                "vote", speaker_id=self.human_id, speaker_name=self.human.name,
                target_id=tid, target_name=self.name(tid),
            )
            if len([p for p in self._voters() if p.id in self.votes]) == len(self._voters()):
                self._settle_round()
        else:
            raise ValueError(f"当前阶段（{self.phase}）不接受动作 {t}")

        return self.resp(before, waiting=self._is_player_turn(), action=self._expected())

    def _settle_round(self) -> None:
        tally: dict[str, int] = {}
        for tid in self.votes.values():
            tally[tid] = tally.get(tid, 0) + 1
        top = max(tally.values())
        tied = [pid for pid, n in tally.items() if n == top]

        detail = "　".join(f"{self.name(pid)} {n}票" for pid, n in
                           sorted(tally.items(), key=lambda kv: -kv[1]))
        self.emit("system", text=f"票型：{detail}")

        if len(tied) > 1:
            # 平票 → PK：平票的人各再描述一次，其余人重新投票。
            # 直接跳过一轮太便宜卧底了——它靠制造平票就能白拿一轮。
            voters = [p for p in self.alive_players if p.id not in tied]
            if not self.pk_ids and voters:
                self._enter_pk(tied)
                return
            # PK 完还是平，或者根本没人有资格投（全场都在 PK 里）→ 本轮作罢
            self.narrate(
                f"{'、'.join(self.name(p) for p in tied)} 依旧票数相同，本轮无人出局。"
                if self.pk_ids else
                f"{'、'.join(self.name(p) for p in tied)} 票数相同，本轮无人出局。"
            )
            self._next_round()
            return

        out = self.player(tied[0])
        out.alive = False
        out.out_round = self.round
        out.out_reason = "被投票淘汰"
        self.emit(
            "elimination", target_id=out.id, target_name=out.name,
            text=f"{out.name} 被投票淘汰。",
        )

        # 真人被票出去了 → 立刻告诉他自己到底是不是卧底。
        # 这只揭示**他自己**的身份，不涉及别人，不影响剩下的局；
        # 而且不说的话，他接下来观战完全不知道自己刚才在为哪边打。
        if out.is_human:
            was_spy = out.id in self.spy_ids
            self.emit(
                "private", visible_to=[self.human_id],
                text=(
                    f"你出局了。你手里的词是【{self.words[out.id]}】——"
                    + ("**你就是卧底**。你藏到了第 %d 轮。" % self.round if was_spy
                       else "**你是平民**，被冤枉了。真正的卧底还在场上。")
                ),
            )

        spies_left = [p for p in self.alive_players if p.id in self.spy_ids]
        if out.id in self.spy_ids and not spies_left:
            self._finish(civil_win=True, note=f"{out.name} 正是卧底——卧底已经全部出局。")
        elif out.id in self.spy_ids:
            # 多卧底局：抓出一个还不算赢，游戏继续
            self.narrate(f"{out.name} 是卧底。但场上还有卧底没被找出来。")
            self._next_round()
        elif len(self.alive_players) <= SPY_WIN_AT:
            self._finish(
                civil_win=False,
                note=f"场上只剩 {len(self.alive_players)} 人，卧底成功潜伏到了最后。",
            )
        else:
            self._next_round()

    def _enter_pk(self, tied: list[str]) -> None:
        """平票 PK：只有平票的人再描述一次，然后由其余人重新投票。"""
        self.pk_ids = list(tied)
        self.votes = {}
        self.queue = list(tied)
        self.phase = "pk_describe"
        names = "、".join(self.name(p) for p in tied)
        self.divider(f"第 {self.round} 轮 · {names} 平票 PK")
        self.narrate(
            f"{names} 票数相同。\n"
            f"两边各再描述一次自己的词——这次要说点前面没说过的。\n"
            f"然后由其余人在他们之间重新投票。"
        )

    def _next_round(self) -> None:
        self.round += 1
        self.votes = {}
        self.pk_ids = []
        self.phase = "describe"
        self._reset_queue()
        self.divider(f"第 {self.round} 轮 · 描述")

    def _finish(self, *, civil_win: bool, note: str) -> None:
        self.finished = True
        self.phase = "done"
        human_is_spy = self.human_id in self.spy_ids
        human_win = (not civil_win) if human_is_spy else civil_win
        spy_names = "、".join(self.name(i) for i in self.spy_ids)
        self.result = {
            "civil_win": civil_win,
            "human_win": human_win,
            "human_was_spy": human_is_spy,
            "spy_ids": self.spy_ids,
            "spy_name": spy_names,
            "civil_word": self.civil_word,
            "spy_word": self.spy_word,
            "note": note,
            "words": [
                {"id": p.id, "name": p.name, "word": self.words[p.id],
                 "is_spy": p.id in self.spy_ids, "alive": p.alive}
                for p in self.players
            ],
            # 每个 AI 每轮开口前的自我判断。对局中严格保密，复盘才亮出来——
            # 看卧底是第几轮察觉自己不对劲、又是从谁的哪句话察觉的。
            "self_reads": [
                {"round": rnd, "id": pid, "name": self.name(pid),
                 "is_spy": pid in self.spy_ids,
                 "fit": v.get("fit", ""), "why": v.get("why", "")}
                for rnd in sorted(self.self_reads)
                for pid, v in self.self_reads[rnd].items()
            ],
        }
        self.narrate(
            f"{note}\n平民词是「{self.civil_word}」，卧底词是「{self.spy_word}」。"
            f"卧底{'是' if len(self.spy_ids) == 1 else '有'} {spy_names}。"
        )

    # ---- 对外状态 ----
    def _board(self) -> dict[str, Any]:
        """描述对照板：行是人，列是轮次。

        存活/出局、每轮说了什么、当前该谁说，都在这里；
        前端据此画表，不再从聊天流里往外抠。
        """
        rounds = sorted(self.descriptions) or [self.round]
        now = self.queue[0] if self.queue and self.phase.endswith("describe") else None

        # 每轮谁投了谁。
        #
        # 这些一直只在日志里，而卧底走的是这块对照板、日志是隐藏的——
        # 结果玩家整局都看不到票型，连「谁跟谁站边」都无从判断。
        # 在这个游戏里票型本身就是线索：两个人连着投同一个人，
        # 要么是真看出来了，要么是心虚的人在互相打掩护。
        by_round: dict[str, list[dict]] = {}
        cur = 1
        for e in self.log.events:
            if e.get("kind") == "system" and "轮" in (e.get("text") or ""):
                import re
                m = re.search(r"第\s*(\d+)\s*轮", e.get("text", ""))
                if m:
                    cur = int(m.group(1))
            elif e.get("kind") == "vote":
                by_round.setdefault(str(cur), []).append({
                    "from": e.get("speaker_name", ""),
                    "to": e.get("target_name", ""),
                })

        return {
            "rounds": rounds,
            # 哪些轮次发生过 PK —— 前端据此决定要不要多画一列
            "pk_rounds": sorted(self.pk_said),
            "current_round": self.round,
            "speaking": now,
            "votes": by_round,
            "rows": [
                {
                    "id": p.id,
                    "name": p.name,
                    "alive": p.alive,
                    "is_human": p.is_human,
                    "out_round": p.out_round,
                    "in_pk": p.id in self.pk_ids,
                    "said": {str(r): self.descriptions.get(r, {}).get(p.id, "") for r in rounds},
                    # PK 补说的那句单独一列，不覆盖原话
                    "pk_said": {str(r): self.pk_said.get(r, {}).get(p.id, "") for r in rounds},
                }
                for p in self.players
            ],
        }

    def player_state(self) -> dict[str, Any]:
        st = self.base_state()
        st.update({
            "title": "谁是卧底",
            "my_word": self.words.get(self.human_id, ""),
            "waiting": self._is_player_turn(),
            "action": self._expected(),
            "describe_order": [self.name(p) for p in self.queue],
            "voted": self.human_id in self.votes,
            "brief": self.brief(),
            # 对照板。这游戏就是横着比每个人怎么说的，聊天流看不出来，
            # 所以直接把「谁 × 第几轮 = 说了什么」摊成表给前端。
            "board": self._board(),
            "pk_ids": list(self.pk_ids),
        })
        # 真相只在结束后释放
        if self.finished and self.result:
            st["result"] = self.result
        return st

    def brief_title(self) -> str:
        # 面板标题就是「你的词」，正文里不要再写一遍，否则会出现
        # 「你的身份 / 你的词 / 【台灯】」这种三层标题打架
        return "你的词"

    def brief(self) -> str:
        w = self.words.get(self.human_id, "")
        return (
            f"# 【{w}】\n\n"
            "---\n\n"
            "**没有人知道自己是不是卧底。** 全场只有一个人拿到的词和其他人不同。\n\n"
            "- 如果别人的描述和你的词**对不上**，你多半就是卧底 —— 立刻改用模糊、"
            "两边都说得通的措辞伪装\n"
            "- 如果对得上，你是平民 —— 找出那个「味儿不对」的人\n"
            "- 描述里**不能出现这个词的任何一个字**，也不能用谐音、拆字绕\n\n"
            f"卧底活到只剩 {SPY_WIN_AT} 人就算赢。"
        )

    def title(self) -> str:
        return f"谁是卧底 · 第{self.round}轮"

    def phase_label(self) -> str:
        return {"describe": "描述中", "vote": "投票中",
                "pk_describe": "平票PK·再描述", "pk_vote": "平票PK·重投",
                "done": "已结束"}.get(self.phase, self.phase)

    def progress(self) -> float:
        if self.finished:
            return 1.0
        n = self.player_count or len(self.players)
        dead = n - len(self.alive_players)
        return min(0.95, dead / max(1, n - SPY_WIN_AT))

    # ---- 存档 ----
    def extra_to_dict(self) -> dict:
        return {
            "words": self.words, "spy_id": self.spy_id, "spy_ids": self.spy_ids,
            "player_count": self.player_count,
            "civil_word": self.civil_word, "spy_word": self.spy_word,
            "queue": self.queue, "votes": self.votes, "pk_ids": self.pk_ids,
            "pk_said": {str(k): v for k, v in self.pk_said.items()},
            "descriptions": {str(k): v for k, v in self.descriptions.items()},
            "self_reads": {str(k): v for k, v in self.self_reads.items()},
        }

    def extra_from_dict(self, d: dict) -> None:
        self.words = d.get("words", {})
        self.spy_id = d.get("spy_id", "")
        # 旧存档只有单个 spy_id，兼容一下
        self.spy_ids = d.get("spy_ids") or ([self.spy_id] if self.spy_id else [])
        self.player_count = d.get("player_count", len(d.get("players", [])) or 8)
        self.civil_word = d.get("civil_word", "")
        self.spy_word = d.get("spy_word", "")
        self.queue = d.get("queue", [])
        self.votes = d.get("votes", {})
        self.pk_ids = d.get("pk_ids", [])
        self.pk_said = {int(k): v for k, v in d.get("pk_said", {}).items()}
        self.descriptions = {int(k): v for k, v in d.get("descriptions", {}).items()}
        self.self_reads = {int(k): v for k, v in d.get("self_reads", {}).items()}
