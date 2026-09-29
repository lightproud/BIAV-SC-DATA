#!/usr/bin/env python3
"""community_cold_compress.py — Community 全量档案月度压冷总入口（甲案推广，2026-07-12）。

守密人裁定「每到一个新月份压上上个月的数据」推广到全部归档：
  - discord dated JSONL → 委托 `discord_cold_compress`（消息 id 并轨语义）；
  - 其余平台 dated 文件（{platform}[/{区服}/{类型}]/YYYY-MM-DD.json[l]）→ 本模块压冷。

平台文件语义：
  - `.json`（单对象 {date, items:[...]}) 压成 `.json.gz`；冷月被回填追加出裸旁车时，
    按条目（url 优先，无 url 按整条内容）并集合并进 .gz，item_count 同步；
  - `.jsonl`（youtube_comments 等行式）同 discord 语义按行去重并轨；
  - 结构不可识别的旁车对（如非 dict/items 形态）不并轨、裸文件原样保留并告警，绝不吞数据。

读方经 `archive_layout.open_archive_text` 透明双开；日期解析一律 `archive_layout.date_stem`。

用法：
  python3 projects/news/scripts/community_cold_compress.py            # discord + 平台全压
  python3 projects/news/scripts/community_cold_compress.py --dry-run
  python3 projects/news/scripts/community_cold_compress.py --scope platforms
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
import archive_layout  # noqa: E402
import discord_cold_compress as dcc  # noqa: E402  discord 语义压冷（复用月界与并轨）
COMMUNITY_ROOT = archive_layout.community_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认


def _atomic_write_gz_text(target: Path, text: str) -> None:
    tmp = target.with_suffix(target.suffix + '.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=9) as g:
        g.write(text)
    os.replace(tmp, target)


def _item_key(it) -> str:
    if isinstance(it, dict) and it.get('url'):
        return f"url:{it['url']}"
    return 'raw:' + json.dumps(it, ensure_ascii=False, sort_keys=True)


def _merge_json_docs(gz_path: Path, raw_path: Path):
    """并轨 {items:[...]} 形态：gz 在前、旁车在后按条目并集。不可识别返回 None。"""
    try:
        with archive_layout.open_archive_text(gz_path) as f:
            base = json.load(f)
        with archive_layout.open_archive_text(raw_path) as f:
            side = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not (isinstance(base, dict) and isinstance(base.get('items'), list)
            and isinstance(side, dict) and isinstance(side.get('items'), list)):
        return None
    seen = set()
    merged = []
    for it in base['items'] + side['items']:
        k = _item_key(it)
        if k in seen:
            continue
        seen.add(k)
        merged.append(it)
    base['items'] = merged
    if 'item_count' in base or 'item_count' in side:
        base['item_count'] = len(merged)
    return base


def compress_platform_file(raw: Path, dry_run: bool = False) -> str:
    """单文件压冷。返回 'compressed' / 'merged' / 'kept'（旁车不可并轨，保留裸文件）。"""
    suffix = '.json.gz' if raw.name.endswith('.json') else '.jsonl.gz'
    gz = raw.with_name(archive_layout.date_stem(raw) + suffix)
    if not gz.exists():
        if not dry_run:
            _atomic_write_gz_text(gz, raw.read_text(encoding='utf-8'))
            raw.unlink()
        return 'compressed'
    if raw.name.endswith('.jsonl'):
        if not dry_run:
            dcc._atomic_write_gz(gz, dcc._merge_lines(gz, raw))
            raw.unlink()
        return 'merged'
    merged = _merge_json_docs(gz, raw)
    if merged is None:
        logger.warning(f'{raw}: 旁车结构不可识别，保留裸文件不并轨')
        return 'kept'
    if not dry_run:
        _atomic_write_gz_text(gz, json.dumps(merged, ensure_ascii=False, indent=2))
        raw.unlink()
    return 'merged'


def compress_activity_daily(cutoff: str, dry_run: bool = False) -> dict:
    """discord 各区服 activity_daily 每日统计压冷。

    旁车语义与消息档不同：统计写方（`discord_archiver._save_daily_stats`）以 gz 为底
    加法续写、裸旁车已含 gz 全部计数——故 **raw 胜出**，直接重压覆盖旧 gz。
    """
    totals = {'compressed': 0, 'superseded': 0, 'raw_bytes': 0, 'gz_bytes': 0}
    discord_root = COMMUNITY_ROOT / 'discord'
    for region_root in sorted(archive_layout.discord_region_roots(discord_root).values()):
        stats_dir = region_root / 'activity_daily'
        if not stats_dir.exists():
            continue
        for raw in sorted(stats_dir.glob('*.json')):
            ds = archive_layout.date_stem(raw)
            if not archive_layout.DATE_STEM.match(ds) or ds[:7] >= cutoff:
                continue
            gz = raw.with_suffix('.json.gz')
            totals['raw_bytes'] += raw.stat().st_size
            outcome = 'superseded' if gz.exists() else 'compressed'
            if not dry_run:
                _atomic_write_gz_text(gz, raw.read_text(encoding='utf-8'))
                raw.unlink()
                totals['gz_bytes'] += gz.stat().st_size
            totals[outcome] += 1
    return totals


def compress_platforms(cutoff: str, dry_run: bool = False) -> dict:
    """discord 之外的全部平台目录压冷。"""
    totals = {'compressed': 0, 'merged': 0, 'kept': 0, 'raw_bytes': 0, 'gz_bytes': 0}
    for pdir in sorted(p for p in COMMUNITY_ROOT.iterdir() if p.is_dir() and p.name != 'discord'):
        pstats = {'compressed': 0, 'merged': 0, 'kept': 0, 'raw_bytes': 0, 'gz_bytes': 0}
        for pattern in ('*.json', '*.jsonl'):
            for raw in sorted(pdir.rglob(pattern)):
                ds = archive_layout.date_stem(raw)
                if not archive_layout.DATE_STEM.match(ds) or ds[:7] >= cutoff:
                    continue
                size = raw.stat().st_size
                outcome = compress_platform_file(raw, dry_run=dry_run)
                pstats[outcome] += 1
                if outcome != 'kept':
                    pstats['raw_bytes'] += size
                    if not dry_run:
                        gz = raw.with_name(ds + ('.json.gz' if pattern == '*.json' else '.jsonl.gz'))
                        if gz.exists():
                            pstats['gz_bytes'] += gz.stat().st_size
        for k in totals:
            totals[k] += pstats[k]
        if pstats['compressed'] or pstats['merged'] or pstats['kept']:
            logger.info(
                f"{pdir.name}: 压缩 {pstats['compressed']} / 并轨 {pstats['merged']} / 保留 {pstats['kept']}，"
                f"{pstats['raw_bytes'] / 1048576:.1f} MB → {pstats['gz_bytes'] / 1048576:.1f} MB"
                f"{'  [dry-run]' if dry_run else ''}"
            )
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description='Community 全量档案月度压冷（冷月 = 上上个月及更早）')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--cutoff', default=None, help='冷月上界 YYYY-MM（不含；默认 = 上月）')
    parser.add_argument('--scope', choices=['all', 'discord', 'platforms'], default='all')
    args = parser.parse_args()
    cutoff = args.cutoff or dcc.default_cutoff()
    logger.info(f'冷月上界（不含）: {cutoff}  scope={args.scope}'
                f'{"  [dry-run：不写任何文件]" if args.dry_run else ""}')
    # dry-run 下 gz_bytes 恒为 0（压缩根本没跑），而合计行照旧打 `X MB → 0.0 MB`——
    # 读起来是「压到零字节」这种不可能的 100% 收益。dry-run 时不报压后体积，只报待压量。
    def _sizes(t: dict) -> str:
        if args.dry_run:
            return f"待压 {t['raw_bytes'] / 1048576:.1f} MB（压后体积 dry-run 不可知）  [dry-run]"
        return f"{t['raw_bytes'] / 1048576:.1f} MB → {t['gz_bytes'] / 1048576:.1f} MB"

    if args.scope in ('all', 'discord'):
        t = dcc.run(cutoff, dry_run=args.dry_run)
        logger.info(f"discord 合计: 压缩 {t['compressed']} / 并轨 {t['merged']}，{_sizes(t)}")
        a = compress_activity_daily(cutoff, dry_run=args.dry_run)
        logger.info(f"activity_daily 合计: 压缩 {a['compressed']} / raw胜出重压 {a['superseded']}，{_sizes(a)}")
    if args.scope in ('all', 'platforms'):
        t = compress_platforms(cutoff, dry_run=args.dry_run)
        logger.info(f"平台合计: 压缩 {t['compressed']} / 并轨 {t['merged']} / 保留 {t['kept']}，{_sizes(t)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
