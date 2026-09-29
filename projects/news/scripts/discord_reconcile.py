#!/usr/bin/env python3
"""discord_reconcile.py — channel_index 与归档目录的一致性对账（T35，守密人 2026-07-12 点火）。

背景：`discord_archiver._save_channel_index` 原为覆盖式重写——每轮只写当前在线频道，
频道一下线/改名条目即蒸发，归档目录却永驻，孤儿目录堆积（2026-07-12 复测 global
571 目录 vs 索引 143 条）且无法由索引反查。

两件事：
  1. `merge_channel_index`：合并式索引更新（archiver 复用，防复发）——在线条目标
     `status: active`，掉线条目保留原名标 `status: offline`，孤儿登记条目保持 `orphan`。
  2. `reconcile_region` / CLI：一次性对账——扫描孤儿目录，从其 JSONL 首行恢复完整
     channel_id，名字尽力从 git 历史（channel_index / guild_meta 全版本并集）回收，
     回收不到的以空名登记 `status: orphan`，保证每个归档目录都可由索引反查。

用法：
  python3 projects/news/scripts/discord_reconcile.py            # 三区服全对账并写回
  python3 projects/news/scripts/discord_reconcile.py --dry-run  # 只报告不写
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_layout  # noqa: E402  归档布局单一真相源（2026-07-02 P0-1）
DISCORD_ROOT = archive_layout.discord_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认

# 索引条目的三种状态（缺省按 active 解释——存量条目未带 status 字段）
STATUS_ACTIVE = 'active'      # 本轮 API 仍在线
STATUS_OFFLINE = 'offline'    # 曾在线（索引里有名字），现已下线/改名
STATUS_ORPHAN = 'orphan'      # 仅有归档目录，从未被在线索引捕获（多为已删频道/论坛帖）


def merge_channel_index(existing: dict, current: dict) -> dict:
    """合并式索引更新：current（本轮在线条目）覆盖同 id 并标 active；
    existing 中不在 current 的条目原样保留——orphan 保持 orphan，其余标 offline。"""
    merged: dict = {}
    for ch_id, entry in existing.items():
        kept = dict(entry)
        if ch_id not in current:
            old_status = entry.get('status', STATUS_ACTIVE)
            kept['status'] = STATUS_ORPHAN if old_status == STATUS_ORPHAN else STATUS_OFFLINE
        merged[ch_id] = kept
    for ch_id, entry in current.items():
        fresh = dict(entry)
        fresh['status'] = STATUS_ACTIVE
        merged[ch_id] = fresh
    return merged


def recover_channel_id(ch_dir: Path) -> str:
    """从目录内任一 JSONL（裸或 .gz）首行恢复完整 channel_id（紧凑 schema 恒留该字段）。"""
    files = sorted(list(ch_dir.glob('*.jsonl')) + list(ch_dir.glob('*.jsonl.gz')), reverse=True)
    for f in files:
        try:
            with archive_layout.open_archive_text(f) as fh:
                line = fh.readline()
            cid = str(json.loads(line).get('channel_id', ''))
            if cid:
                return cid
        except (OSError, json.JSONDecodeError):
            continue
    return ''


def collect_historical_names(region_root: Path) -> dict:
    """尽力从 git 历史回收 id→name：channel_index.json + guild_meta.json 全版本并集。
    仓库不可用/文件无历史时返回现有可得的并集（不失败）。"""
    names: dict = {}

    def _harvest_index(payload: dict):
        for k, v in payload.items():
            if isinstance(v, dict) and v.get('name'):
                names.setdefault(str(k), v['name'])

    def _harvest_meta(payload):
        chans = payload.get('channels', payload) if isinstance(payload, dict) else payload
        if isinstance(chans, list):
            for c in chans:
                if isinstance(c, dict) and c.get('name'):
                    names.setdefault(str(c.get('id', '')), c['name'])

    for fname, harvest in (('channel_index.json', _harvest_index),
                           ('guild_meta.json', _harvest_meta)):
        path = region_root / fname
        if path.exists():
            try:
                harvest(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, json.JSONDecodeError):
                pass
        try:
            rel = path.resolve().relative_to(_REPO_ROOT)
        except ValueError:
            continue
        try:
            shas = subprocess.run(
                ['git', 'log', '--format=%H', '--', str(rel)],
                capture_output=True, text=True, cwd=_REPO_ROOT, timeout=60,
            ).stdout.split()
        except (subprocess.SubprocessError, OSError):
            shas = []
        for sha in shas:
            try:
                r = subprocess.run(
                    ['git', 'show', f'{sha}:{rel}'],
                    capture_output=True, text=True, cwd=_REPO_ROOT, timeout=60,
                )
                if r.returncode == 0:
                    harvest(json.loads(r.stdout))
            except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
                continue
    names.pop('', None)
    return names


def reconcile_region(region_root: Path, names: dict | None = None,
                     dry_run: bool = False) -> dict:
    """单区服对账：孤儿目录 → 恢复 channel_id → 登记 orphan 条目（名字尽力回收）。"""
    index_path = region_root / 'channel_index.json'
    index: dict = {}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding='utf-8'))
    if names is None:
        names = collect_historical_names(region_root)

    channels_dir = region_root / 'channels'
    dirs = [p for p in channels_dir.iterdir() if p.is_dir()] if channels_dir.exists() else []
    indexed_dirs = {v.get('dir', '') for v in index.values()}

    stats = {'dirs': len(dirs), 'index_before': len(index),
             'orphans': 0, 'named': 0, 'unrecovered': 0}
    for ch_dir in sorted(dirs, key=lambda p: p.name):
        if ch_dir.name in indexed_dirs:
            continue
        stats['orphans'] += 1
        cid = recover_channel_id(ch_dir)
        if not cid:
            stats['unrecovered'] += 1
            logger.warning(f'{region_root.name}/channels/{ch_dir.name}: 无法从 JSONL 恢复 channel_id，跳过')
            continue
        name = names.get(cid, '')
        if name:
            stats['named'] += 1
        index[cid] = {
            'name': name,
            'type': 'unknown',
            'parent_id': '',
            'dir': ch_dir.name,
            'status': STATUS_ORPHAN,
        }
    stats['index_after'] = len(index)
    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子替换：channel_index.json 是频道反查的唯一入口，写一半被中断即整份不可解析。
        # 归档器 `_save_channel_index` 读不出就「rebuilding from scratch」——offline/orphan
        # 条目当场蒸发（正是 T35 要治的 498 孤儿目录形态），而孤儿名字自全仓压扁后
        # 已无 git 历史可再回收（CLAUDE.md §6.3），一次撕裂即永久失名。
        tmp = index_path.with_name(index_path.name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        os.replace(tmp, index_path)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description='Discord channel_index reconcile (T35)')
    parser.add_argument('--dry-run', action='store_true', help='只报告不写回索引')
    args = parser.parse_args()

    roots = archive_layout.discord_region_roots(DISCORD_ROOT)
    if not roots:
        logger.error(f'no discord region roots under {DISCORD_ROOT}')
        return 1
    for region, root in sorted(roots.items()):
        stats = reconcile_region(root, dry_run=args.dry_run)
        logger.info(
            f"{region}: 目录 {stats['dirs']} / 索引 {stats['index_before']}→{stats['index_after']} / "
            f"孤儿 {stats['orphans']}（回收到名字 {stats['named']}，ID 不可恢复 {stats['unrecovered']}）"
            f"{'  [dry-run]' if args.dry_run else ''}"
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
