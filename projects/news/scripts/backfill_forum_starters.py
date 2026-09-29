#!/usr/bin/env python3
"""
backfill_forum_starters.py — 一次性回填所有 forum thread 的 starter 消息

背景：
  Discord API 的 `/channels/{thread_id}/messages` 不返回 thread starter（OP）。
  在修复 commit 之前归档过的 forum thread（state.json 已记录 thread:* 条目）
  全部漏掉了 starter。本脚本对每个已记录的 thread 调用
  `/channels/{thread_id}/messages/{thread_id}` 拿到 starter，写入对应日期的
  jsonl，annotate `is_thread_starter: True`。

用法：
  # 默认运行 25 分钟，断点续传
  python projects/news/scripts/backfill_forum_starters.py

  # 自定义预算 / 检测模式（不写文件）
  RUNTIME_BUDGET=600 python projects/news/scripts/backfill_forum_starters.py
  DRY_RUN=1 python projects/news/scripts/backfill_forum_starters.py

进度状态：
  state.json 中新增 'forum_starter_backfill' 顶级键：
    {
      "completed": ["thread:1234...", ...],
      "skipped_no_starter": ["thread:5678..."],
      "started_at": "2026-05-03T...",
      "last_run_at": "2026-05-03T..."
    }

去重：
  - 写入前查 jsonl 是否已含 starter（按 message id）
  - 已 completed 的 thread 跳过

完成后：
  - state.json `forum_starter_backfill.completed` 应包含全部 10000+ thread
  - 后续 cron 不再需要 starter backfill（修复后的 _fetch_forum_thread 自动处理新 thread）
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, UTC
from pathlib import Path

# Reuse the archiver's API + state machinery
sys.path.insert(0, str(Path(__file__).resolve().parent))
from discord_archiver import DiscordArchiver, resolve_data_dir, _bucket_day
import archive_layout  # noqa: E402  冷热分层统一开档（2026-07-12 甲案）
import news_common  # noqa: E402  容错 env 读取（env_int / env_float）

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 与归档器同一解析（DISCORD_GUILD_ID 选服，默认 global；2026-07-10 方案甲布局）
DATA_DIR = resolve_data_dir(os.environ.get('DISCORD_GUILD_ID'))
RUNTIME_BUDGET = news_common.env_int('RUNTIME_BUDGET', 25 * 60)  # default 25 min
DRY_RUN = os.environ.get('DRY_RUN', '').lower() in ('1', 'true', 'yes')
# Per-thread sleep between API calls. Each thread costs 2 reqs (starter + parent),
# so 0.2s ≈ 5 reqs/s, comfortably under Discord's per-bot global ceiling.
RATE_LIMIT_SLEEP = news_common.env_float('RATE_LIMIT_SLEEP', 0.2)

# Priority threads — process these first regardless of state.channels iteration order.
# Defaults to the known-missing Producer's Letter so it lands on the next cron run.
# Override with comma-separated ids: PRIORITY_THREAD_IDS="123,456,789"
_DEFAULT_PRIORITY = '1470748188888797306'  # [Producer's Letter] You Saved Morimens (2026-04-14)
PRIORITY_THREAD_IDS = [
    s.strip() for s in os.environ.get('PRIORITY_THREAD_IDS', _DEFAULT_PRIORITY).split(',') if s.strip()
]


def load_state() -> dict:
    state_path = DATA_DIR / 'state.json'
    if not state_path.exists():
        return {}
    with open(state_path, encoding='utf-8') as f:
        return json.load(f)


def save_state(state: dict) -> None:
    # 原子替换：state.json 同时装着归档器的增量游标与本脚本 10000+ 条 completed 进度,
    # 而本脚本按 RUNTIME_BUDGET（默认 25 分钟）跑到一半就会被 CI 掐掉——直写正撞在
    # 这个窗口上，留下半截 JSON 后归档器 `_load_state` 回落空 state：游标归零、全部
    # thread 进度作废，下一轮从头重刷。
    news_common.dump_json_atomic(DATA_DIR / 'state.json', state)


def already_has_starter(forum_channel_id: str, date_str: str, msg_id: str) -> bool:
    """Check if the forum channel's daily jsonl already contains this starter id."""
    # forum channel directory uses last 8 digits of channel_id
    ch_dir = DATA_DIR / 'channels' / forum_channel_id[-8:]
    # 冷热分层：冷月已压 .gz，裸旁车可能并存，两处都查（2026-07-12 甲案）
    for jsonl_path in (ch_dir / f'{date_str}.jsonl', ch_dir / f'{date_str}.jsonl.gz'):
        if not jsonl_path.exists():
            continue
        with archive_layout.open_archive_text(jsonl_path) as f:
            for line in f:
                try:
                    m = json.loads(line)
                    if m.get('id') == msg_id:
                        return True
                except json.JSONDecodeError:
                    continue
    return False


def main():
    state = load_state()
    if not state:
        logger.error('state.json not found or empty')
        return

    bf = state.setdefault('forum_starter_backfill', {
        'completed': [],
        'skipped_no_starter': [],
        'started_at': datetime.now(UTC).isoformat(),
    })
    bf['last_run_at'] = datetime.now(UTC).isoformat()
    completed: set = set(bf.get('completed', []))
    skipped: set = set(bf.get('skipped_no_starter', []))

    channels = state.get('channels', {})
    thread_keys = [k for k in channels if k.startswith('thread:')]
    pending = [k for k in thread_keys if k not in completed and k not in skipped]

    # Move priority threads to the front of the queue, regardless of source.
    # Inject any priority id not already in state.channels so we can backfill
    # threads we know about but haven't seen yet (e.g. Producer's Letter).
    if PRIORITY_THREAD_IDS:
        priority_keys = [f'thread:{tid}' for tid in PRIORITY_THREAD_IDS]
        pending_set = set(pending)
        for pk in priority_keys:
            if pk in completed or pk in skipped:
                continue
            if pk not in pending_set:
                pending.insert(0, pk)
                pending_set.add(pk)
            else:
                pending.remove(pk)
                pending.insert(0, pk)
        logger.info(f'Priority threads moved to front: {PRIORITY_THREAD_IDS}')

    logger.info(
        f'Forum starter backfill: {len(thread_keys)} threads total, '
        f'{len(completed)} done, {len(skipped)} skipped, {len(pending)} pending'
    )

    if not pending:
        logger.info('Nothing to backfill. All threads processed.')
        return

    archiver = DiscordArchiver()
    deadline = time.time() + RUNTIME_BUDGET
    processed = 0
    new_starters = 0

    for ch_key in pending:
        if time.time() > deadline:
            logger.info(f'Runtime budget hit. Saved progress at {processed} processed.')
            break

        thread_id = ch_key.replace('thread:', '')
        try:
            starter = archiver._api(f'/channels/{thread_id}/messages/{thread_id}')
        except Exception as e:
            err_str = str(e)
            # 404 = thread deleted; 403 = no access; both are permanent skips
            if '404' in err_str or '403' in err_str or 'Unknown' in err_str:
                skipped.add(ch_key)
                logger.debug(f'  {ch_key}: permanent skip ({err_str[:60]})')
            else:
                logger.warning(f'  {ch_key}: transient error, will retry next run: {err_str[:80]}')
            processed += 1
            continue

        if not isinstance(starter, dict) or not starter.get('id'):
            skipped.add(ch_key)
            processed += 1
            continue

        # Find the parent forum channel
        forum_channel_id = ''
        try:
            ch_meta = archiver._api(f'/channels/{thread_id}')
            forum_channel_id = str(ch_meta.get('parent_id') or '')
        except Exception as e:
            logger.warning(f'  {ch_key}: parent_id lookup failed: {e}')
            skipped.add(ch_key)
            processed += 1
            continue

        if not forum_channel_id:
            skipped.add(ch_key)
            processed += 1
            continue

        # 落桶经归档器的日期基准单点（北京日）。写方与回填必须同源，否则回填出来的
        # starter 会落在与归档器不同的日子里——同一个帖的首楼和回复分家两天。
        date_str = _bucket_day(starter.get('timestamp', ''))

        msg_id = str(starter['id'])
        if already_has_starter(forum_channel_id, date_str, msg_id):
            completed.add(ch_key)
            processed += 1
            continue

        slim = archiver._slim_message(starter)
        slim.update({
            'thread_id': thread_id,
            'thread_title': ch_meta.get('name', ''),
            'forum_channel_id': forum_channel_id,
            'is_thread_starter': True,
        })
        applied_tags = ch_meta.get('applied_tags', [])
        if applied_tags:
            slim['thread_tags'] = applied_tags

        if not DRY_RUN:
            archiver._write_msg(forum_channel_id, date_str, slim)
            archiver._update_daily_stats(slim, ch_meta.get('name', ''))

        completed.add(ch_key)
        new_starters += 1
        processed += 1

        if processed % 50 == 0:
            bf['completed'] = sorted(completed)
            bf['skipped_no_starter'] = sorted(skipped)
            if not DRY_RUN:
                save_state(state)
            logger.info(
                f'  progress: {processed} processed, {new_starters} new starters written, '
                f'{len(completed)}/{len(thread_keys)} total complete'
            )

        time.sleep(RATE_LIMIT_SLEEP)

    bf['completed'] = sorted(completed)
    bf['skipped_no_starter'] = sorted(skipped)
    if not DRY_RUN:
        save_state(state)

    logger.info(
        f'Done this run: {processed} processed, {new_starters} new starters. '
        f'Total state: {len(completed)} completed, {len(skipped)} skipped, '
        f'{len(thread_keys) - len(completed) - len(skipped)} remaining.'
    )


if __name__ == '__main__':
    main()
