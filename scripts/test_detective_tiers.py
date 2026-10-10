"""证词层级的隔离断言。

这是《探案》最要紧的一条：喂给模型的系统提示里，
**绝对不能出现它当前层之外的内容**。

如果把三层全塞进去再叮嘱「第二层先别说」，模型迟早会漏——
玩家随便诈一句就崩。不给它，它就不可能漏。

顺带验证：摆错证据不能推进层级，摆对了才推进，且脚本化的第一反应必须原样出现。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SG_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.games.detective import DetectiveGame, load_case  # noqa: E402

c = load_case("case_paint")
fails: list[str] = []

print("=" * 74)
print("一、系统提示的层级隔离")
print("=" * 74)

for p in c.people:
    maxt = max(t.tier for t in p.testimonies)
    for cur in range(maxt + 1):
        g = DetectiveGame.create("case_paint")
        g.tiers[p.id] = cur
        sysmsg = g._sys(p.id)

        # 当前层的内容必须在
        here = p.tier_at(cur)
        for line in here.knows:
            if line not in sysmsg:
                fails.append(f"{p.name} tier{cur}：本层的「{line[:18]}…」没进提示")

        # 其它层的内容一个字都不能在
        for t in p.testimonies:
            if t.tier <= cur:
                continue
            for line in t.knows + t.denies:
                if line and line in sysmsg:
                    fails.append(
                        f"{p.name} tier{cur} 泄漏了 tier{t.tier} 的内容：「{line[:26]}…」"
                    )
            if t.reaction and t.reaction in sysmsg:
                fails.append(f"{p.name} tier{cur} 泄漏了 tier{t.tier} 的反应台词")

        # 真相绝不能出现在任何人的提示里
        for bad, label in [
            (c.solution.explanation[:40], "完整真相"),
            ("凶手", "「凶手」字样"),
        ]:
            if bad and bad in sysmsg:
                fails.append(f"{p.name} tier{cur} 提示里出现了{label}")
    print(f"  {p.name:<5}（{maxt + 1} 层）已逐层检查")

print()
print("=" * 74)
print("二、摆证据的推进规则")
print("=" * 74)

g = DetectiveGame.create("case_paint")
g.evidence = [e.id for e in c.evidence]      # 直接给全物证，只测推进逻辑
qin = c.person("qin")
t1_key = next(t for t in qin.testimonies if t.tier == 1).unlocked_by
t2_key = next(t for t in qin.testimonies if t.tier == 2).unlocked_by
wrong = next(e.id for e in c.evidence if e.id not in (t1_key, t2_key))

g.scene = "foyer"    # 秦棠在前厅

before = g.tiers["qin"]
g.act({"type": "present", "person_id": "qin", "evidence_id": wrong})
after_wrong = g.tiers["qin"]
print(f"  摆错的（{c.ev(wrong).name}）：tier {before} → {after_wrong}"
      f"  {'OK' if after_wrong == before else '←← 不该推进'}")
if after_wrong != before:
    fails.append("摆错证据也推进了层级")

g.act({"type": "present", "person_id": "qin", "evidence_id": t1_key})
after_right = g.tiers["qin"]
print(f"  摆对的（{c.ev(t1_key).name}）：tier {before} → {after_right}"
      f"  {'OK' if after_right == before + 1 else '←← 没推进'}")
if after_right != before + 1:
    fails.append("摆对证据没有推进层级")

# 脚本化的第一反应必须原样出现在日志里
cracks = [e for e in g.log if e["kind"] == "crack"]
want = next(t for t in qin.testimonies if t.tier == 1).reaction
if not cracks:
    fails.append("破防事件没有发出")
elif cracks[-1]["text"] != want:
    fails.append("破防的第一反应被改写了，没有按脚本原样输出")
else:
    print(f"  破防台词原样输出：「{want.splitlines()[0][:30]}…」 OK")

# 跳级：直接摆 tier2 的钥匙，在 tier1 时应当生效；但在 tier0 时不该跳过 tier1
g2 = DetectiveGame.create("case_paint")
g2.evidence = [e.id for e in c.evidence]
g2.scene = "foyer"
g2.act({"type": "present", "person_id": "qin", "evidence_id": t2_key})
print(f"  tier0 时直接摆 tier2 的钥匙：tier → {g2.tiers['qin']}"
      f"  {'OK（没跳级）' if g2.tiers['qin'] == 0 else '←← 跳级了'}")
if g2.tiers["qin"] != 0:
    fails.append("证词层级被跳过了")

print()
print("=" * 74)
if fails:
    print("失败：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("全部通过：模型只看得见当前层，摆对才推进，破防台词不被改写。")
