"""全局测试夹具（2026-09-30 随采集测试自 BIAV-SC-CODE 迁入）。

数据湖隔离（autouse，2026-09-18 守密人裁定）：`BIAV_SC_DATA_ROOT` 指向真实
BIAV-SC-DATA checkout 时，凡运行时调 `archive_layout.*_root()` 的测试会读到**真实
归档**而非自己铺的临时目录——3 个 discord 归档器用例因此在「clone 了数据仓的会话」
里报红（实测把 `historical_month` 读成 2023-08），在不设该变量的 CI 里却恒绿。
于是 `scripts/premerge_gate.py` 这道判定门在本地会话中失真：红的不是改动，是环境。
本夹具把该变量从测试环境摘掉，令测试**恒与 CI 同形态**。需要数据湖的测试自行
monkeypatch 指向自己的 fixture 目录，照常覆盖本夹具。

路径兜底（结构审视 P1，2026-07-27）：本仓 Python 侧不是包（无 pyproject、无
`__init__.py`），模块以裸 basename 互相 import，所以每个测试档都自报 sys.path。
2026-07-27 的 P3 迁档暴露了这套做法的隐患——8 个档的注入指着旧目录，全量跑却
照绿，因为 pytest 单进程共享 sys.path，先被收集的档已经替它们铺好了路；换个
收集顺序就会集体炸。这里把三个源目录一次性备齐，让**全量跑不再取决于收集顺序**。

这不是让各档的注入变成冗余：`python tests/test_x.py` 直跑不经过 conftest（70 个
档有 `__main__` 入口，抽检 8/8 可直跑），那条路仍靠各档自报。两者分管两个场景，
`tests/test_test_isolation.py` 守的正是「档内注入自身够不够」——它刻意不认
conftest 的兜底，否则守卫会被兜底喂成永远绿。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for _src in ("projects/news/scripts",):
    _p = str(REPO / _src)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _isolate_data_lake(monkeypatch):
    # 见档首「数据湖隔离」。摘掉 env 后 archive_layout 回落在树默认根，与 CI 一致。
    monkeypatch.delenv("BIAV_SC_DATA_ROOT", raising=False)
    # `discord_archiver.DISCORD_DATA_DIR` 是**导入期**算定的模块常量：测试档在收集期
    # 就 import 了它，那时 env 还在，常量已经钉在真实数据仓上，光摘 env 追不回来。
    # 已导入才改道，未导入则什么都不做（mutmut 工作副本里可能根本没有这个模块）。
    mod = sys.modules.get("discord_archiver")
    if mod is None:
        return
    try:
        import archive_layout
    except ImportError:
        return
    monkeypatch.setattr(mod, "DISCORD_DATA_DIR", archive_layout.discord_root(), raising=False)
