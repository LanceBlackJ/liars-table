"""「我不知道说什么」—— 给真人玩家的发言提示。

**这个模块最重要的性质是它不能作弊。**

提示的输入必须和玩家自己屏幕上能看到的东西**完全一致**：
他的角色视角（build_view 切出来的）、公开日志、他手里的线索。
一个字的额外信息都不能给——否则提示会变成「AI 偷偷告诉你凶手是谁」，
这局就没有玩的必要了。

所以这里刻意不接收 Script、不接收 solution、不接收别人的 view。
函数签名就是这道防线：拿不到的东西，写错也泄不出去。

给的是**几个方向**而不是一段成稿：玩家要的是「哦对，我可以问这个」，
不是一段代写的台词直接粘贴。所以每条都短，且必须落到具体的人或事上。
"""
from __future__ import annotations

import json
from typing import Any

from .agents import render_situation
from .llm import LLMError, chat, extract_json
from .models import CharacterView

_SYS = """\
你是玩家身边的一个"军师"。玩家正在玩推理游戏，轮到他发言但一时不知道说什么，
你帮他想几个可以说的方向。

铁律：
0. **先看清楚他现在的处境**（下面「他的处境」那一段），三条提示必须
   全部落在**那个场合**里。这条压倒下面所有的例子。
   比如处境写的是「夜里狼队私下商量刀谁，只有狼队听得见」，那就给
   「刀谁、为什么、怎么跟同伴对齐」这类建议——
   别给「为自己洗清嫌疑」「追问别人的破绽」，那是白天当着全场说的话，
   夜里说给自己同伴听没有任何意义。
1. **你只知道玩家告诉你的这些**。不要推测、编造、或者假装知道真相是什么。
   不知道谁是凶手就别装知道——你和他掌握的信息一模一样。
2. 每条提示必须**落到具体的人或具体的事**上：点名某个人、引用他说过的某句话、
   或者用上玩家手里的某条线索。空泛的「可以质疑一下别人」没有任何用。
2b. **绝对不许编造证据，也不许编造还没发生的事。** 只能用上面材料里
   真实出现过的线索、发言和事实。
   不要发明「地上有水渍」「门把手上有指纹」这种材料里没有的东西——
   玩家照着说出去，等于当众捏造，会把自己搭进去。
   同样不要假设「昨晚有人被杀」「某某已经跳了预言家」这类**日志里没有的
   剧情**。第一轮就是什么都还没发生，那就按什么都没发生来想办法。
   想不出具体的就少给一条，宁缺毋滥。
3. 给 3 条，彼此方向不同，不要 3 条都是同一个意思。
   「方向不同」指的是**做法**不同（进攻 / 防守 / 试探 / 交底 / 拖延），
   具体做什么由第 0 条的处境决定，不要套用固定模板。
4. 每条 25-45 字，是**提示**不是台词。写「可以追问 X 为什么…」这种，
   不要写成一整段可以直接照读的发言。
5. 如果玩家有个人目标还没达成，其中至少一条要为那个目标服务。"""


def suggest(
    view: CharacterView,
    public_log: list[dict],
    my_clues: list[dict],
    *,
    situation: str = "轮到你发言",
    extra: str = "",
) -> list[str]:
    """返回 2-4 条发言方向。失败时返回空列表——提示是锦上添花，
    不能因为它挂了就把玩家卡住。"""
    goals = "\n".join(f"  · [{g.type}] {g.text}" for g in view.personal_goals)
    body = (
        f"玩家扮演的是「{view.name}」（{view.public_role}）。\n\n"
        f"═══ 他的处境 ═══\n{situation}\n"
        + (f"{extra}\n" if extra else "")
        + (f"\n═══ 他还没达成的个人目标 ═══\n{goals}\n" if goals else "")
        + "\n"
        + render_situation(public_log, my_clues, view.name)
        + '\n\n只输出 JSON：{"tips": ["方向一", "方向二", "方向三"]}'
    )
    try:
        d = extract_json(
            chat(
                [{"role": "system", "content": _SYS}, {"role": "user", "content": body}],
                temperature=0.9,
                max_tokens=700,
            )
        )
        tips = [str(t).strip() for t in (d.get("tips") or []) if str(t).strip()]
        return tips[:4]
    except (LLMError, json.JSONDecodeError, AttributeError, TypeError):
        return []


_ASK_SYS = """\
侦探正在问话，你替他想**一句**要问的。只输出那句话本身。

先做一件事，再写：
把他说过的每一句，逐条去对下面的物证清单——
**他哪句话，被哪件物证直接推翻了？**
找到了，就照着那一处问。这是最有杀伤力的问法。

举个例子（只是示范做法，不是本案内容）：
  他说：「我一直在屋里烤着炉子。」
  物证：「炉子插头是松的，机身是凉的，今晚没热起来过。」
  → 问：「你说你一直烤着炉子——那炉子机身现在还是凉的，怎么解释？」

没找到矛盾，就挑一件物证直接摆到他面前，问他怎么说。

三条硬规矩：
· 只能用下面给你的物证和他的原话。**不许发明**「监控拍到」「有人看见」
  「法医说」这类材料里没有的东西——问出去对方一句「哪来的监控」就顶回来了。
· 物证清单就在下面，一定有东西可用，看清楚再写。
· 问句里必须出现一个**具体的名词**——某件物证、某个地点、某个时刻、
  或他原话里的某个词。泛泛地请对方「讲讲情况」等于没问。"""


def ask_for(*, person: str, role: str, evidence: list[str],
            their_words: list[str], already_asked: list[str]) -> str:
    """替玩家想一句该问这个人的话。

    单独写而不是复用 suggest（那个要求给 3 条方向）——
    两套指令混在一起时，模型实测会输出「暂无可追问的破绽」这种废话，
    甚至在玩家明明有三件物证的情况下说「你手中暂无物证」。
    """
    ev = "\n".join(f"  · {x}" for x in evidence) or "  （还没找到任何物证）"
    said = "\n".join(f"  · {x}" for x in their_words[-8:]) or "  （他还没说过话）"
    asked = "\n".join(f"  · {x}" for x in already_asked[-6:]) or "  （还没问过）"
    body = (
        f"你要问的人：{person}（{role}）\n\n"
        f"═══ 你手里的物证 ═══\n{ev}\n\n"
        f"═══ 他说过的话 ═══\n{said}\n\n"
        f"═══ 你已经问过的 ═══\n{asked}\n\n"
        '只输出 JSON：{"q": "一句问话"}'
    )
    try:
        d = extract_json(
            chat(
                [{"role": "system", "content": _ASK_SYS},
                 {"role": "user", "content": body}],
                temperature=0.5, max_tokens=400,   # 这是精确活儿，温度高了会飘去编造
            )
        )
        return str(d.get("q", "")).strip()[:200]
    except (LLMError, json.JSONDecodeError, AttributeError, TypeError):
        return ""


def suggest_plain(
    *,
    who: str,
    situation: str,
    log_lines: list[str],
    private: str = "",
) -> list[str]:
    """给没有 CharacterView 的游戏用（狼人杀 / 谁是卧底）。

    log_lines 必须是**该玩家可见**的日志，调用方负责过滤——
    这里再强调一遍是因为狼人杀的日志带 visible_to，
    传错了就等于把狼队夜聊念给平民听。
    """
    recent = "\n".join(log_lines[-30:]) or "（还没有人说话）"
    body = (
        f"玩家是「{who}」。\n\n═══ 他的处境 ═══\n{situation}\n"
        + (f"\n═══ 只有他知道的信息 ═══\n{private}\n" if private else "")
        + f"\n═══ 目前为止他听到的 ═══\n{recent}\n\n"
        '只输出 JSON：{"tips": ["方向一", "方向二", "方向三"]}'
    )
    try:
        d = extract_json(
            chat(
                [{"role": "system", "content": _SYS}, {"role": "user", "content": body}],
                temperature=0.9,
                max_tokens=700,
            )
        )
        tips = [str(t).strip() for t in (d.get("tips") or []) if str(t).strip()]
        return tips[:4]
    except (LLMError, json.JSONDecodeError, AttributeError, TypeError):
        return []
