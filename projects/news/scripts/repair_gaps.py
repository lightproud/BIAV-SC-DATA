#!/usr/bin/env python3
"""
repair_gaps.py — Detect and report date gaps in platform archives.

Scans the Public-Info-Pool/Record/Community archive (flat legacy files plus
the region/type-layered layout, via the archive_layout SSOT) for missing
dates between the earliest and latest archive file for each registered source.

2026-07-02 修复：本工具此前仍扫已迁空的旧根 projects/news/data/platforms/——
断档检测自 2026-06-21 数据根迁移起整体失明（天天报 No gaps 是因为在扫空屋）。
现改为按注册源经 archive_layout 遍历新根。

Writes a gap_report.json for monitoring instead of creating empty placeholder
files (which inflate coverage metrics and mislead backfill scripts).

Usage:
    python repair_gaps.py              # detect + write report
    python repair_gaps.py --dry-run    # detect only, don't write
    python repair_gaps.py --since 2026-04-01  # only check from this date
"""

import json
import argparse
import logging
import sys
from datetime import datetime, date, timedelta, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import KNOWN_SOURCES, INDEPENDENT_ARCHIVE_SOURCES, LEGACY_SOURCES
import archive_layout

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ARCHIVE_DIR = archive_layout.community_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认
# 工作报告仍留 projects/news/data/（Record/ 是档案层，不放监控产物）
REPORT_PATH = _REPO_ROOT / 'projects' / 'news' / 'data' / 'gap_report.json'

# discord 有独立归档器与逐频道语义，不参与按日断档检测
GAP_SOURCES = [s for s in (list(KNOWN_SOURCES) + list(INDEPENDENT_ARCHIVE_SOURCES)
                           + list(LEGACY_SOURCES)) if s != 'discord']


# 本次扫描的覆盖台账（detect_gaps 每次重置）。「一个缺口都没有」与「一个源都没扫到」
# 在原日志里长得一模一样：数据根未挂载（BIAV_SC_DATA_ROOT 未设 / data 仓未 clone）时
# 每个源都只有 0 个日期文件，全部走 `len(dates) < 2` 的静默 continue，工具照报
# 「No gaps detected in any platform archive.」、报告落 total_gaps: 0。
# 2026-07-02 已因同款失明（扫空屋）天天报绿一次，这里把「扫了几个源」一并说出来。
SCAN_STATS: dict[str, int] = {'sources_total': 0, 'sources_scanned': 0, 'sources_insufficient': 0}


def detect_gaps(since: date | None = None) -> dict[str, list[str]]:
    """Scan all registered source archives and return {source: [missing_date_str, ...]}.

    副产物 SCAN_STATS 记录本轮实际能判连续性的源数（供调用方分辨「零缺口」与「零覆盖」）。
    """
    gaps: dict[str, list[str]] = {}
    SCAN_STATS.update(sources_total=len(GAP_SOURCES), sources_scanned=0, sources_insufficient=0)

    for source in GAP_SOURCES:
        dates: list[date] = []
        for f in archive_layout.dated_files(source, ARCHIVE_DIR):
            try:
                # 冷压 .gz 的 Path.stem 残留 .json，日期一律经 date_stem（甲案推广）
                dates.append(date.fromisoformat(archive_layout.date_stem(f)))
            except ValueError:
                continue
        dates = sorted(set(dates))  # 分层后同日可有多区服文件，去重再判连续性

        if len(dates) < 2:
            SCAN_STATS['sources_insufficient'] += 1
            continue
        SCAN_STATS['sources_scanned'] += 1

        start = since if since and since > dates[0] else dates[0]
        end = dates[-1]
        date_set = set(dates)

        missing = []
        current = start
        while current <= end:
            if current not in date_set:
                missing.append(current.isoformat())
            current += timedelta(days=1)

        if missing:
            gaps[source] = missing

    return gaps


def write_gap_report(gaps: dict[str, list[str]]):
    """Write a JSON report of detected gaps for monitoring, without creating placeholder files."""
    report_path = REPORT_PATH
    report = {
        'generated_at': datetime.now(UTC).isoformat(),
        'total_gaps': sum(len(v) for v in gaps.values()),
        # 覆盖披露：total_gaps=0 必须能与「一个源都没扫到」区分开（见 SCAN_STATS）
        'coverage': dict(SCAN_STATS),
        'platforms': {
            source: {'missing_dates': dates, 'count': len(dates)}
            for source, dates in sorted(gaps.items())
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


# 默认检测窗口：近 N 天。全史检测会把陈年缺口（如 weixin 一条 2016 年杂散日期
# 撑出 3600+ 天"缺口"）灌满报告，淹没真正可操作的新鲜断档。
DEFAULT_WINDOW_DAYS = 60


def main():
    parser = argparse.ArgumentParser(description='Detect and repair date gaps in archives')
    parser.add_argument('--dry-run', action='store_true', help='Only detect, do not repair')
    parser.add_argument('--since', type=str, default=None, help='Only check from this date (YYYY-MM-DD)')
    parser.add_argument('--full', action='store_true',
                        help=f'检测全部历史（默认仅近 {DEFAULT_WINDOW_DAYS} 天窗口）')
    args = parser.parse_args()

    if args.since:
        since = date.fromisoformat(args.since)
    elif args.full:
        since = None
    else:
        # 窗口基准取北京日期（与归档桶名同源）。原 date.today() 在 UTC 容器里
        # 每天有 8 小时比归档日期早一天，缺口检测把当天已归档的源误报为缺。
        since = archive_layout.archive_today() - timedelta(days=DEFAULT_WINDOW_DAYS)
    gaps = detect_gaps(since=since)

    scanned = SCAN_STATS['sources_scanned']
    if not scanned:
        logger.warning(
            f'0 gaps reported but 0 sources were checkable: {SCAN_STATS["sources_total"]} '
            f'registered source(s), none with >=2 dated archive files under {ARCHIVE_DIR} '
            f'—— 这是「没扫到」而不是「没缺口」（数据根是否已挂载 BIAV_SC_DATA_ROOT？）'
        )
    if not gaps:
        logger.info(f'No gaps detected across {scanned} checkable platform archive(s) '
                    f'({SCAN_STATS["sources_insufficient"]} source(s) had <2 dated files, not judgeable).')
        # 零缺口也要刷新报告（2026-07-02 验证编队 minor）：否则历史报告里的旧缺口
        # 落到检测窗口之外后永远没人擦，入库监控产物与工具日志自相矛盾。
        if not args.dry_run:
            write_gap_report({})
        return

    total_gaps = sum(len(v) for v in gaps.values())
    logger.info(f'Detected {total_gaps} gap(s) across {len(gaps)} platform(s):')

    for source, missing_dates in sorted(gaps.items()):
        logger.info(f'  {source}: {len(missing_dates)} missing — {", ".join(missing_dates)}')

    if args.dry_run:
        logger.info('Dry run — no changes made.')
        return

    write_gap_report(gaps)
    logger.info(f'Gap report written to data/gap_report.json ({total_gaps} gaps across {len(gaps)} platforms).')


if __name__ == '__main__':
    main()
