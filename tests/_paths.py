"""直跑场景的路径引导（`python tests/test_x.py`）。pytest 场景见 pyproject.toml 的 pythonpath。

2026-09-30 采集测试自 BIAV-SC-CODE 迁入本仓；采集代码在 projects/news/scripts/。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 与 pyproject.toml 的 `pythonpath` 保持一致。
SOURCE_DIRS = (".", "projects/news/scripts")

for _rel in SOURCE_DIRS:
    _abs = str((REPO / _rel).resolve())
    if _abs not in sys.path:
        sys.path.insert(0, _abs)
