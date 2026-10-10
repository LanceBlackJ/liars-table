"""给玩家自己写的「手法」和「动机」打分。

为什么要有这个模块：原来手法和动机是四选一，那本质是 1/4 的猜，
玩家根本不需要推理——扫一眼哪个选项听着像就行。改成自己写之后，
必须由裁判来判「他说的是不是同一回事」，而这件事没法用字符串比对做：
同一个手法，玩家可能写「用镇尺敲后脑，再把画架搬过来伪装成摔的」，
也可能写「先打晕再布置现场」，都该算对。

判分标准刻意宽松到「抓住了要害就给分」，严在**关键点**上：
真相里的核心动作（凶器、伪装、时间差）说中了才算 2 分，
方向对但漏了关键环节算 1 分，完全不沾边才是 0 分。

判定失败（网络抖动、模型抽风）时**按 1 分处理**——玩家辛苦查了一整局，
不能因为裁判掉链子就判 0。
"""
from __future__ import annotations

import json

from .llm import LLMError, chat, extract_json

_SYS = """\
你是推理游戏的判分裁判。玩家刚刚查完一桩案子，写下了他认为的作案手法和动机。
你的任务是对照真相，判断他写的和真相**是不是同一回事**。

判分标准（0/1/2 三档）：
  2 = 说中了。核心要素都对上了。**用词不同、详略不同都不扣分**，
      只要那件事是同一件事。玩家写得比真相简略是正常的，他不是在默写。
  1 = 方向对，但漏了关键环节，或者掺了明显错误的成分。
  0 = 完全不是一回事，或者空话套话（「他杀了人」这种没有信息量的）。

判分时请注意：
  · 玩家不知道标准答案的措辞，别拿字面重合度当标准。
  · 抓住**要害**就给分：凶器是什么、怎么伪装的、时间差在哪、图什么。
  · 别因为他没写全部细节就压分——只要没写错，写到点子上就够。
  · 但只点到「和某某事有关」这种**话题层面**的（比如「跟假画有关」
    「因为钱」），没说清具体是什么关系、会造成什么后果，最多 1 分。
    2 分要求他说出那个具体的因果，而不只是命中一个关键词。
  · 也别因为写得长就给高分。空洞的长句不如一句说到点上的短话。

点评写给玩家看，一到两句，直接说他哪里对了、哪里没说到。不要复述真相全文。"""


def _judge_one(kind: str, truth: str, mine: str, context: str) -> dict:
    if not mine.strip():
        return {"score": 0, "comment": "你没有写。"}

    user = (
        f"═══ 案件背景 ═══\n{context}\n\n"
        f"═══ 真相（{kind}）═══\n{truth}\n\n"
        f"═══ 玩家写的（{kind}）═══\n{mine.strip()}\n\n"
        '只输出 JSON：{"score": 0|1|2, "comment": "一到两句点评"}'
    )
    try:
        d = extract_json(
            chat(
                [{"role": "system", "content": _SYS}, {"role": "user", "content": user}],
                temperature=0.2,   # 判分要稳，不要每次跑出不同结果
                max_tokens=500,
            )
        )
        score = int(d.get("score", 1))
        if score not in (0, 1, 2):
            score = 1
        return {"score": score, "comment": str(d.get("comment", "")).strip()[:120]}
    except (LLMError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        # 裁判挂了不能让玩家背锅。给中间分并说明情况。
        return {"score": 1, "comment": "（判分服务没响应，本项按半分计）"}


def judge(*, method_truth: str, method_mine: str,
          motive_truth: str, motive_mine: str, context: str) -> dict:
    """返回 {"method": {score, comment}, "motive": {...}}。"""
    return {
        "method": _judge_one("作案手法", method_truth, method_mine, context),
        "motive": _judge_one("作案动机", motive_truth, motive_mine, context),
    }
