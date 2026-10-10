"""校验 data/scripts/ 下所有剧本：结构断言 + 视角切分泄漏检查。

    conda run -n scriptgame python scripts/validate_all.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import SCRIPTS_DIR  # noqa: E402
from backend.library import (  # noqa: E402
    all_script_files, assert_no_leak, build_view, validate_script,
)
from backend.models import Script  # noqa: E402


def main() -> int:
    # 含 community/ 子目录；以 _ 开头的模板/草稿不上架，也就不校验
    paths = all_script_files()
    if not paths:
        print(f"没有找到任何剧本：{SCRIPTS_DIR}")
        return 1

    failed = 0
    for path in paths:
        print(f"\n{'=' * 64}\n{path.name}\n{'=' * 64}")
        try:
            script = Script.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 解析失败: {str(exc)[:600]}")
            failed += 1
            continue

        print(f"  《{script.meta.title}》 {script.meta.player_count}人 "
              f"/ {script.meta.difficulty} / {script.meta.estimated_minutes}分钟")

        problems = validate_script(script)
        if problems:
            print(f"  ❌ 结构校验 {len(problems)} 项未通过：")
            for p in problems:
                print(f"     - {p}")
            failed += 1
        else:
            print("  ✅ 结构校验通过")

        leaks_total = 0
        # 逐幕、逐角色：第一幕的视角里不能有他自己第三幕才揭晓的东西，
        # 任何一幕都不能有别人的追加剧本。只看第一幕会把三幕本的泄漏全放过去。
        for act in range(1, script.meta.acts + 1):
            for c in script.characters:
                view = build_view(script, c.id, act=act)
                leaks = assert_no_leak(script, view)
                if leaks:
                    leaks_total += len(leaks)
                    print(f"  ❌ 第{act}幕 {c.id} {c.name} 视角泄漏：")
                    for lk in leaks:
                        print(f"     - {lk}")
        if leaks_total == 0:
            print(f"  ✅ 视角切分无泄漏（{script.meta.acts} 幕 × {len(script.characters)} 人）")
        else:
            failed += 1

        # 信息量体检
        print(f"  · 线索 {len(script.clues)} 条"
              f"（红鲱鱼 {sum(1 for k in script.clues if k.is_red_herring)} 条，"
              f"关键 {len(script.solution.minimum_clues_required)} 条）")
        for c in script.characters:
            v = build_view(script, c.id)
            print(f"  · {c.name:<5} 观察 {len(v.my_observations)} 条 / "
                  f"可为 {len(v.known_others)} 人作证 / "
                  f"目标 {len(v.personal_goals)} 个"
                  f"{'  ← 凶手' if c.is_murderer else ''}")

    print(f"\n{'=' * 64}")
    if failed:
        print(f"❌ {failed}/{len(paths)} 个剧本未通过")
        return 1
    print(f"✅ 全部 {len(paths)} 个剧本通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
