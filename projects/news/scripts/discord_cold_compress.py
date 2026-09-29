#!/usr/bin/env python3
"""discord_cold_compress.py — discord 归档月度压冷（守密人 2026-07-12 甲案裁定）。

冷热分层规则：**每到新月份，压上上个月及更早的数据**——当月 + 上月保持裸 JSONL
热层（维护回填绝大多数落在近两月），更早月份压成 .jsonl.gz 冷层（实测压至 ~18%）。

行为（幂等，可安全重入）：
  - `{date}.jsonl` 且日期属冷月、无同名 .gz → 压缩为 `{date}.jsonl.gz`，删裸文件；
  - 同日期裸 + .gz 并存（冷月被历史回填追加出旁车）→ 按消息 id 并集合并进 .gz，删裸文件；
  - 写入走临时文件 + 原子 rename，单文件级原子。

读方经 `archive_layout.open_archive_text` 透明双开；跨档案检索冷层用 `rg -z`。

用法：
  python3 projects/news/scripts/discord_cold_compress.py --dry-run   # 只报告
  python3 projects/news/scripts/discord_cold_compress.py             # 实际压冷
  python3 projects/news/scripts/discord_cold_compress.py --cutoff 2026-06  # 显式冷月上界（不含）
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_layout  # noqa: E402  归档布局单一真相源（2026-07-02 P0-1）
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date
DISCORD_ROOT = archive_layout.discord_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认


def default_cutoff(today: date | None = None) -> str:
    """冷月上界（不含）= 上月 'YYYY-MM'：month < cutoff 的即为冷月。"""
    # 「今天」取北京日期（归档桶名同基准）。原 date.today() 取容器本地日期，CI 为 UTC,
    # 每天有 8 小时（北京 00:00–08:00）算出的月界比归档实际早一个月：月初那几小时
    # 跑月度压冷会把「上月」当冷月一起压掉（上月按裁定应留热层）。
    d = today or archive_layout.archive_today()
    year, month = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return f'{year:04d}-{month:02d}'


def _merge_lines(gz_path: Path, raw_path: Path) -> list[str]:
    """gz 在前、裸旁车在后，按消息 id 并集（无 id 的行原样保留）。"""
    lines: list[str] = []
    seen: set[str] = set()

    def _absorb(fh):
        for line in fh:
            line = line.rstrip('\n')
            if not line:
                continue
            try:
                mid = str(json.loads(line).get('id', ''))
            except json.JSONDecodeError:
                mid = ''
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            lines.append(line)

    with archive_layout.open_archive_text(gz_path) as fh:
        _absorb(fh)
    with archive_layout.open_archive_text(raw_path) as fh:
        _absorb(fh)
    return lines


def _atomic_write_gz(target: Path, lines: list[str]) -> None:
    tmp = target.with_suffix(target.suffix + '.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=9) as g:
        for line in lines:
            g.write(line + '\n')
    os.replace(tmp, target)


def compress_channel_dir(ch_dir: Path, cutoff: str, dry_run: bool = False) -> dict:
    """单频道目录压冷。返回 {compressed, merged, raw_bytes, gz_bytes}。"""
    stats = {'compressed': 0, 'merged': 0, 'raw_bytes': 0, 'gz_bytes': 0}
    for raw in sorted(ch_dir.glob('*.jsonl')):
        stem = raw.stem  # YYYY-MM-DD
        if not archive_layout.DATE_STEM.match(stem) or stem[:7] >= cutoff:
            continue
        gz = raw.with_suffix('.jsonl.gz')
        stats['raw_bytes'] += raw.stat().st_size
        if dry_run:
            stats['merged' if gz.exists() else 'compressed'] += 1
            continue
        if gz.exists():
            _atomic_write_gz(gz, _merge_lines(gz, raw))
            stats['merged'] += 1
        else:
            with open(raw, encoding='utf-8') as f:
                _atomic_write_gz(gz, [ln.rstrip('\n') for ln in f if ln.strip()])
            stats['compressed'] += 1
        raw.unlink()
        stats['gz_bytes'] += gz.stat().st_size
    return stats


def run(cutoff: str, dry_run: bool = False, region: str | None = None) -> dict:
    totals = {'compressed': 0, 'merged': 0, 'raw_bytes': 0, 'gz_bytes': 0}
    roots = archive_layout.discord_region_roots(DISCORD_ROOT)
    for r, root in sorted(roots.items()):
        if region is not None and r != region:
            continue
        base = root / 'channels'
        if not base.exists():
            continue
        rstats = {'compressed': 0, 'merged': 0, 'raw_bytes': 0, 'gz_bytes': 0}
        for ch_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            s = compress_channel_dir(ch_dir, cutoff, dry_run=dry_run)
            for k in rstats:
                rstats[k] += s[k]
                totals[k] += s[k]
        logger.info(
            f"{r}: 压缩 {rstats['compressed']} / 并轨 {rstats['merged']} 文件，"
            f"{rstats['raw_bytes'] / 1048576:.0f} MB → {rstats['gz_bytes'] / 1048576:.0f} MB"
            f"{'  [dry-run]' if dry_run else ''}"
        )
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description='discord 归档月度压冷（冷月 = 上上个月及更早）')
    parser.add_argument('--dry-run', action='store_true', help='只报告不写')
    parser.add_argument('--cutoff', default=None, help="冷月上界 YYYY-MM（不含；默认 = 上月）")
    parser.add_argument('--region', default=None, help='只处理指定区服')
    args = parser.parse_args()
    cutoff = args.cutoff or default_cutoff()
    logger.info(f'冷月上界（不含）: {cutoff}{"  [dry-run：不写任何文件]" if args.dry_run else ""}')
    t = run(cutoff, dry_run=args.dry_run, region=args.region)
    # dry-run 时压缩没跑，gz_bytes 恒 0；原合计行照打 `N MB → 0 MB`，读起来像压到了零字节。
    sizes = (f"待压 {t['raw_bytes'] / 1048576:.0f} MB（压后体积 dry-run 不可知）  [dry-run]"
             if args.dry_run
             else f"{t['raw_bytes'] / 1048576:.0f} MB → {t['gz_bytes'] / 1048576:.0f} MB")
    logger.info(f"合计: 压缩 {t['compressed']} / 并轨 {t['merged']} 文件，{sizes}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
