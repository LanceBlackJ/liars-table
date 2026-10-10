"""组装《冬埋》→ data/scripts/dongmai.json。

    python scripts/build_dongmai.py

内容分散在 dm_*.py 里（太长，一个文件写不下）：
  dm_chars_a / dm_chars_b  角色（含三幕追加剧本）
  dm_world                 背景、地图、案情、时间线
  dm_clues_a / dm_clues_b  线索（搜出来的 / 随身的 / 合成的）
  dm_end                   幕间、推理链、答卷
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _scriptkit as K  # noqa: E402
import dm_chars_a as A  # noqa: E402
import dm_chars_b as B  # noqa: E402
import dm_clues_a as CA  # noqa: E402
import dm_clues_b as CB  # noqa: E402
import dm_clues_c as CC  # noqa: E402
import dm_clues_d as CD  # noqa: E402
import dm_clues_e as CE  # noqa: E402
import dm_end as END  # noqa: E402
import dm_extra_a as XA  # noqa: E402
import dm_extra_b as XB  # noqa: E402
import dm_extra_c as XC  # noqa: E402
import dm_extra_d as XD  # noqa: E402
import dm_extra_e as XE  # noqa: E402
import dm_lines as LN  # noqa: E402
import dm_world as W  # noqa: E402

PARA = "\n\n"


def _apply_extras(chars: list[dict]) -> list[dict]:
    """把补充段落并进角色：背景段并入 secret，延伸段并入各幕 text。"""
    secrets = {**XA.SECRET_EXTRA, **XB.SECRET_EXTRA2}
    for c in chars:
        c["lines"] = LN.LINES.get(c["id"], [])
        if c["id"] in secrets:
            c["secret"] = c["secret"] + PARA + secrets[c["id"]]
        for a in c.get("acts", []):
            for table in (XC.ACT_EXTRA, XD.ACT_EXTRA_B, XE.ACT_EXTRA_C):
                extra = table.get(f"{c['id']}:{a['act']}")
                if extra:
                    a["text"] = a["text"] + PARA + extra
    return chars


SPEC = {
    "id": "dongmai",
    "title": "冬埋",
    "tagline": "北岭林场的雪夜寿宴，一个老人想把埋了十年的东西挖出来。他没等到天亮。",
    "author": "ScriptGame 原创",
    "difficulty": "hard",
    "minutes": 180,
    "acts": 3,
    "search_budget": [4, 3, 3],
    "tags": ["三幕", "暴风雪山庄", "往事", "断片", "合成线索", "证人", "硬核"],
    "background": W.BACKGROUND,
    "map": W.MAP,
    "slots": W.SLOTS,
    "victim": W.CASE["victim"],
    "death_slot": W.CASE["death_slot"],
    "death_location": W.CASE["death_location"],
    "weapon": W.CASE["weapon"],
    "cause": W.CASE["cause"],
    "found_by": W.CASE["found_by"],
    "found_slot": W.CASE["found_slot"],
    "murderer": "ding",
    "chars": _apply_extras(A.CHARS_A + B.CHARS_B),
    "where": W.WHERE,
    "actions": W.ACTIONS,
    "observed": W.OBSERVED,
    "no_witness_slots": W.NO_WITNESS_SLOTS,
    "clues": CA.CLUES_A + CB.CLUES_B + CC.CLUES_C + CD.CLUES_D + CE.CLUES_E,
    "interludes": END.INTERLUDES,
    "chain": END.CHAIN,
    "key_clues": END.KEY_CLUES,
    "questions": END.QUESTIONS,
    "question_pass": 0.6,
}


def hanzi(o) -> int:
    return len(re.findall(r"[一-鿿]", str(o)))


def main() -> int:
    ok = K.write_all([SPEC])
    print(f"规格内汉字（粗算）：{hanzi(SPEC):,}")
    return 0 if ok == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
