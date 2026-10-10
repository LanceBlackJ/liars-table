"""在真实任务上对比候选模型：角色扮演 + JSON 结构化输出 + 凶手抗压。

不是简单问答——用真实的角色 system prompt 和输出契约，测三件事：
  1. JSON 能不能可靠解析（解析失败会让线索公开机制失效）
  2. 台词是否贴人设
  3. 凶手被物证击穿时能不能按预设应对圆回来
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import agents  # noqa: E402
from backend.library import build_view, load_script  # noqa: E402
from backend.llm import LLMError  # noqa: E402

CANDIDATES = [
    "Qwen3.5-122B-A10B",
    "Qwen3.5-35B-A3B",
    "Qwen3.5-9B",
    "DeepSeek-V3.1-W8A8",  # 作为对照
]

script = load_script("sc_songfeng")
killer = build_view(script, "c3")   # 林砚秋，凶手
civ = build_view(script, "c1")      # 白若薇，非凶手
LOG = [{"kind": "narration", "text": script.setting.public_background}]

ATTACK = "我在书房地毯上找到了石炭酸结晶，那是医用消毒剂。这屋里只有你一个医生。你怎么解释？"


def run(model: str) -> dict:
    orig = agents.chat
    calls: list[float] = []

    def timed(messages, **kw):
        kw["model"] = model
        t0 = time.time()
        out = orig(messages, **kw)
        calls.append(time.time() - t0)
        return out

    agents.chat = timed
    res: dict = {"model": model}
    try:
        a = agents.act(civ, "intro", LOG, [])
        res["civ_speech"] = a["speech"]
        res["civ_json_ok"] = a["speech"] != "……（沉默）" and len(a["speech"]) > 20

        b = agents.answer(killer, ATTACK, "白若薇", LOG, [])
        res["killer_reply"] = b["speech"]
        low = b["speech"]
        res["confessed"] = any(w in low for w in ("我杀了", "人是我杀", "是我干的", "我承认是我"))
        res["json_leak"] = '"speech"' in low or "```" in low
        res["avg_s"] = round(sum(calls) / len(calls), 1)
    except LLMError as exc:
        res["error"] = str(exc)[:150]
    finally:
        agents.chat = orig
    return res


print(f"{'模型':<24} {'JSON':<6} {'自曝':<6} {'格式泄漏':<9} {'均耗时'}")
print("─" * 78)
results = []
for m in CANDIDATES:
    r = run(m)
    results.append(r)
    if "error" in r:
        print(f"{m:<24} ❌ {r['error']}")
        continue
    print(
        f"{m:<24} {'✅' if r['civ_json_ok'] else '❌':<6} "
        f"{'❌是' if r['confessed'] else '✅否':<6} "
        f"{'❌有' if r['json_leak'] else '✅无':<9} {r['avg_s']}s"
    )

print("\n" + "═" * 78)
for r in results:
    if "error" in r:
        continue
    print(f"\n【{r['model']}】")
    print(f"  白若薇自述: {r['civ_speech'][:110]}")
    print(f"  凶手被击穿: {r['killer_reply'][:150]}")
