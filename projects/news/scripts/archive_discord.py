#!/usr/bin/env python3
"""
Discord 月度归档 — 向后兼容垫片（守密人 2026-06-21 裁定 A + 合并）

实际逻辑已迁入通用引擎 archive_engine.py（来源注册表 archive_sources.json
的 "discord" 条目）。本垫片仅保留原 CLI 入口与 flag，委派引擎执行，使
discord-archive.yml 无需改动。

原 flag 透传:
  --dry-run / --skip-upload 直接透传；
  --force-month YYYY-MM 映射为引擎的 --force-group YYYY-MM（discord 桶名即月份）。
"""

import sys

from archive_engine import main

if __name__ == '__main__':
    # --force-month 是 discord 专用别名，映射到引擎通用的 --force-group
    argv = ['--source', 'discord']
    for tok in sys.argv[1:]:
        argv.append('--force-group' if tok == '--force-month' else tok)
    main(argv)
