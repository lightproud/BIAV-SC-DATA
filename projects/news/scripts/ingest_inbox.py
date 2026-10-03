#!/usr/bin/env python3
"""收件箱入湖：把外部采集（Claude 云会话例程）交来的 JSON 并入社区全量档案（T111，守密人 2026-10-03「方案 A」）。

收件文件格式（arca_live_collector.py --out 产出）:
  {"source": "arca_live", "collected_at": ISO, "items": [...], "blocked": true?}

入湖口径与 collect_global -> archive_platforms.archive_all 完全一致：
  - 落点 archive_layout.resolve_write_layout(源名)，按条目自身时间换算 UTC+8 日期分桶；
  - 写入走 archive_platforms.write_archive（item_key 去重合并、冷层旁车减重、原子替换）。
幂等：已入湖文件记录在状态文件 projects/news/data/inbox_ingested.json（文件名 -> sha256 / 条数）；
同名且内容未变的文件跳过；即使状态丢失重跑，也由 item_key 去重保证档案不重复。
空文件 / 被拦文件（items 为空）也记入状态，不报错。

用法: python projects/news/scripts/ingest_inbox.py <收件目录> [--state PATH]
"""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_layout
import archive_platforms
import news_common
from sources import normalize_source

STATE_PATH = Path(__file__).resolve().parent.parent / 'data' / 'inbox_ingested.json'


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get('files'), dict) else {'files': {}}
    except (OSError, ValueError):
        return {'files': {}}


def ingest_items(items: list, fallback_date: str) -> dict:
    """与 archive_platforms.archive_all 同口径分桶写入；返回 {源: 条数}。"""
    groups = defaultdict(list)
    for raw in items:
        if not isinstance(raw, dict):
            continue
        norm = normalize_source(raw.get('source', 'unknown'))
        if norm == 'discord':
            continue
        src, region, subtype = archive_layout.resolve_write_layout(
            norm, raw.get('region') or None, raw.get('archive_subtype') or None)
        d = archive_platforms.item_date_utc8(raw, fallback_date)
        groups[(src, region, subtype, d)].append(raw)
    totals = defaultdict(int)
    for (src, region, subtype, d), group in groups.items():
        archive_platforms.write_archive(src, region, subtype, d, group)
        totals[src] += len(group)
    return dict(totals)


def ingest_dir(inbox: Path, state_path: Path = STATE_PATH) -> dict:
    """入湖目录下全部 *.json（按文件名排序）。返回统计。"""
    state = load_state(state_path)
    stats = {'files': 0, 'skipped': 0, 'bad': 0, 'items': 0, 'blocked': 0}
    today = archive_layout.archive_date_str()
    for f in sorted(Path(inbox).glob('*.json')):
        raw = f.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        rec = state['files'].get(f.name)
        if rec and rec.get('sha256') == digest:
            stats['skipped'] += 1
            continue
        try:
            doc = json.loads(raw.decode('utf-8'))
            items = doc.get('items', []) if isinstance(doc, dict) else []
            if not isinstance(items, list):
                raise ValueError('items 不是列表')
        except (ValueError, UnicodeDecodeError) as e:
            print(f'  ! 跳过不可读收件文件 {f.name}: {e}', file=sys.stderr)
            stats['bad'] += 1
            continue
        totals = ingest_items(items, today)
        n = sum(totals.values())
        blocked = bool(isinstance(doc, dict) and doc.get('blocked'))
        state['files'][f.name] = {
            'sha256': digest, 'items': n, 'blocked': blocked,
            'ingested_at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        stats['files'] += 1
        stats['items'] += n
        stats['blocked'] += blocked
    if stats['files']:
        news_common.dump_json_atomic(state_path, state)
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description='收件箱入湖')
    ap.add_argument('inbox', help='收件 JSON 所在目录')
    ap.add_argument('--state', default=str(STATE_PATH), help='已入湖状态文件')
    args = ap.parse_args(argv)
    inbox = Path(args.inbox)
    if not inbox.is_dir():
        print(f'收件目录不存在：{inbox}，无事可做')
        return 0
    s = ingest_dir(inbox, Path(args.state))
    print(f"入湖 {s['files']} 个文件 / {s['items']} 条（被拦 {s['blocked']}），"
          f"跳过已入湖 {s['skipped']}，不可读 {s['bad']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
