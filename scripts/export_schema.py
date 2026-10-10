"""导出 docs/script.schema.json —— 模型改了字段就跑一次，编辑器补全才不会过时。

    python scripts/export_schema.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.models import Script  # noqa: E402


def main() -> int:
    out = ROOT / "docs" / "script.schema.json"
    out.write_text(json.dumps(Script.model_json_schema(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"已写入 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
