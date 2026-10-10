"""测试专用的小本子。

线上本库只留厚本和 AI 生成的本子，但回归测试需要一个人少、线索少、跑得快的本子，
所以把两个老本子放在 scripts/fixtures/（不在 data/scripts 里，选本页看不到），
再通过 library.EXTRA_PATHS 登记，load_script("sc_songfeng") 照常能找到。

用法：测试脚本在 import backend 之后写一行  `import _fixtures  # noqa: F401`
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from backend import library  # noqa: E402

for p in (HERE / "fixtures").glob("*.json"):
    library.EXTRA_PATHS[p.stem] = p
