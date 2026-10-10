"""命令行：用大模型生成本子 / 探案案子。

    python scripts/gen_script.py "民国戏院后台的一桩命案" --suspects 5 --acts 3 --difficulty hard
    python scripts/gen_script.py "北宋贡院锁院夜" --case              # 剧本杀 + 同一个故事的探案
    python scripts/gen_script.py "海岛灯塔看守人之死" --case-only       # 只要探案

没配密钥（SG_API_KEY）时只会产出示例，用来验证流程。
剧本杀本子在 data/scripts/community/ai_*.json，探案案子在 data/cases/case_ai_*.json，之后可以照常
    python scripts/check_script.py data/scripts/community/ai_xxx.json
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import scriptgen  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("theme")
    ap.add_argument("--suspects", type=int, default=4)
    ap.add_argument("--npc", type=int, default=1)
    ap.add_argument("--acts", type=int, default=2, choices=(2, 3))
    ap.add_argument("--difficulty", default="medium", choices=("easy", "medium", "hard"))
    ap.add_argument("--title", default="")
    ap.add_argument("--case", action="store_true", help="同时生成同一个故事的探案案子")
    ap.add_argument("--case-only", action="store_true", help="只生成探案案子")
    a = ap.parse_args()
    t0 = time.time()
    params = {k: v for k, v in vars(a).items() if k not in ("case", "case_only")}
    params["with_script"] = not a.case_only
    params["with_case"] = a.case or a.case_only

    def show(frac: float, stage: str, msg: str) -> None:
        print(f"[{frac * 100:3.0f}%] {stage}　{msg}", flush=True)

    try:
        res = scriptgen.generate_all(params, show)
    except scriptgen.GenError as exc:
        print("❌", exc)
        return 1
    print(f"\n用时 {time.time() - t0:.0f} 秒")
    if res["script"]:
        s = res["script"]
        print(f"✅ 剧本杀《{s['title']}》约 {s['han']:,} 汉字\n   {s['path']}")
    if res["case"]:
        c = res["case"]
        print(f"✅ 探案《{c['title']}》{c['scenes']} 场景 / {c['evidence']} 物证 / {c['people']} 人物\n   {c['path']}")
    if res["case_error"]:
        print("⚠ 探案案子没通过自检，未写入：\n" + res["case_error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
