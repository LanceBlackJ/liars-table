"""个人目标判定。

剧本杀的个人目标是自由文本（"绝不能让人知道你已经应了庆乐园的邀约"），
没法用代码机械判定——只能把整场公开记录交给一个 LLM 裁判去读。

这是全项目里少数几个"必须用 LLM"的地方之一：判断"这个秘密有没有在场上暴露"
本质上是阅读理解，写规则匹配关键词会漏（换个说法就绕过了）也会误伤
（提到相关词但没暴露）。

判定失败时**默认算达成**——宁可让玩家白赢一局，也不要因为网络抖动
把人家辛苦打完的一局判负。
"""
from __future__ import annotations

import json

from .llm import LLMError, chat, extract_json
from .models import CharacterView

JUDGE_SYS = """\
你是剧本杀的复盘裁判。给你一场游戏的完整公开记录，以及某个角色的个人目标。
你要判断每个目标**是否达成**。

判定标准：
· [隐瞒] 类目标：只要这个秘密在公开记录里**被说破了**（不管是他自己说漏、
  还是别人指认出来并被坐实），就算失败。只是被怀疑、被问到但没承认，不算失败。
· [取得] 类目标：记录里要有他实际拿到/找到那样东西的动作，才算达成。
· [保护] 类目标：被保护的对象/事物在记录结束时没有受损，算达成。
· [揭穿] 类目标：他要把那件事在公开场合说出来并被其他人接受，才算达成。

判定要克制：证据不足时倾向于判「达成」，不要凭猜测判失败。
"""


def judge(view: CharacterView, public_log: list[dict], model: str | None = None) -> list[dict]:
    """返回 [{id, text, type, achieved: bool, reason: str}]。"""
    goals = view.personal_goals
    if not goals:
        return []

    fallback = [
        {"id": g.id, "text": g.text, "type": g.type, "achieved": True,
         "reason": "（未能判定，按达成处理）"}
        for g in goals
    ]

    lines = []
    for e in public_log:
        k = e.get("kind")
        if k == "speech":
            lines.append(f"{e['speaker_name']}：{e['text']}")
        elif k == "clue_revealed":
            lines.append(f"【{e['speaker_name']} 公开线索】{e['text']}")
        elif k in ("narration", "system"):
            lines.append(f"【{e.get('text','')}】")
        elif k == "vote":
            lines.append(f"{e['speaker_name']} 投给 {e.get('target_name','')}")
        elif k == "private":
            # 私聊里说漏的话，以前判定完全看不见——
            # 于是「不要让别人知道 X」这类目标，只要你是在密谈里说的，
            # 就永远判成达成。密谈本来就是最容易交底的场合，漏掉它
            # 等于这条规则对半失效。
            #
            # 注意 public_log 是调用方按角色过滤过的（`engine._log_for(cid)`），
            # 所以这里看到的私聊一定是当事人自己参与的那些。
            lines.append(f"〔私下〕{e.get('speaker_name','')}：{e.get('text','')}")
        elif k == "interlude":
            lines.append(f"【{e.get('title','幕间')}】{e.get('text','')}")
    transcript = "\n".join(lines)[-9000:]   # 太长会拖慢判定，取尾部足够

    label = {"conceal": "隐瞒", "acquire": "取得", "protect": "保护", "expose": "揭穿"}
    goal_txt = "\n".join(
        f'{i+1}. [{label.get(g.type, g.type)}] {g.text}' for i, g in enumerate(goals)
    )

    user = f"""\
═══ 角色 ═══
{view.name}（{view.public_role}）
他的秘密：{view.private_secret}

═══ 他的个人目标 ═══
{goal_txt}

═══ 全场公开记录 ═══
{transcript}

逐条判定上面每个目标是否达成。

只输出 JSON：
{{"results": [{{"index": 1, "achieved": true, "reason": "一句话依据"}}, ...]}}"""

    try:
        raw = chat(
            [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": user}],
            model=model,
            temperature=0.2,
            max_tokens=1200,
        )
        data = extract_json(raw)
        out = []
        by_idx = {int(r.get("index", 0)): r for r in data.get("results", []) if isinstance(r, dict)}
        for i, g in enumerate(goals, 1):
            r = by_idx.get(i, {})
            out.append({
                "id": g.id,
                "text": g.text,
                "type": g.type,
                "achieved": bool(r.get("achieved", True)),
                "reason": str(r.get("reason", "")).strip() or "（裁判未给理由）",
            })
        return out
    except (LLMError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return fallback
