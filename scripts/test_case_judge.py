"""判分裁判的行为测试。

关键在于**宽严要合适**：
  · 玩家用自己的话把要害说对了 → 必须给 2 分（他不是在默写标准答案）
  · 方向对但漏了关键环节 → 1 分
  · 完全不沾边、或者空话 → 0 分

判太严，玩家推对了也拿不到分，比选择题还挫败；
判太松，随便写两句都满分，那不如不判。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.case_judge import judge  # noqa: E402
from backend.games.detective import load_case  # noqa: E402

c = load_case("case_paint")
sol = c.solution
truth_m = next(o.text for o in sol.method_options if o.id == sol.method)
truth_v = next(o.text for o in sol.motive_options if o.id == sol.motive)
ctx = f"{c.title}：{c.tagline}\n死者：{c.victim}"

print("=" * 74)
print(f"真相手法：{truth_m}")
print(f"真相动机：{truth_v}")
print("=" * 74)

CASES = [
    # (标签, 手法, 动机, 期望手法分, 期望动机分)
    ("换成自己的话，要害全中",
     "先用书案上那把大理石镇尺砸他后脑，人倒下之后再把画架从北窗边搬到尸体旁，做成他踩凳子挂画摔死的样子。",
     "邵禾发现自己早年的画被仿冒拿去卖，准备在开幕式上当众把这件事捅出来。",
     2, 2),
    ("说得简略但抓住了要害",
     "拿镇尺打晕的，然后挪了画架伪装成意外。",
     "怕他揭发仿画的事。",
     2, 2),
    ("方向对，漏了伪装环节",
     "被人用重物打了后脑。",
     "跟卖假画有关。",
     1, 1),
    ("完全错",
     "他自己踩凳子挂画摔下来磕到的，没有第二个人。",
     "为了争遗产。",
     0, 0),
    ("空话",
     "凶手把他杀了。",
     "因为他们有矛盾。",
     0, 0),
]

ok = 0
for label, m, v, em, ev in CASES:
    r = judge(method_truth=truth_m, method_mine=m,
              motive_truth=truth_v, motive_mine=v, context=ctx)
    ms, vs = r["method"]["score"], r["motive"]["score"]
    hit = (ms == em) and (vs == ev)
    ok += hit
    print(f"\n【{label}】 {'✓' if hit else '✗ 期望 %d/%d' % (em, ev)}")
    print(f"  手法 {ms}/2　{r['method']['comment']}")
    print(f"  动机 {vs}/2　{r['motive']['comment']}")

print("\n" + "=" * 74)
print(f"符合预期：{ok}/{len(CASES)}")
