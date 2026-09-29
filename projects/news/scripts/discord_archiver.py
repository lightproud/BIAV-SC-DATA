#!/usr/bin/env python3
"""
Discord 全量数据归档器 v2 — 双轨并行 + 断点续传 + JSONL 去重

存储结构:
  projects/news/data/discord/
  ├── guild_meta.json              # 服务器元信息
  ├── channels/
  │   ├── {channel_id[-8:]}/       # 目录名用 ID 后 8 位（避免 emoji/特殊字符在路径中）
  │   │   └── YYYY-MM-DD.jsonl    # 按自然日分片的消息
  ├── activity_daily/
  │   └── YYYY-MM-DD.json         # 每日纯统计摘要（永久保留）
  ├── channel_index.json          # ID → {name, type, dir} 映射（含 emoji 的完整名称在此）
  └── state.json                  # 增量游标 + 历史偿还进度（每频道粒度）

运行模式:
  python discord_archiver.py                         # 常规：今日增量 + 历史偿还一个月
"""

import argparse
import json
import os
import sys
import time
import logging
from datetime import datetime, UTC
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discord_compact import compact_record  # noqa: E402  紧凑 schema 单一权威定义
import news_common  # noqa: E402  原子写 JSON 单一真源（dump_json_atomic）

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

import archive_layout  # noqa: E402  discord 布局收编 SSOT（2026-07-10 方案甲）

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
DISCORD_DATA_DIR = archive_layout.discord_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认

# 三服统一 discord/{global,jp,volunteer}/（2026-07-10 方案甲，根特例消灭）。
# guild_id → 区服名映射唯一源 = archive_layout.DISCORD_GUILD_REGIONS；
# 未登记 guild 响亮失败。可用 DISCORD_DATA_ROOT 环境变量显式覆盖数据根。
GLOBAL_GUILD_ID = '1131791637933199470'


def _bucket_day(timestamp: str) -> str:
    """消息落哪个日档：一律**北京日期**，与 `archive_layout` 声明的日期基准同源。

    原写法是 `datetime.fromisoformat(ts).strftime('%Y-%m-%d')`——Discord 的 timestamp
    带 `+00:00`，直接 strftime 取到的是 **UTC 日**，于是每天北京 00:00–08:00 的消息
    都归进前一天的桶。2026-09-17 全量对账：9,708,177 条里 2,783,113 条（28.67%）错位，
    漂移清一色 -1 天；而同一棵 `Record/Community/` 树下的平台层全按北京日切，读方
    按文件名做跨层同日对比时两侧差一天。守密人 2026-09-18 裁定改写方并迁移历史。

    时间戳缺失 / 不可解析时回落「当下的北京日」——那是**归档时刻**而非发生时刻，
    属于没有更好答案时的下策，不是正常路径。
    """
    try:
        return archive_layout.archive_date_str(datetime.fromisoformat(timestamp))
    except (ValueError, TypeError):
        return archive_layout.archive_date_str()


def resolve_data_dir(guild_id: str | None = None) -> Path:
    """guild 数据目录唯一解析（归档器与回填工具共用）。"""
    override = os.environ.get('DISCORD_DATA_ROOT')
    if override:
        return Path(override)
    return archive_layout.discord_region_dir(DISCORD_DATA_DIR,
                                             guild_id or GLOBAL_GUILD_ID)

REQUEST_DELAY = 0.25          # seconds between API calls (Discord allows 50 req/s per bot)
# 单轮预算。默认 45 分钟不变；允许 env 覆盖，是因为加了积压追赶轮（Track 1b）之后
# 归档器**真的会**吃满预算，而它只是作业里的第一步——后面还有 forum starter 回填与
# commit/push。作业 timeout 必须 > 归档器预算 + 回填预算，否则积压重的那几轮会在
# 提交前被 CI 砍掉，等于白跑。谁排在同一个作业里，谁就在工作流里显式分配预算。
MAX_RUNTIME_SECONDS = int(os.environ.get('DISCORD_RUNTIME_BUDGET') or 45 * 60)
MAX_MESSAGES_PER_CHANNEL = 5000     # 单轮**每频道公平配额**（非本轮抓取总量上限，见 Track 1b）
# 429 `retry_after` 由**服务端**给值，本地无上限地照睡是把整轮跑的进度权交了出去:
# 一个 retry_after=86400 的响应（全局限流 / Cloudflare 拦截 / 被篡改的中间层）就让
# 进程干睡到 CI 作业超时被杀 —— 45 分钟预算 `_is_time_up()` 检查不到睡眠里,
# state.json 也来不及落盘，下一轮从同一处重来。夹到一分钟：真限流时退避照样有效,
# 荒谬值则很快耗尽 max_retries、抛给调用方跳过该频道，本轮其余频道继续推进。
MAX_RETRY_AFTER_SECONDS = 60
# 论坛归档帖分页上限（每页 100 帖 → 20,000 帖）。分页游标是服务端给的
# archive_timestamp，游标不前进 + has_more 恒真即为无限循环兼无限累积。
MAX_ARCHIVED_THREAD_PAGES = 200


# ── Snowflake helpers ────────────────────────────────────────────────────────

DISCORD_EPOCH_MS = 1420070400000


def _sf_from_dt(dt: datetime) -> str:
    """Minimum Discord snowflake string for a given UTC datetime."""
    ms = int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS
    return str(max(ms, 0) << 22)


def _dt_from_sf(snowflake: str | int) -> datetime:
    """Extract UTC datetime from a Discord snowflake ID."""
    ts_ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


def _month_bounds(year: int, month: int):
    """Return (after_sf, before_sf) snowflake strings bracketing a calendar month."""
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC) if month == 12 \
        else datetime(year, month + 1, 1, tzinfo=UTC)
    return _sf_from_dt(start), _sf_from_dt(end)


def _prev_month(year: int, month: int):
    """Return (year, month) for the previous calendar month."""
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _mstr(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


# ── HTTP helper ──────────────────────────────────────────────────────────────

def request_with_retry(method, url, max_retries=3, backoff_base=2, **kwargs):
    """HTTP with exponential backoff; respects Discord 429 rate limits."""
    import requests

    kwargs.setdefault('timeout', 15)
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            # S113 误报：上方 `kwargs.setdefault('timeout', 15)` 已保证必带超时。
            resp = requests.request(method, url, **kwargs)  # noqa: S113
            if resp.status_code == 429:
                retry_after = min(max(resp.json().get('retry_after', 5), 2.0),
                                  MAX_RETRY_AFTER_SECONDS)
                logger.warning(f'Rate limited, waiting {retry_after}s...')
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp
        except Exception as e:
            import requests as _req
            if isinstance(e, _req.exceptions.HTTPError):
                status = e.response.status_code if e.response is not None else 0
                if status not in (429, 500, 502, 503, 504):
                    raise
            last_exc = e
            logger.warning(f'Request error (attempt {attempt + 1}): {e}')
            if attempt < max_retries:
                time.sleep(backoff_base ** attempt)

    raise last_exc


def _ends_cleanly(path: Path) -> bool:
    """JSONL 文件是否以完整行收尾（空文件视为干净）。追加写前的半截行探测。"""
    try:
        size = path.stat().st_size
    except OSError:
        return True
    if size == 0:
        return True
    try:
        with open(path, 'rb') as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b'\n'
    except OSError:
        return True


def _is_forbidden(exc) -> bool:
    """True if the exception is a Discord 403 — bot lacks permission to read the channel."""
    resp = getattr(exc, 'response', None)
    return getattr(resp, 'status_code', None) == 403


# ── Main class ───────────────────────────────────────────────────────────────

class DiscordArchiver:
    API_BASE = 'https://discord.com/api/v10'

    def __init__(self):
        self.token = os.environ.get('DISCORD_BOT_TOKEN', '')
        self.guild_id = os.environ.get('DISCORD_GUILD_ID') or GLOBAL_GUILD_ID
        if not self.token:
            raise RuntimeError('DISCORD_BOT_TOKEN is required')

        # 数据根 = discord/<区服>/（2026-07-10 方案甲，映射唯一源在 archive_layout；
        # 未登记 guild 在此响亮失败）。state/meta/index/channels/activity_daily
        # 全部挂在该根下，服务器之间天然隔离。DISCORD_DATA_ROOT 可显式覆盖。
        self.data_dir = resolve_data_dir(self.guild_id)
        self.state_path = self.data_dir / 'state.json'

        self.headers = {
            'Authorization': f'Bot {self.token}',
            'Content-Type': 'application/json',
        }
        self.state = self._load_state()
        self._start_time = time.time()
        self._pending_threads: list = []
        # Per-run cache of JSONL file → set of already-stored message IDs (dedup guard)
        self._file_ids_cache: dict = {}
        # Per-run set of JSONL files whose tail was already checked for a torn line
        self._tail_checked: set = set()
        # Per-run cache of months already uploaded to Releases (archive-aware backfill)
        self._archived_months_cache: set | None = None
        self.daily_stats: dict = defaultdict(lambda: {
            'messages': 0,
            'reactions_total': 0,
            'attachments': 0,
            'unique_authors': set(),
            'channel_activity': defaultdict(int),
            'hourly_activity': defaultdict(int),
            'top_reacted': [],
            'message_types': defaultdict(int),
        })

    # ── API ──────────────────────────────────────────────────────────────────

    def _api(self, path, **params):
        url = f'{self.API_BASE}{path}'
        resp = request_with_retry('GET', url, headers=self.headers, params=params)
        return resp.json()

    # ── State persistence ────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path, encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f'Failed to load state: {e}')
        return {'channels': {}, 'historical_month': None, 'last_run': None}

    def _save_state(self):
        # 原子替换（news_common.dump_json_atomic 单一真源）：本档是**全部**断点续传
        # 依据——每频道增量游标 / 历史回填指针 / history_backfill_complete 闩锁 /
        # empty_months / forbidden 标记。直写在 json.dump 流式吐字节的中途被杀
        # （CI 作业超时、runner 回收、磁盘写满）就留下半截 JSON，而 _load_state
        # `except Exception` 一律回落 `{'channels': {}, ...}`：**整份游标归零**。
        # 后果不是少存一次，而是下一轮把每个频道当新频道冷启动重抓、历史指针
        # 重置回上个月从头再走一遍（_init_historical_month 的永动机正是靠
        # history_backfill_complete 闩锁挡住的，闩锁也一并没了）。
        # 同一个 state.json 的另一个写方 backfill_forum_starters.py 早已走原子写。
        self.state['last_run'] = datetime.now(UTC).isoformat()
        news_common.dump_json_atomic(self.state_path, self.state)

    def _ch_state(self, channel_id) -> dict:
        """Return (and initialise if missing) per-channel state dict."""
        return self.state['channels'].setdefault(str(channel_id), {})

    # ── Storage helpers ──────────────────────────────────────────────────────

    def _ch_dir(self, channel_id) -> Path:
        """Channel data directory: last 8 chars of ID (no emoji in filesystem path)."""
        return self.data_dir / 'channels' / str(channel_id)[-8:]

    def _file_ids(self, file_path: Path) -> set:
        """Cached set of message IDs already present for this daily file (dedup support).

        gz-aware (2026-07-12 cold-layer ruling): if the date was already
        cold-compressed to `{date}.jsonl.gz`, its IDs count too — history
        backfill re-walking a cold month must not duplicate messages into
        the raw sidecar it appends to.
        """
        if file_path not in self._file_ids_cache:
            ids: set = set()
            gz_path = file_path.with_suffix('.jsonl.gz')
            for path in (file_path, gz_path):
                if not path.exists():
                    continue
                with archive_layout.open_archive_text(path) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                ids.add(json.loads(line).get('id', ''))
                            except json.JSONDecodeError:
                                pass
            self._file_ids_cache[file_path] = ids
        return self._file_ids_cache[file_path]

    def _write_msg(self, channel_id, date_str: str, slim: dict) -> bool:
        """Write a slim message to its daily JSONL, skipping duplicates.

        Returns True only when the message was newly written (H5: callers must
        gate _update_daily_stats on this to avoid double counting on re-fetch).
        """
        ch_dir = self._ch_dir(channel_id)
        ch_dir.mkdir(parents=True, exist_ok=True)
        file_path = ch_dir / f'{date_str}.jsonl'
        ids = self._file_ids(file_path)
        msg_id = slim.get('id', '')
        if not msg_id or msg_id in ids:
            return False  # already stored — dedup guard
        # 写盘紧凑 schema（缺字段=默认值契约，见 discord_compact）；内存 slim 保持完整
        # 供线程队列等内部逻辑读 has_thread/thread_id。
        with open(file_path, 'a', encoding='utf-8') as f:
            # 追加前先确认上一行是完整的（以 \n 收尾）。上一轮若在刷盘中途被杀
            # （作业超时 / 磁盘写满短写），文件尾部会留下一条**没有换行**的半截
            # 记录；直接追加会把新记录粘在它后面拼成一行 —— 这一行谁也解析不了,
            # 于是**被粘上去的那条新消息也一起报废**，而 _file_ids 读不出它的 id、
            # 增量游标却已越过它，这条消息从此永久缺失（实测：1003 号消息消失）。
            # 补一个换行即可让损坏只停留在原来那半截上，重抓的记录干净落地。
            if file_path not in self._tail_checked:
                self._tail_checked.add(file_path)
                if not _ends_cleanly(file_path):
                    f.write('\n')
            f.write(json.dumps(compact_record(slim), ensure_ascii=False) + '\n')
        ids.add(msg_id)
        return True

    # ── Guild metadata ───────────────────────────────────────────────────────

    def fetch_guild_meta(self) -> list:
        """Fetch channel list, save guild_meta.json, return channel list."""
        channels = self._api(f'/guilds/{self.guild_id}/channels')
        meta = {
            'guild_id': self.guild_id,
            'updated_at': datetime.now(UTC).isoformat(),
            'channels': [
                {
                    'id': ch['id'],
                    'name': ch.get('name', ''),
                    'type': ch.get('type', 0),
                    'parent_id': ch.get('parent_id'),
                    'position': ch.get('position', 0),
                    'topic': ch.get('topic', ''),
                }
                for ch in channels
            ],
        }
        meta_path = self.data_dir / 'guild_meta.json'
        news_common.dump_json_atomic(meta_path, meta)   # 半截 JSON 无人可读，原子替换
        logger.info(f'Guild meta saved: {len(channels)} channels')
        return channels

    def _save_channel_index(self, channels: list):
        """
        channel_index.json: full ID → {name (with emoji), type, dir, status} mapping.
        Full channel names (including emoji) live ONLY here, not in filesystem paths.

        Merge-update (T35, 2026-07-12): entries for channels no longer online are
        preserved with status 'offline' instead of being dropped — overwrite-style
        saves were how 498 archive dirs became unindexable orphans.
        """
        import discord_reconcile
        current = {}
        for ch in channels:
            ch_id = str(ch.get('id', ''))
            ch_type = ch.get('type', 0)
            type_label = {0: 'text', 5: 'announcement', 15: 'forum'}.get(ch_type, 'other')
            current[ch_id] = {
                'name': ch.get('name', ''),
                'type': type_label,
                'parent_id': str(ch.get('parent_id') or ''),
                'dir': ch_id[-8:],   # quick reference: the storage directory name
            }
        index_path = self.data_dir / 'channel_index.json'
        existing = {}
        if index_path.exists():
            try:
                existing = json.loads(index_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                logger.warning('channel_index.json unreadable, rebuilding from scratch')
        index = discord_reconcile.merge_channel_index(existing, current)
        # 原子替换：直写被中断留下的半截索引会被上面的 `except json.JSONDecodeError`
        # 判成「unreadable, rebuilding from scratch」——offline/orphan 历史条目
        # 全部蒸发，正是 T35 那 498 个不可反查孤儿目录的成因（合并式更新救不了
        # 一份读不出来的旧档）。同文件的另一个写方 discord_reconcile 已走原子写。
        news_common.dump_json_atomic(index_path, index)
        offline = sum(1 for v in index.values() if v.get('status') == 'offline')
        orphan = sum(1 for v in index.values() if v.get('status') == 'orphan')
        logger.info(f'Channel index saved: {len(index)} entries '
                    f'({len(current)} active / {offline} offline / {orphan} orphan)')

    # ── Message processing ───────────────────────────────────────────────────

    def _slim_message(self, msg: dict) -> dict:
        """Extract and slim a raw Discord message for storage."""
        reactions = [
            {
                'emoji': r.get('emoji', {}).get('name', '?'),
                'emoji_id': r.get('emoji', {}).get('id'),
                'count': r.get('count', 0),
            }
            for r in msg.get('reactions', [])
        ]
        attachments = [
            {
                'id': a.get('id', ''),
                'filename': a.get('filename', ''),
                'content_type': a.get('content_type', ''),
                'size': a.get('size', 0),
                'url': a.get('url', ''),
            }
            for a in msg.get('attachments', [])
        ]
        embeds = [
            {
                'type': e.get('type', ''),
                'title': e.get('title', ''),
                'url': e.get('url', ''),
                'description': (e.get('description') or '')[:300],
            }
            for e in msg.get('embeds', [])
        ]
        ref = msg.get('message_reference')
        author = msg.get('author', {})
        return {
            'id': msg['id'],
            'channel_id': msg.get('channel_id', ''),
            'type': msg.get('type', 0),
            'author_id': author.get('id', ''),
            'author_name': author.get('username', ''),
            'author_bot': author.get('bot', False),
            'content': msg.get('content', ''),
            'timestamp': msg.get('timestamp', ''),
            'edited_timestamp': msg.get('edited_timestamp'),
            'pinned': msg.get('pinned', False),
            'mentions': [u.get('id', '') for u in msg.get('mentions', [])],
            'reactions': reactions,
            'attachments': attachments,
            'embeds': embeds,
            'reply_to': ref.get('message_id') if ref else None,
            'has_thread': bool(msg.get('thread')),
            'thread_id': msg.get('thread', {}).get('id') if msg.get('thread') else None,
            'flags': msg.get('flags', 0),
        }

    def _process_message(self, msg: dict, channel_id, channel_name: str = ''):
        """Slim, write to JSONL, update daily stats, queue threads."""
        slim = self._slim_message(msg)
        date_str = _bucket_day(slim['timestamp'])
        # H5: 仅在新写入时计入日统计，重复抓取不再膨胀 activity_daily 计数
        if self._write_msg(channel_id, date_str, slim):
            self._update_daily_stats(slim, channel_name)
        if slim['has_thread'] and slim['thread_id']:
            self._pending_threads.append(slim['thread_id'])
        return slim

    def _update_daily_stats(self, slim: dict, channel_name: str = ''):
        try:
            ts = datetime.fromisoformat(slim['timestamp'])
        except (ValueError, TypeError):
            return
        # 日键与消息落档桶同基准（北京日），否则 activity_daily 与 JSONL 日档对不上。
        date_str = archive_layout.archive_date_str(ts)
        stats = self.daily_stats[date_str]
        stats['messages'] += 1
        stats['unique_authors'].add(slim['author_id'])
        stats['channel_activity'][channel_name or slim['channel_id']] += 1
        # 小时分布随日键一并折北京时：日按北京切、小时仍按 UTC，同一份统计里两套钟。
        stats['hourly_activity'][str(ts.astimezone(archive_layout.BEIJING_TZ).hour)] += 1
        stats['message_types'][str(slim['type'])] += 1
        stats['attachments'] += len(slim['attachments'])
        total_reactions = sum(r['count'] for r in slim['reactions'])
        stats['reactions_total'] += total_reactions
        if total_reactions > 0:
            stats['top_reacted'].append({
                'id': slim['id'],
                'channel_id': slim['channel_id'],
                'content': slim['content'][:80],
                'author': slim['author_name'],
                'reactions': total_reactions,
                'channel': channel_name,
            })

    def _is_time_up(self) -> bool:
        return (time.time() - self._start_time) > MAX_RUNTIME_SECONDS

    # ── Track 1: Incremental (new messages since last run) ───────────────────

    def _cold_start_backfill(self, channel_id, channel_name: str = '') -> int:
        """First-ever pass for a channel: page BACKWARD (before cursor) from
        newest to oldest, up to MAX_MESSAGES_PER_CHANNEL, so a freshly-added
        guild/channel is archived in full on first run instead of only its
        latest 100. Sets the incremental cursor to the newest message so
        subsequent runs resume normal after-based incremental. Any messages
        older than the cap fall to the historical backfill track.
        """
        ch_key = str(channel_id)
        total = 0
        before = None
        newest_id = '0'

        while total < MAX_MESSAGES_PER_CHANNEL:
            if self._is_time_up():
                logger.warning(f'Runtime limit during cold-start backfill: {channel_name}')
                break
            params = {'limit': 100}
            if before:
                params['before'] = before
            try:
                messages = self._api(f'/channels/{channel_id}/messages', **params)
            except Exception as e:
                if _is_forbidden(e):
                    ch_st = self._ch_state(ch_key)
                    ch_st['forbidden'] = True
                    ch_st['name'] = channel_name
                    logger.info(f'Channel {channel_name}({channel_id}) inaccessible (403) — marked forbidden')
                else:
                    logger.warning(f'Channel {channel_id} cold-start fetch failed: {e}')
                break
            if not isinstance(messages, list) or not messages:
                break
            messages.sort(key=lambda m: m['id'])  # ascending
            if before is None:
                # First batch (no cursor) holds the newest messages; the last
                # after ascending sort is the channel's newest message overall.
                newest_id = messages[-1]['id']
            for msg in messages:
                self._process_message(msg, channel_id, channel_name)
                total += 1
            before = messages[0]['id']  # page further back from the oldest in batch
            time.sleep(REQUEST_DELAY)
            if len(messages) < 100:
                break

        if newest_id != '0':
            self._ch_state(ch_key)['last_message_id'] = newest_id
            self._ch_state(ch_key)['name'] = channel_name
            self._ch_state(ch_key)['cold_started'] = True

        if total >= MAX_MESSAGES_PER_CHANNEL:
            logger.info(f'Cold-start {channel_name}({channel_id}): hit cap {MAX_MESSAGES_PER_CHANNEL}, older history via backfill track')
        else:
            logger.info(f'Cold-start {channel_name}({channel_id}): {total} messages (full channel)')
        return total

    def fetch_channel_incremental(self, channel_id, channel_name: str = '') -> int:
        """Fetch all new messages since last archived message. Returns count.

        On the very first pass for a channel (no stored cursor) this delegates
        to a full backward backfill so freshly-added guilds capture their whole
        history, not just the latest 100 messages.
        """
        ch_key = str(channel_id)
        if self._ch_state(ch_key).get('forbidden'):
            return 0  # no read permission (403) — skip, don't burn requests each run
        last_id = self._ch_state(ch_key).get('last_message_id', '0')
        if last_id == '0':
            return self._cold_start_backfill(channel_id, channel_name)
        total = 0

        while total < MAX_MESSAGES_PER_CHANNEL:
            if self._is_time_up():
                logger.warning(f'Runtime limit during incremental fetch: {channel_name}')
                break
            params = {'limit': 100}
            if last_id != '0':
                params['after'] = last_id
            try:
                messages = self._api(f'/channels/{channel_id}/messages', **params)
            except Exception as e:
                if _is_forbidden(e):
                    self._ch_state(ch_key)['forbidden'] = True
                    logger.info(f'Channel {channel_name}({channel_id}) inaccessible (403) — marked forbidden')
                else:
                    logger.warning(f'Channel {channel_id} incremental fetch failed: {e}')
                break
            if not isinstance(messages, list) or not messages:
                break
            messages.sort(key=lambda m: m['id'])
            for msg in messages:
                self._process_message(msg, channel_id, channel_name)
                total += 1
            last_id = messages[-1]['id']
            self._ch_state(ch_key)['last_message_id'] = last_id
            self._ch_state(ch_key)['name'] = channel_name
            time.sleep(REQUEST_DELAY)
            if len(messages) < 100:
                break

        if total >= MAX_MESSAGES_PER_CHANNEL:
            logger.info(f'Incremental {channel_name}({channel_id}): hit cap {MAX_MESSAGES_PER_CHANNEL}, continues next run')
        else:
            logger.info(f'Incremental {channel_name}({channel_id}): {total} new messages')
        return total

    # ── Track 2: Historical backfill (one month per run) ─────────────────────

    def _guild_start_month(self):
        """Derive guild creation year/month from its Snowflake ID (no extra API call)."""
        try:
            dt = _dt_from_sf(self.guild_id)
            return dt.year, dt.month
        except (ValueError, TypeError):
            return 2023, 1

    def _init_historical_month(self):
        """Initialise historical_month to last month if not already set.

        Once the backfill has walked all the way back to guild creation, the
        `history_backfill_complete` flag latches True and we do NOT re-init —
        otherwise the pointer resets to last month every run and re-fetches the
        entire history forever (perpetual-motion bug). Recent months stay fresh
        via the incremental collector; historical backfill is a one-time gap-fill.
        """
        if self.state.get('history_backfill_complete'):
            return
        if not self.state.get('historical_month'):
            now = datetime.now(UTC)
            y, m = _prev_month(now.year, now.month)
            self.state['historical_month'] = _mstr(y, m)
            self._save_state()

    def _archived_months(self) -> set:
        """Months already uploaded to Releases (per archive-log.json).

        The hourly history backfill must NOT re-fetch/rewrite these — doing so
        un-does the monthly cleanup's git_rm and churns GBs back into the working
        tree (the cleanup/backfill hedge). Cached per run.
        """
        if self._archived_months_cache is not None:
            return self._archived_months_cache
        months: set = set()
        # 日志落点 = **引擎的 base_dir**，即 discord 根（archive_sources.json 里
        # "base_dir": "Public-Info-Pool/Record/Community/discord"），不是本归档器的
        # 区服目录。2026-07-10 方案甲把 Global 从 discord/ 根迁进 discord/global/ 之后,
        # 只看 self.data_dir 的这一处就再也读不到那份日志了：本守卫恒返回空集,
        # 「该月已在 Releases → 跳过重抓」从此一次都不成立。触发点正是 state.json
        # 丢失/重置（见 _save_state）——历史指针退回上个月重走，本该被跳过的 33 个
        # 已归档月份逐个重抓，45 分钟预算全耗在已有数据上，真正缺的月份永远轮不到。
        # 两处都读：区服目录（旧根特例 / 单测）+ discord 根（引擎实际写入处）。
        log_paths = [self.data_dir / 'archive-log.json']
        engine_log = archive_layout.discord_root() / 'archive-log.json'
        if engine_log not in log_paths:
            log_paths.append(engine_log)
        for log_path in log_paths:
            try:
                with open(log_path, encoding='utf-8') as f:
                    entries = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # Engine writes the month under 'group' (group_by=month_from_stem);
                # legacy archive_discord entries used 'month'. Read both.
                month = entry.get('group') or entry.get('month')
                if entry.get('uploaded_to_releases') and month:
                    months.add(month)
        self._archived_months_cache = months
        return months

    def _advance_historical_month(
        self, hist_y: int, hist_m: int,
        guild_start_y: int, guild_start_m: int,
    ):
        """Step the historical pointer to the previous month, or latch complete.

        When the previous month falls before guild creation, the backfill is
        finished: pointer → None and `history_backfill_complete` → True so it
        never re-initialises (see _init_historical_month).
        """
        prev_y, prev_m = _prev_month(hist_y, hist_m)
        if (prev_y, prev_m) < (guild_start_y, guild_start_m):
            logger.info('Historical backfill complete: reached guild creation date')
            self.state['historical_month'] = None
            self.state['history_backfill_complete'] = True
        else:
            self.state['historical_month'] = _mstr(prev_y, prev_m)
            logger.info(f'Historical month done → {_mstr(prev_y, prev_m)}')
        self._save_state()

    @staticmethod
    def _channel_created_month(channel_id) -> tuple[int, int]:
        """Derive channel creation year/month from its Snowflake ID."""
        try:
            dt = _dt_from_sf(channel_id)
            return dt.year, dt.month
        except (ValueError, TypeError):
            return 2023, 1

    def fetch_channel_history_month(
        self, channel_id, channel_name: str, year: int, month: int
    ) -> int:
        """
        Fetch all messages for a channel in a specific calendar month (historical backfill).
        Saves state after completion for断点续传 (resume from breakpoint).
        Returns count of newly archived messages.
        Returns -1 if channel was skipped (created after target month).
        """
        ch_key = str(channel_id)
        if self._ch_state(ch_key).get('forbidden'):
            return -1  # no read permission (403) — skip, excluded from completion check
        month_str = _mstr(year, month)
        after_sf, before_sf = _month_bounds(year, month)

        # ── Optimisation 1: skip channels created after this month (no API call) ──
        ch_created_y, ch_created_m = self._channel_created_month(channel_id)
        if (ch_created_y, ch_created_m) > (year, month):
            ch_st = self._ch_state(ch_key)
            ch_st['last_historical_message_id'] = before_sf
            ch_st['last_historical_month'] = month_str
            return -1  # skip — channel didn't exist yet

        ch_st = self._ch_state(ch_key)

        # ── Optimisation 2: check empty-months set (skip without API call) ──
        empty_months = set(ch_st.get('empty_months', []))
        if month_str in empty_months:
            ch_st['last_historical_message_id'] = before_sf
            ch_st['last_historical_month'] = month_str
            return 0

        # Resume from per-channel cursor if it belongs to this month
        if ch_st.get('last_historical_month') == month_str:
            cursor = ch_st.get('last_historical_message_id', after_sf)
        else:
            cursor = after_sf  # starting fresh for this month

        # Already complete for this month?
        if int(cursor) >= int(before_sf):
            return 0

        total = 0
        after = cursor

        while True:
            if self._is_time_up():
                logger.warning(f'Runtime limit during historical fetch: {channel_name} {month_str}')
                break

            params = {'limit': 100, 'after': after}
            try:
                messages = self._api(f'/channels/{channel_id}/messages', **params)
            except Exception as e:
                if _is_forbidden(e):
                    self._ch_state(ch_key)['forbidden'] = True
                    logger.info(f'Channel {channel_name}({channel_id}) inaccessible (403) — marked forbidden')
                else:
                    logger.warning(f'Channel {channel_id} history {month_str} failed: {e}')
                break

            if not isinstance(messages, list) or not messages:
                # No more messages — channel exhausted for this month
                after = before_sf
                break

            messages.sort(key=lambda m: m['id'])

            # Only process messages within this month's boundaries
            in_month = [m for m in messages if m['id'] < before_sf]
            for msg in in_month:
                self._process_message(msg, channel_id, channel_name)
                total += 1

            newest = messages[-1]['id']
            after = newest

            if not in_month or newest >= before_sf:
                after = before_sf  # reached end of month
                break
            if len(messages) < 100:
                after = before_sf  # exhausted before end of month
                break

            time.sleep(REQUEST_DELAY)

        # ── Optimisation 2b: remember empty months to skip in future ──
        if total == 0 and int(after) >= int(before_sf):
            empty_months.add(month_str)
            ch_st['empty_months'] = sorted(empty_months)

        # Persist cursor — every channel's month completion triggers a state save (断点续传)
        ch_st['last_historical_message_id'] = after
        ch_st['last_historical_month'] = month_str
        self._save_state()

        if total > 0:
            logger.info(f'History {channel_name}({channel_id}) {month_str}: {total} messages')
        return total

    def _all_channels_done_for_month(
        self, channel_ids: list, month_str: str, before_sf: str
    ) -> bool:
        """Return True if every channel has fully processed the given historical month."""
        for ch_id in channel_ids:
            ch_st = self.state['channels'].get(str(ch_id), {})
            if ch_st.get('forbidden'):
                continue  # 无权限频道(403)永远抓不到，排除出完成判定，否则游标永久卡死
            if ch_st.get('last_historical_month') != month_str:
                return False
            if int(ch_st.get('last_historical_message_id', '0')) < int(before_sf):
                return False
        return True

    # ── Forum channels ───────────────────────────────────────────────────────

    def _fetch_archived_threads(self, forum_channel_id, channel_name: str = '') -> list:
        """
        Fetch all archived public threads for a forum channel, handling pagination.
        Discord paginates via archive_timestamp of the last result.
        Returns a flat list of thread objects.
        """
        threads = []
        before = None
        page = 0
        while page < MAX_ARCHIVED_THREAD_PAGES:
            # 与本类其余抓取循环同一纪律：45 分钟预算到点即收尾。原先此循环
            # （及分页游标）完全由服务端说了算——`has_more` 恒 true 且
            # `archive_timestamp` 不前进（服务端 bug / 被篡改的响应）时，本循环
            # 既不停也不受预算约束，`threads` 一路 extend 到把内存吃光。
            if self._is_time_up():
                logger.warning(
                    f'Runtime limit during archived-thread paging: {channel_name}({forum_channel_id})'
                )
                break
            params: dict = {'limit': 100}
            if before:
                params['before'] = before
            try:
                data = self._api(
                    f'/channels/{forum_channel_id}/threads/archived/public', **params
                )
            except Exception as e:
                logger.warning(
                    f'Forum {channel_name}({forum_channel_id}) archived page {page} failed: {e}'
                )
                break
            batch = data.get('threads', [])
            threads.extend(batch)
            page += 1
            if not data.get('has_more', False) or not batch:
                break
            # Pagination cursor: archive_timestamp of the last thread in the batch
            last_meta = batch[-1].get('thread_metadata', {})
            before = last_meta.get('archive_timestamp', '')
            if not before:
                break
            time.sleep(REQUEST_DELAY)
        if page >= MAX_ARCHIVED_THREAD_PAGES:
            logger.warning(
                f'Forum {channel_name}({forum_channel_id}): 归档帖分页触顶 '
                f'{MAX_ARCHIVED_THREAD_PAGES} 页，其余留待下轮'
            )
        logger.info(
            f'Forum {channel_name}({forum_channel_id}): {len(threads)} archived threads found'
        )
        return threads

    def _fetch_forum_thread(
        self,
        thread_id,
        forum_channel_id,
        thread_meta: dict,
        api_last_message_id: str = '',
    ) -> int:
        """
        Fetch new messages from a forum thread incrementally.
        Messages are stored in the *forum channel's* directory (not the thread's own dir),
        and each message is annotated with thread metadata (title, forum_channel_id).
        Returns count of newly archived messages.

        Note: Discord's `/channels/{thread_id}/messages` endpoint does NOT include
        the thread's starter message (the OP that opens the forum post). For forum
        threads, the starter message ID equals the thread ID itself, and we must
        explicitly fetch it via `/channels/{thread_id}/messages/{thread_id}` on
        first pass. This is where Producer's Letter style content lives.
        """
        ch_key = f'thread:{thread_id}'
        stored_last_id = self._ch_state(ch_key).get('last_message_id', '0')
        is_first_pass = (stored_last_id == '0')

        # Skip if the thread's last_message_id hasn't changed since our last fetch
        if api_last_message_id and api_last_message_id != '0' and stored_last_id >= api_last_message_id:
            return 0

        last_id = stored_last_id
        total = 0

        # ── Fetch the starter message (only on first pass) ───────────────────
        # Discord's normal /messages endpoint omits this; without this block, the
        # OP of every forum post (Producer's Letter, official announcements,
        # community-spotlight starters, etc) would never reach the archive.
        if is_first_pass:
            try:
                starter = self._api(f'/channels/{thread_id}/messages/{thread_id}')
                if isinstance(starter, dict) and starter.get('id'):
                    slim = self._slim_message(starter)
                    slim.update(thread_meta)
                    slim['is_thread_starter'] = True
                    date_str = _bucket_day(slim['timestamp'])
                    # H5: 仅在新写入时计入日统计
                    if self._write_msg(forum_channel_id, date_str, slim):
                        self._update_daily_stats(slim, thread_meta.get('thread_title', ''))
                    total += 1
                    # Don't advance last_id past the starter — replies use the same
                    # snowflake range, and `after={thread_id}` would skip them.
            except Exception as e:
                logger.debug(f'Forum thread {thread_id} starter fetch failed: {e}')

        while True:
            if self._is_time_up():
                break
            params: dict = {'limit': 100}
            if last_id != '0':
                params['after'] = last_id
            try:
                messages = self._api(f'/channels/{thread_id}/messages', **params)
            except Exception as e:
                logger.warning(f'Forum thread {thread_id} fetch failed: {e}')
                break
            if not isinstance(messages, list) or not messages:
                break
            messages.sort(key=lambda m: m['id'])
            for msg in messages:
                slim = self._slim_message(msg)
                # Annotate with thread metadata so consumers know which post this belongs to
                slim.update(thread_meta)
                date_str = _bucket_day(slim['timestamp'])
                # Store under the forum channel directory, not the individual thread directory
                # H5: 仅在新写入时计入日统计
                if self._write_msg(forum_channel_id, date_str, slim):
                    self._update_daily_stats(slim, thread_meta.get('thread_title', ''))
                total += 1
            last_id = messages[-1]['id']
            self._ch_state(ch_key)['last_message_id'] = last_id
            time.sleep(REQUEST_DELAY)
            if len(messages) < 100:
                break

        return total

    def fetch_forum_threads(self, channel_id, channel_name: str = '') -> int:
        """
        Fetch messages from a forum channel by iterating over all threads:
          - Active threads  (via guild-level active-threads endpoint)
          - Archived threads (via /channels/{id}/threads/archived/public)
        Each thread is fetched incrementally; messages are stored in the forum
        channel's own directory with thread metadata attached.
        Returns total message count across all threads.
        """
        # 1. Active threads (guild-wide endpoint, filtered to this forum)
        active_threads: list = []
        try:
            data = self._api(f'/guilds/{self.guild_id}/threads/active')
            active_threads = [
                t for t in data.get('threads', [])
                if str(t.get('parent_id', '')) == str(channel_id)
            ]
        except Exception as e:
            logger.warning(f'Forum {channel_name}({channel_id}) active threads failed: {e}')

        # 2. Archived threads
        archived_threads: list = []
        try:
            archived_threads = self._fetch_archived_threads(channel_id, channel_name)
        except Exception as e:
            logger.warning(f'Forum {channel_name}({channel_id}) archived threads error: {e}')

        all_threads = active_threads + archived_threads
        seen_ids: set = set()
        total = 0

        for thread in all_threads:
            if self._is_time_up():
                logger.warning(f'Runtime limit: forum {channel_name}({channel_id}) incomplete')
                break
            t_id = str(thread.get('id', ''))
            if not t_id or t_id in seen_ids:
                continue
            seen_ids.add(t_id)

            thread_meta = {
                'thread_id': t_id,
                'thread_title': thread.get('name', ''),
                'forum_channel_id': str(channel_id),
            }
            applied_tags = thread.get('applied_tags', [])
            if applied_tags:
                thread_meta['thread_tags'] = applied_tags  # type: ignore[assignment]

            api_last_msg_id = str(thread.get('last_message_id') or '0')
            count = self._fetch_forum_thread(t_id, channel_id, thread_meta, api_last_msg_id)
            total += count
            if count > 0:
                self._save_state()  # persist per-thread for 断点续传

        logger.info(
            f'Forum {channel_name}({channel_id}): {total} messages from {len(seen_ids)} threads'
        )
        return total

    def _fetch_thread_incremental(self, thread_id) -> int:
        """Fetch new messages from a thread since last seen (incremental)."""
        ch_key = f'thread:{thread_id}'
        last_id = self._ch_state(ch_key).get('last_message_id', '0')
        total = 0

        # 与 fetch_channel_incremental 同一对护栏（本循环原先两条都没有）：
        # ① 每轮消息上限——一条被刷屏 / 被 bot 灌爆的 thread（后 100 条不断有新的,
        #    `len(messages) < 100` 那条出口永远不成立）能让本循环无限翻页,
        #    `daily_stats` 随每条消息累积、内存一路涨；
        # ② 45 分钟运行预算——本循环是**唯一**不看 `_is_time_up()` 的抓取循环,
        #    一条热 thread 就能把整轮跑拖到 CI 超时被杀，state 不落盘、零推进。
        while total < MAX_MESSAGES_PER_CHANNEL:
            if self._is_time_up():
                logger.warning(f'Runtime limit during thread fetch: {thread_id}')
                break
            params = {'limit': 100}
            if last_id != '0':
                params['after'] = last_id
            try:
                messages = self._api(f'/channels/{thread_id}/messages', **params)
            except Exception as e:
                logger.warning(f'Thread {thread_id} fetch failed: {e}')
                break
            if not isinstance(messages, list) or not messages:
                break
            messages.sort(key=lambda m: m['id'])
            for msg in messages:
                self._process_message(msg, thread_id)
                total += 1
            last_id = messages[-1]['id']
            self._ch_state(ch_key)['last_message_id'] = last_id
            time.sleep(REQUEST_DELAY)
            if len(messages) < 100:
                break

        return total

    # ── Daily statistics output ───────────────────────────────────────────────

    def _save_daily_stats(self):
        """Write (and merge with existing) daily stats JSON files."""
        stats_dir = self.data_dir / 'activity_daily'
        stats_dir.mkdir(parents=True, exist_ok=True)

        for date_str, stats in self.daily_stats.items():
            top_reacted = sorted(
                stats['top_reacted'], key=lambda x: x['reactions'], reverse=True
            )[:20]
            output = {
                'date': date_str,
                'messages': stats['messages'],
                'unique_authors': len(stats['unique_authors']),
                'reactions_total': stats['reactions_total'],
                'attachments': stats['attachments'],
                'channel_activity': dict(stats['channel_activity']),
                'hourly_activity': dict(stats['hourly_activity']),
                'message_types': dict(stats['message_types']),
                'top_reacted_messages': top_reacted,
            }
            file_path = stats_dir / f'{date_str}.json'
            # 冷热分层（2026-07-12）：冷月统计已压 .gz 时以其为底续加——写出的裸旁车
            # 已含 gz 计数，压冷器对 activity_daily 按「raw 胜出」重压覆盖。
            existing_path = file_path if file_path.exists() else (
                file_path.with_suffix('.json.gz')
                if file_path.with_suffix('.json.gz').exists() else None)
            if existing_path is not None:
                try:
                    with archive_layout.open_archive_text(existing_path) as f:
                        existing = json.load(f)
                    output['messages'] += existing.get('messages', 0)
                    output['unique_authors'] = max(
                        output['unique_authors'], existing.get('unique_authors', 0)
                    )
                    output['reactions_total'] += existing.get('reactions_total', 0)
                    output['attachments'] += existing.get('attachments', 0)
                    for ch, cnt in existing.get('channel_activity', {}).items():
                        output['channel_activity'][ch] = output['channel_activity'].get(ch, 0) + cnt
                    for h, cnt in existing.get('hourly_activity', {}).items():
                        output['hourly_activity'][h] = output['hourly_activity'].get(h, 0) + cnt
                    # message_types 与 messages 同源累计，却漏了这一并轨：归档器每小时
                    # 跑一轮，第二轮起本字段被本轮那几条消息的分布**整体覆盖**，于是
                    # 同一份日统计里 messages=503 而 message_types 合计只有 3 —— 自相
                    # 矛盾且不可事后还原（原始分布已被覆盖）。
                    for mt, cnt in existing.get('message_types', {}).items():
                        output['message_types'][mt] = output['message_types'].get(mt, 0) + cnt
                    all_top = top_reacted + existing.get('top_reacted_messages', [])
                    seen: set = set()
                    deduped = []
                    for item in sorted(all_top, key=lambda x: x['reactions'], reverse=True):
                        if item['id'] not in seen:
                            seen.add(item['id'])
                            deduped.append(item)
                    output['top_reacted_messages'] = deduped[:20]
                except Exception as exc:
                    # 静默 pass = 「读不出旧档就当它不存在」，而下一行紧接着把
                    # **只含本轮增量**的 output 覆盖上去：一天 503 条的统计被
                    # 悄悄改写成 3 条。JSONL 里的消息还在，可 _write_msg 的去重
                    # 守卫让这些消息**再也不会**被重新计入统计（重抓即 return
                    # False → _update_daily_stats 不执行），差额永久不可回收。
                    # 至少让它出现在 CI 日志里。
                    logger.warning(
                        f'activity_daily {existing_path.name} 读取失败，本日旧统计'
                        f'将被本轮增量覆盖（不可回收）: {type(exc).__name__}: {exc}'
                    )
            # 原子替换：直写在中途被杀即留下半截 JSON，下一轮走的正是上面那条
            # 「读不出来 → 用本轮增量覆盖」的路，累计计数就此清零。
            news_common.dump_json_atomic(file_path, output)

        logger.info(f'Daily stats saved for {len(self.daily_stats)} day(s)')

    def _flush_on_exit(self):
        """收尾落盘：日统计 + state，异常路径也必须走一遍。

        为什么必须放 finally：消息一经 _write_msg 就**已经持久化**在 JSONL 里,
        而它对应的日统计只攒在内存 self.daily_stats 里、等整轮跑完才写盘。中途
        抛异常（_write_msg 撞 ENOSPC、畸形 payload、作业被取消 → KeyboardInterrupt）
        时，那批消息留在 JSONL、统计凭空消失；下一轮重抓这些消息，_write_msg 的
        去重守卫返回 False，_update_daily_stats 便**永不再执行**——activity_daily
        与 JSONL 之间的这笔差额没有任何补记路径（实测：50 条消息落盘，
        activity_daily 连当天那个文件都不存在）。
        """
        try:
            self._save_daily_stats()
        except Exception:
            logger.exception('收尾保存 activity_daily 失败')
        try:
            self._save_state()
        except Exception:
            logger.exception('收尾保存 state.json 失败')

    # ── Main pipeline ─────────────────────────────────────────────────────────

    def run(self):
        """
        Regular run: dual-track parallel execution.
          Track 1 — Incremental: fetch new messages since last run (all channels)
          Track 2 — Historical:  backfill one calendar month per run (text channels)

        收尾落盘走 finally（见 _flush_on_exit）：中途抛异常时 JSONL 已持久化的
        消息不能只剩「统计从未记过」这一个结局。
        """
        try:
            self._run_tracks()
        finally:
            self._flush_on_exit()

    def _run_tracks(self):
        run_start = time.time()
        logger.info(f'Discord archiver v2 starting (guild {self.guild_id})...')

        # ── Metadata ──
        channels = self.fetch_guild_meta()
        readable_types = {0, 5, 15}
        workable = [ch for ch in channels if ch.get('type', 0) in readable_types]
        text_channels = [ch for ch in workable if ch.get('type', 0) != 15]  # text + announcement
        forum_channels = [ch for ch in workable if ch.get('type', 0) == 15]
        self._save_channel_index(workable)

        # ── Track 1: Incremental ──
        total_incremental = 0
        lagging: list[dict] = []
        for ch in text_channels:
            if self._is_time_up():
                logger.warning('Runtime limit: stopping incremental track')
                break
            count = self.fetch_channel_incremental(ch['id'], ch.get('name', ''))
            total_incremental += count
            # 撞到单轮配额 = 该频道还有没抽干的积压，登记进追赶轮
            if count >= MAX_MESSAGES_PER_CHANNEL:
                lagging.append(ch)
            self._save_state()  # per-channel save for 断点续传

        # ── Track 1b: 积压追赶轮 ──────────────────────────────────────────────
        # MAX_MESSAGES_PER_CHANNEL 是**单轮公平配额**（防一个大频道把预算吃光、
        # 饿死排在后面的频道），不是「本轮最多只要这么多」。少了这一轮，日产出
        # 高于配额的频道每轮净欠一截、游标越掉越远且**永不收敛**：global 的
        # general-chat 日均 ~7.2k 条、morimens-game-chat ~7k 条，对 5k 配额每天
        # 净欠 2k+，于是 general-chat 卡死在 2026-08-07、morimens-game-chat 卡死
        # 在 2026-06-18；而同一轮里排在更后面的 game-question 反倒始终是新的
        # （日产出低于配额，一轮就抽干）——「靠后的活、靠前的死」正是配额缺追赶
        # 的指纹，不是频道顺序或权限问题。
        #
        # 所以第一轮走完后，只要预算还有剩，就对撞了配额的频道反复补抽，直到抽干
        # 或时间到。抽 5k 条 ≈ 50 次请求 × REQUEST_DELAY(0.25s) ≈ 13 秒，相对 45
        # 分钟预算极宽裕；真正的护栏自始至终是 _is_time_up()，不是配额本身。
        catchup_rounds = 0
        while lagging and not self._is_time_up():
            catchup_rounds += 1
            still_lagging = []
            for ch in lagging:
                if self._is_time_up():
                    still_lagging.append(ch)
                    continue
                count = self.fetch_channel_incremental(ch['id'], ch.get('name', ''))
                total_incremental += count
                if count >= MAX_MESSAGES_PER_CHANNEL:
                    still_lagging.append(ch)
                self._save_state()
            lagging = still_lagging
        if catchup_rounds:
            state = '积压已抽干' if not lagging else f'{len(lagging)} 个频道仍有积压（预算耗尽）'
            logger.info(f'Catch-up: {catchup_rounds} 轮追赶，{state}')
        self._warn_unresolved_backlog(lagging)

        # Forum channels: active + archived threads, incremental (no historical backfill)
        for ch in forum_channels:
            if self._is_time_up():
                break
            self.fetch_forum_threads(ch['id'], ch.get('name', ''))

        # ── Track 2: Historical backfill (multi-month per run) ──
        self._init_historical_month()
        guild_start_y, guild_start_m = self._guild_start_month()
        ch_ids = [ch['id'] for ch in text_channels]

        months_completed = 0
        while not self._is_time_up():
            hist_month_str = self.state.get('historical_month')
            if not hist_month_str:
                break

            hist_y = int(hist_month_str[:4])
            hist_m = int(hist_month_str[5:7])

            # Archive-aware: skip months already in Releases — re-fetching them
            # would rewrite files the monthly cleanup git_rm'd (the hedge bug).
            if hist_month_str in self._archived_months():
                logger.info(f'Historical {hist_month_str} already in Releases — skip re-fetch')
                self._advance_historical_month(hist_y, hist_m, guild_start_y, guild_start_m)
                continue

            _, before_sf = _month_bounds(hist_y, hist_m)
            total_historical = 0
            skipped = 0

            for ch in text_channels:
                if self._is_time_up():
                    logger.warning(f'Runtime limit: historical {hist_month_str} incomplete')
                    break
                count = self.fetch_channel_history_month(
                    ch['id'], ch.get('name', ''), hist_y, hist_m
                )
                if count == -1:
                    skipped += 1
                else:
                    total_historical += count

            logger.info(
                f'Historical {hist_month_str}: {total_historical} msgs, '
                f'{skipped} channels skipped (not yet created)'
            )

            # Advance to previous month if all channels completed this one
            if self._all_channels_done_for_month(ch_ids, hist_month_str, before_sf):
                months_completed += 1
                self._advance_historical_month(hist_y, hist_m, guild_start_y, guild_start_m)
            else:
                break  # month not yet complete, continue next run

        if months_completed:
            logger.info(f'Completed {months_completed} historical months this run')

        # ── Deferred threads from incremental track ──
        if self._pending_threads and not self._is_time_up():
            logger.info(f'Fetching {len(self._pending_threads)} deferred threads...')
            for thread_id in self._pending_threads:
                if self._is_time_up():
                    logger.warning(f'{len(self._pending_threads)} threads deferred to next run')
                    break
                self._fetch_thread_incremental(thread_id)

        # Daily stats + final state save 由 run() 的 finally 统一收尾
        elapsed = int(time.time() - run_start)
        logger.info(
            f'Archival complete: {total_incremental} incremental msgs, '
            f'{len(workable)} channels, {elapsed}s elapsed'
        )

    def _warn_unresolved_backlog(self, lagging: list) -> None:
        """追赶轮跑完仍有积压的频道要出声——这类停摆此前对所有监控都是隐形的。

        沉默源审计只看 source 级（discord 整体有没有产出），并明确把频道级健康
        「交给 archiver 的 state.json 自理」，而 archiver 从来没做过这个检查。
        于是 general-chat 停摆 9 天、morimens-game-chat 停摆近 2 个月，全程零告警
        ——公会整体每天照常几千条进账，聚合层面完全看不出来。

        判据用「本轮反复撞配额、到预算耗尽仍没抽干」而不是「游标很旧」：後者会把
        已停用频道全部误报（实测 global 121 个文字频道里有 31 个游标超 3 天，其中
        绝大多数是「已停止使用」类频道，最久的 990 天没人发言——那是频道死了，不是
        采集坏了）。撞配额是活跃度与滞后的合取，死频道永远不会命中。
        """
        if not lagging:
            return
        names = ', '.join(str(ch.get('name', ch.get('id'))) for ch in lagging[:10])
        logger.warning(
            f'积压未抽干频道 {len(lagging)} 个（预算耗尽仍在撞单轮配额）：{names}'
            ' —— 单轮排干速度已跟不上这些频道的产出，若连续多轮出现同一批频道，'
            '说明采集频次或预算需要再上调，否则游标会像 general-chat 那样越掉越远'
        )


    def run_history_only(self):
        """
        History-only mode: skip incremental, dedicate full runtime to historical backfill.
        Used by the dedicated history-backfill workflow for maximum throughput.
        """
        try:
            self._run_history_tracks()
        finally:
            self._flush_on_exit()

    def _run_history_tracks(self):
        run_start = time.time()
        logger.info(f'Discord archiver HISTORY-ONLY mode (guild {self.guild_id})...')

        channels = self.fetch_guild_meta()
        readable_types = {0, 5}
        text_channels = [ch for ch in channels if ch.get('type', 0) in readable_types]
        ch_ids = [ch['id'] for ch in text_channels]

        self._init_historical_month()
        guild_start_y, guild_start_m = self._guild_start_month()

        months_completed = 0
        while not self._is_time_up():
            hist_month_str = self.state.get('historical_month')
            if not hist_month_str:
                logger.info('All historical months complete!')
                break

            hist_y = int(hist_month_str[:4])
            hist_m = int(hist_month_str[5:7])

            # Archive-aware: skip months already in Releases — re-fetching them
            # would rewrite files the monthly cleanup git_rm'd (the hedge bug).
            if hist_month_str in self._archived_months():
                logger.info(f'Historical {hist_month_str} already in Releases — skip re-fetch')
                self._advance_historical_month(hist_y, hist_m, guild_start_y, guild_start_m)
                continue

            _, before_sf = _month_bounds(hist_y, hist_m)
            total_historical = 0
            skipped = 0

            for ch in text_channels:
                if self._is_time_up():
                    break
                count = self.fetch_channel_history_month(
                    ch['id'], ch.get('name', ''), hist_y, hist_m
                )
                if count == -1:
                    skipped += 1
                else:
                    total_historical += count

            logger.info(
                f'Historical {hist_month_str}: {total_historical} msgs, '
                f'{skipped} skipped'
            )

            if self._all_channels_done_for_month(ch_ids, hist_month_str, before_sf):
                months_completed += 1
                self._advance_historical_month(hist_y, hist_m, guild_start_y, guild_start_m)
            else:
                break

        # 收尾落盘由 run_history_only() 的 finally 统一执行
        elapsed = int(time.time() - run_start)
        logger.info(
            f'History-only complete: {months_completed} months, '
            f'{len(text_channels)} channels, {elapsed}s'
        )


def main():
    parser = argparse.ArgumentParser(description='Discord data archiver v2')
    parser.add_argument(
        '--history-only', action='store_true',
        help='Skip incremental, dedicate full runtime to historical backfill'
    )
    args = parser.parse_args()

    archiver = DiscordArchiver()
    if args.history_only:
        archiver.run_history_only()
    else:
        archiver.run()


if __name__ == '__main__':
    main()
