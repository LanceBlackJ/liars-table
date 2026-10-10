"""提示功能的**防作弊**验证。

提示如果泄露了玩家不该知道的东西，这个功能就不是帮助而是外挂。
所以这里的重点不是「提示写得好不好」，而是：

  · 扮演清白角色时，提示里**不能出现真凶的名字被当成事实断言**
  · 提示里不能出现别人的私密秘密原文
  · 提示必须真的用上玩家自己有的东西（点名、引用、线索）

同一局分别用「清白角色」和「凶手角色」各要一次提示，
凶手那次应该往「怎么把自己摘出去」走，清白那次往「怎么找凶手」走。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fixtures  # noqa: F401
from backend import hints  # noqa: E402
from backend.library import build_view, load_script  # noqa: E402

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else "sc_factory"
s = load_script(SCRIPT)
murderer = s.solution.murderer
m_name = s.char(murderer).name
innocent = next(c.id for c in s.characters if c.id != murderer)

log = [
    {"seq": 1, "kind": "narration", "text": s.setting.public_background},
]
for c in s.characters[:4]:
    log.append({"seq": len(log) + 1, "kind": "speech", "speaker_name": c.name,
                "text": f"我当时在{s.setting.map[0]}，没离开过。"})

print("=" * 74)
print(f"《{s.meta.title}》　真凶：{m_name}（{murderer}）")
print("=" * 74)

for cid, label in [(innocent, "清白角色"), (murderer, "凶手")]:
    view = build_view(s, cid)
    clues = [{"id": k.id, "location": k.location, "text": k.text} for k in s.clues[:2]]
    tips = hints.suggest(view, log, clues, situation="轮到他在圆桌上发言。")

    print(f"\n【{label}】{view.name}")
    for t in tips:
        print(f"  · {t}")

    blob = " ".join(tips)
    # 泄漏检查
    leaks = []
    for c in s.characters:
        if c.id == cid:
            continue
        # 别人的秘密原文不能出现（取前 12 字做指纹）
        frag = c.private_secret[:12]
        if frag and frag in blob:
            leaks.append(f"泄露了 {c.name} 的秘密原文")
    # 清白角色的提示里不能把真凶当作已知事实
    if cid != murderer:
        for phrase in (f"{m_name}就是凶手", f"凶手是{m_name}", f"{m_name}是凶手"):
            if phrase in blob:
                leaks.append(f"把真凶当成已知事实说了出来：{phrase}")
    print(f"  泄漏检查：{'❌ ' + '；'.join(leaks) if leaks else '✓ 无泄漏'}")

    # 有用性：是否点到了具体的人或线索
    named = [c.name for c in s.characters if c.id != cid and c.name in blob]
    print(f"  点名了：{named or '（没点名，偏空泛）'}")
    print(f"  条数 {len(tips)}　平均长度 {sum(len(t) for t in tips) // max(1, len(tips))} 字")
