"""组装《哑舷》→ data/scripts/yanxian.json。

    python scripts/build_yanxian.py

内容分散在 yx_*.py 里（太长，一个文件写不下）：
  yx_chars_a / yx_chars_b   角色（含三幕追加剧本）
  yx_world                  背景、地图、案情、时间线
  yx_clues_a..d             线索（搜出来的 / 随身的 / 合成的）
  yx_end                    幕间、推理链、答卷、口吻范例
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _scriptkit as K  # noqa: E402
import yx_chars_a as A  # noqa: E402
import yx_chars_b as B  # noqa: E402
import yx_chars_c as C3  # noqa: E402
import yx_clues_a as CA  # noqa: E402
import yx_clues_b as CB  # noqa: E402
import yx_clues_c as CC  # noqa: E402
import yx_clues_d as CD  # noqa: E402
import yx_clues_e as CE  # noqa: E402
import yx_clues_f as CF6  # noqa: E402
import yx_end as END  # noqa: E402
import yx_extra_a as XA  # noqa: E402
import yx_extra_b as XB  # noqa: E402
import yx_extra_c as XC  # noqa: E402
import yx_extra_d as XD  # noqa: E402
import yx_extra_e as XE  # noqa: E402
import yx_world as W  # noqa: E402


PARA = "\n\n"


def _chars() -> list[dict]:
    """把补充段落并进角色：背景段并入 secret，备忘与打法并入各幕 text。"""
    chars = A.CHARS_A + B.CHARS_B + C3.CHARS_C
    for c in chars:
        c["lines"] = END.LINES.get(c["id"], [])
        if c["id"] in XA.SECRET_EXTRA:
            c["secret"] = c["secret"] + PARA + XA.SECRET_EXTRA[c["id"]]
        if c["id"] in XE.SECRET_CLOCK:
            c["secret"] = c["secret"] + PARA + XE.SECRET_CLOCK[c["id"]]
        for a in c.get("acts", []):
            key = f"{c['id']}:{a['act']}"
            for table in (XC.ACT_EXTRA_PSY, XD.ACT_EXTRA_LOOK, XB.ACT_EXTRA_B,
                          XD.ACT_EXTRA_ASK, XC.ACT_EXTRA_C):
                extra = table.get(key)
                if extra:
                    a["text"] = a["text"] + PARA + extra
    return chars


def _clues() -> list[dict]:
    """没有地点的线索（随身 / 合成）同属一个“空地点”，难度值逐条排开，免得搜证顺序按 id 抽签。"""
    out = CA.CLUES_A + CB.CLUES_B + CC.CLUES_C + CD.CLUES_D + CE.CLUES_E + CF6.F
    n = 0
    for c in out:
        if not c.get("loc"):
            n += 1
            c["d"] = n
    return out


SPEC = {
    "id": "yanxian",
    "title": "哑舷",
    "tagline": "一九四七年的长江，浓雾锚泊的四个钟头。每个人都看见凶手在敲键，没有人听见他的机器。",
    "author": "ScriptGame 原创",
    "difficulty": "hard",
    "minutes": 200,
    "acts": 3,
    "search_budget": [5, 4, 4],
    "tags": ["三幕", "民国", "封闭空间", "机械诡计", "断片", "合成线索", "证人", "硬核"],
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
    "murderer": "yu",
    "chars": _chars(),
    "where": W.WHERE,
    "actions": {**W.ACTIONS, **W.ACTIONS_SAN},
    "observed": {**W.OBSERVED, **W.OBSERVED_SAN},
    "no_witness_slots": W.NO_WITNESS_SLOTS,
    "clues": _clues(),
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
