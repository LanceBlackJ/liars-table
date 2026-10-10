"""组装《锁院》→ data/scripts/kechang.json。

    python scripts/build_kechang.py

内容分散在 kc_*.py 里（太长，一个文件写不下）：
  kc_chars_a / kc_chars_b   角色（含三幕追加剧本）
  kc_world                  背景、地图、案情、时间线
  kc_clues_a..d             线索（搜出来的 / 随身的 / 合成的）
  kc_end                    幕间、推理链、答卷、口吻范例
  kc_extra_*                各人背景段、小抄、打法
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _scriptkit as K  # noqa: E402
import kc_chars_a as A  # noqa: E402
import kc_chars_b as B  # noqa: E402
import kc_clues_a as CA  # noqa: E402
import kc_clues_b as CB  # noqa: E402
import kc_clues_c as CC  # noqa: E402
import kc_clues_d as CD  # noqa: E402
import kc_clues_e as CE  # noqa: E402
import kc_clues_f as CF  # noqa: E402
import kc_end as END  # noqa: E402
import kc_world as W  # noqa: E402

PARA = "\n\n"

try:                      # 补充段落（可选，随着字数补足逐步加入）
    import kc_extra_a as XA
    import kc_extra_b as XB
    import kc_extra_c as XC
    import kc_extra_d as XD
except ImportError:       # pragma: no cover
    XA = XB = XC = XD = None


def _chars() -> list[dict]:
    chars = A.CHARS_A + B.CHARS_B
    for c in chars:
        c["lines"] = END.LINES.get(c["id"], [])
        for _m in (XA, XD):
            if _m is not None and c["id"] in getattr(_m, "SECRET_EXTRA", {}):
                c["secret"] = c["secret"] + PARA + _m.SECRET_EXTRA[c["id"]]
        for a in c.get("acts", []):
            key = f"{c['id']}:{a['act']}"
            for mod in (XA, XB, XC):
                if mod is None:
                    continue
                for tname in ("ACT_EXTRA", "ACT_EXTRA_B", "ACT_EXTRA_C", "ACT_EXTRA_D", "ACT_EXTRA_E"):
                    table = getattr(mod, tname, None)
                    if table and key in table:
                        a["text"] = a["text"] + PARA + table[key]
    return chars


def _clues() -> list[dict]:
    """没有地点的线索（随身 / 合成）同属一个“空地点”，难度值逐条排开，免得搜证顺序按 id 抽签。"""
    out = CA.CLUES_A + CB.CLUES_B + CC.CLUES_C + CD.CLUES_D + CE.CLUES_E + CF.CLUES_F
    n = 0
    for c in out:
        if not c.get("loc"):
            n += 1
            c["d"] = n
    return out


SPEC = {
    "id": "kechang",
    "title": "锁院",
    "tagline": "中秋夜的汴京贡院，每个人都以为自己知道现在是什么时辰。",
    "author": "ScriptGame 原创",
    "difficulty": "hard",
    "minutes": 200,
    "acts": 3,
    "search_budget": [5, 4, 4],
    "tags": ["三幕", "北宋", "封闭空间", "机械诡计", "断片", "合成线索", "证人", "硬核"],
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
    "murderer": "lin",
    "chars": _chars(),
    "where": W.WHERE,
    "actions": W.ACTIONS,
    "observed": W.OBSERVED,
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
