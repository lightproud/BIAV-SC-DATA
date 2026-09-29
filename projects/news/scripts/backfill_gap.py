#!/usr/bin/env python3
"""
backfill_gap.py — One-time script to backfill the Apr 13-25 data gap.

Designed to run in GitHub Actions where API keys and network access work.
Uses existing collector functions with expanded time windows.

Usage:
  python projects/news/scripts/backfill_gap.py

Requires env vars: YOUTUBE_API_KEY (optional), WEIBO_COOKIE (optional)
"""

import json
import re
import sys
import os
import logging
import time
from datetime import datetime, timedelta, UTC
from pathlib import Path
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # 日志脱敏单一真源（H3）
import archive_layout  # noqa: E402  归档布局单一真相源（2026-07-02 P0-1）
# 4R 迁移后全量档案层现址（旧 projects/news/data/platforms 已废，2026-07-12 修复）
ARCHIVE_DIR = archive_layout.community_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认


def _gap_bound(env_name: str, default: datetime, end_of_day: bool) -> datetime:
    """Parse a YYYY-MM-DD gap bound from env, falling back to default.

    日期按**北京日**（UTC+8）理解：缺口清单来自 repair_gaps，而归档桶名自始是
    北京日期（archive_layout 日期基准 SSOT），落档也用 archive_date_str 折回北京日。
    原实现把边界当 UTC 午夜，于是抓取窗口与要补的那一天整整错开 8 小时：
    北京 04-13 的前 8 小时（UTC 04-12 16:00–24:00）永远抓不到、这一天补不齐；
    而 UTC 04-13 16:00–24:00 抓到的条目会被 archive_date_str 落进 04-14 桶——
    一个**没人报缺**的日子。窗口与桶自此同基准。
    """
    raw = os.environ.get(env_name, '').strip()
    if not raw:
        return default
    try:
        d = datetime.strptime(raw, '%Y-%m-%d').replace(tzinfo=archive_layout.BEIJING_TZ)
        return d.replace(hour=23, minute=59, second=59) if end_of_day else d
    except ValueError:
        logger.warning(f'{env_name}={raw!r} 非法（需 YYYY-MM-DD），改用默认 {default.date()}')
        return default


# 缺口范围可由 workflow 经 GAP_START_DATE / GAP_END_DATE 覆盖；默认沿用历史 Apr 13-25。
GAP_START = _gap_bound('GAP_START_DATE',
                       datetime(2026, 4, 13, tzinfo=archive_layout.BEIJING_TZ), False)
GAP_END = _gap_bound('GAP_END_DATE',
                     datetime(2026, 4, 25, 23, 59, 59, tzinfo=archive_layout.BEIJING_TZ), True)


def _archive_items(source: str, items: list[dict]):
    """Archive items into per-date files."""
    from collections import defaultdict
    by_date = defaultdict(list)
    for item in items:
        t = item.get('time', '')
        if not t:
            continue
        try:
            # 与 backfill_platforms / archive_platforms 统一分桶：基准经 archive_layout
            # 单一真相源（原三处各自手写换算，且对非 UTC 偏移会把偏移算两遍）
            date_str = archive_layout.archive_date_str(datetime.fromisoformat(t))
            by_date[date_str].append(item)
        except (ValueError, TypeError):  # 只挡坏时间戳，别的异常必须冒出来
            continue

    for date_str, date_items in by_date.items():
        platform, region, subtype = archive_layout.resolve_write_layout(source)
        out_path = ARCHIVE_DIR / archive_layout.build_relpath(platform, region, subtype, date_str)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        existing = []
        if out_path.exists():
            try:
                data = json.loads(out_path.read_text(encoding='utf-8'))
                existing = data.get('items', [])
                if data.get('_gap_repaired') and not existing:
                    existing = []
            except (OSError, json.JSONDecodeError, AttributeError):
                pass

        # 冷层旁车语义：本脚本按定义补的是历史缺口，落点几乎必在冷月——此时 out_path
        # 是 `.json.gz` 旁边的裸旁车，只能放 .gz 里没有的增量。不减去冷层条目，同一条
        # 在 .gz 与旁车各存一份，而读方（dated_files 冷热并出）两个都读 → 全量层双计。
        cold_items = archive_layout.read_cold_doc(out_path).get('items', [])

        # 去重键须对**无 url 条目**也成立（与 backfill_platforms._key /
        # archive_platforms.item_key 同构）。原写法只收非空 url 进集合、再拿
        # `i.get('url','')` 去比：url 缺失的条目（如 bilibili 的 arcurl 偶发为空）
        # 键恒为 '' 而 '' 从不在集合里，于是**每次重跑都被判为新条目再追加一遍**。
        # 本脚本的缺口窗口是固定日期区间、天生反复重跑，一条无 url 的条目跑 N 次就在
        # 全量档案层里躺 N 份，item_count 与真实条数一起失真。
        def _key(it: dict) -> str:
            return it.get('url', '').strip() or f"{it.get('title', '')[:60]}|{it.get('source', '')}"

        existing_urls = {_key(i) for i in existing if isinstance(i, dict)}
        existing_urls |= {_key(i) for i in cold_items if isinstance(i, dict)}
        new_items = []
        for i in date_items:
            k = _key(i)
            if k in existing_urls:
                continue
            existing_urls.add(k)   # 同轮内重复条目也只落一份
            new_items.append(i)

        if new_items:
            merged = existing + new_items
            out_data = {
                'date': date_str,
                'archived_at': datetime.now(UTC).isoformat(),
                'source': source,
                'item_count': len(merged),
                'items': merged,
            }
            news_common.dump_json_atomic(out_path, out_data)
            logger.info(f'  {source}/{date_str}: +{len(new_items)} items (total {len(merged)})')


def backfill_reddit():
    """Reddit: paginate /new.json with 'after' cursor to reach gap period."""
    from global_collectors import _make_item
    import requests

    items = []
    subreddits = ["Morimens", "MorimensGame"]

    for sub in subreddits:
        after = None
        for page in range(20):
            try:
                params = {'limit': 100, 'sort': 'new', 'raw_json': 1}
                if after:
                    params['after'] = after
                resp = requests.get(
                    f'https://www.reddit.com/r/{sub}/new.json',
                    params=params,
                    headers={'User-Agent': f'backfill-gap/1.0 (r/{sub})'},
                    timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning(f'Reddit r/{sub} p{page}: HTTP {resp.status_code}')
                    break
                data = resp.json()
                posts = data.get('data', {}).get('children', [])
                if not posts:
                    break

                found_in_gap = 0
                oldest = None
                for post in posts:
                    d = post['data']
                    created = datetime.fromtimestamp(d['created_utc'], tz=UTC)
                    oldest = created
                    if created < GAP_START:
                        continue
                    if created > GAP_END:
                        continue
                    if sub.lower() not in ('morimens', 'morimensgame'):
                        continue
                    items.append(_make_item(
                        title=d['title'],
                        summary=(d.get('selftext') or '')[:300],
                        source='reddit',
                        platform_region='global',
                        time_str=created.isoformat(),
                        url=f"https://reddit.com{d['permalink']}",
                        engagement=d.get('score', 0) + d.get('num_comments', 0),
                        is_hot=d.get('score', 0) > 50,
                        author=d.get('author', ''),
                        lang='en',
                    ))
                    found_in_gap += 1

                after = data.get('data', {}).get('after')
                logger.info(f'Reddit r/{sub} p{page}: {found_in_gap} in gap (oldest: {oldest.date() if oldest else "?"})')

                if oldest and oldest < GAP_START:
                    break
                if not after:
                    break

                time.sleep(1)
            except Exception as e:
                logger.warning(f'Reddit r/{sub} p{page} failed: {e}')
                break

    if items:
        _archive_items('reddit', items)
    logger.info(f'Reddit total: {len(items)} items in gap period')
    return len(items)


def backfill_youtube():
    """YouTube: use publishedAfter/Before to target gap period exactly."""
    from global_collectors import _get, _make_item

    api_key = os.environ.get('YOUTUBE_API_KEY')
    if not api_key:
        logger.info('YouTube: no API key, skipping')
        return 0

    items = []
    for keyword in ['Morimens', '忘却前夜', '忘卻前夜']:
        try:
            data = _get(
                'https://www.googleapis.com/youtube/v3/search',
                params={
                    'part': 'snippet',
                    'q': keyword,
                    'type': 'video',
                    'order': 'date',
                    # RFC3339 的 'Z' 是**字面 UTC**：边界是北京时刻，必须先
                    # astimezone(UTC) 再格式化，否则墙钟原样贴个 Z 就整体偏 8 小时。
                    'publishedAfter': GAP_START.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'publishedBefore': GAP_END.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'maxResults': 50,
                    'key': api_key,
                },
            ).json()

            video_ids = [i['id']['videoId'] for i in data.get('items', []) if i.get('id', {}).get('videoId')]

            stats = {}
            if video_ids:
                stats_data = _get(
                    'https://www.googleapis.com/youtube/v3/videos',
                    params={'part': 'statistics', 'id': ','.join(video_ids), 'key': api_key},
                ).json()
                for v in stats_data.get('items', []):
                    s = v.get('statistics', {})
                    stats[v['id']] = int(s.get('viewCount', 0)) + int(s.get('likeCount', 0))

            for entry in data.get('items', []):
                vid = entry.get('id', {}).get('videoId', '')
                snippet = entry.get('snippet', {})
                items.append(_make_item(
                    title=snippet.get('title', ''),
                    summary=snippet.get('description', '')[:200],
                    source='youtube',
                    platform_region='global',
                    time_str=snippet.get('publishedAt', ''),
                    url=f'https://www.youtube.com/watch?v={vid}',
                    engagement=stats.get(vid, 0),
                    is_hot=stats.get(vid, 0) > 10000,
                    author=snippet.get('channelTitle', ''),
                    lang='',
                ))
            logger.info(f'YouTube "{keyword}": {len(data.get("items", []))} videos')
        except Exception as e:
            # H3: 异常文本含完整请求 URL（key=<API key>），脱敏后再进公开日志
            logger.warning(f'YouTube "{keyword}" failed: {news_common.redact_secrets(e)}')

    if items:
        _archive_items('youtube', items)
    logger.info(f'YouTube total: {len(items)} items in gap period')
    return len(items)


def backfill_bilibili():
    """Bilibili: search with multiple keywords, retry on 412."""
    from global_collectors import _get, _make_item, KEYWORDS

    items = []
    for keyword in KEYWORDS.get('zh', []) + KEYWORDS.get('en', []):
        for page in range(1, 6):
            try:
                data = _get(
                    'https://api.bilibili.com/x/web-interface/search/type',
                    params={'keyword': keyword, 'search_type': 'video', 'page': page, 'pagesize': 50, 'order': 'pubdate'},
                ).json()

                results = data.get('data', {}).get('result', [])
                if not results:
                    break

                found = 0
                for v in results:
                    pubdate = v.get('pubdate', 0)
                    if not pubdate:
                        continue
                    created = datetime.fromtimestamp(pubdate, tz=UTC)
                    if created < GAP_START or created > GAP_END:
                        continue
                    items.append(_make_item(
                        title=v.get('title', '').replace('<em class="keyword">', '').replace('</em>', ''),
                        summary=v.get('description', ''),
                        source='bilibili',
                        platform_region='cn',
                        time_str=created.isoformat(),
                        url=v.get('arcurl', ''),
                        engagement=v.get('play', 0) + v.get('favorites', 0),
                        is_hot=v.get('play', 0) > 10000,
                        author=v.get('author', ''),
                        lang='zh',
                    ))
                    found += 1

                logger.info(f'Bilibili "{keyword}" p{page}: {found} in gap')
                time.sleep(2)
            except Exception as e:
                logger.warning(f'Bilibili "{keyword}" p{page}: {e}')
                time.sleep(5)
                break

    if items:
        _archive_items('bilibili', items)
    logger.info(f'Bilibili total: {len(items)} items in gap period')
    return len(items)


def backfill_weibo():
    """Weibo: search with cookie if available."""
    from global_collectors import _make_item
    import requests

    cookie = os.environ.get('WEIBO_COOKIE', '')
    items = []

    for keyword in ['忘却前夜', 'Morimens']:
        for page in range(1, 6):
            try:
                headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://m.weibo.cn/'}
                if cookie:
                    headers['Cookie'] = cookie
                resp = requests.get(
                    'https://m.weibo.cn/api/container/getIndex',
                    params={'containerid': f'100103type=1&q={keyword}', 'page': page},
                    headers=headers,
                    timeout=15,
                )
                data = resp.json()
                cards = data.get('data', {}).get('cards', [])
                if not cards:
                    break

                found = 0
                for card in cards:
                    mblog = card.get('mblog', {})
                    if not mblog:
                        continue
                    created = mblog.get('created_at', '')
                    if not created:
                        continue
                    try:
                        from global_collectors import _parse_weibo_time
                        time_str, _ = _parse_weibo_time(created)
                    except Exception:
                        time_str = created

                    try:
                        dt = datetime.fromisoformat(time_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        if dt < GAP_START or dt > GAP_END:
                            continue
                    except (ValueError, TypeError):  # 只挡坏时间戳
                        continue

                    text = mblog.get('text', '')
                    text = re.sub(r'<[^>]+>', '', text)[:200]
                    items.append(_make_item(
                        title=text[:80],
                        summary=text,
                        source='weibo',
                        platform_region='cn',
                        time_str=time_str,
                        url=f"https://m.weibo.cn/detail/{mblog.get('id', '')}",
                        engagement=mblog.get('reposts_count', 0) + mblog.get('comments_count', 0) + mblog.get('attitudes_count', 0),
                        author=mblog.get('user', {}).get('screen_name', ''),
                        lang='zh',
                    ))
                    found += 1

                logger.info(f'Weibo "{keyword}" p{page}: {found} in gap')
                time.sleep(1.5)
            except Exception as e:
                logger.warning(f'Weibo "{keyword}" p{page}: {e}')
                break

    if items:
        _archive_items('weibo', items)
    logger.info(f'Weibo total: {len(items)} items in gap period')
    return len(items)


def backfill_steam_reviews():
    """Steam: paginate appreviews API backwards to reach gap period."""
    import subprocess as _sp
    from global_collectors import _make_item

    app_id = 3052450
    items = []
    cursor = '*'
    page = 0

    try:
        while page < 50:
            page += 1
            url = (
                f'https://store.steampowered.com/appreviews/{app_id}'
                f'?json=1&filter=recent&num_per_page=100&language=all&purchase_type=all'
                f'&cursor={quote(cursor, safe="")}'
            )
            result = _sp.run(
                ['curl', '-s', '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)', url],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning(f'Steam p{page}: curl failed')
                break
            if not result.stdout.strip():
                logger.warning(f'Steam p{page}: curl empty body')
                break
            data = json.loads(result.stdout)
            reviews = data.get('reviews', []) or []
            if not reviews:
                break

            found_in_gap = 0
            oldest = None
            for review in reviews:
                ts = review.get('timestamp_created', 0)
                created = datetime.fromtimestamp(ts, tz=UTC)
                oldest = created

                if created < GAP_START:
                    continue
                if created > GAP_END:
                    continue

                language = review.get('language', 'unknown')
                voted_up = review.get('voted_up', False)
                sentiment = '正面' if voted_up else '负面'
                review_text = review.get('review', '')
                summary_text = review_text[:50].strip()
                title = f'[{sentiment}] {summary_text}...' if len(review_text) > 50 else f'[{sentiment}] {summary_text}'

                author_info = review.get('author', {})
                steamid = author_info.get('steamid', '')
                votes_up = review.get('votes_up', 0)

                items.append(_make_item(
                    title=title,
                    summary=review_text[:300],
                    source='steam',
                    platform_region='global',
                    time_str=created.isoformat(),
                    url=f'https://steamcommunity.com/profiles/{steamid}/recommended/{app_id}',
                    engagement=votes_up,
                    is_hot=votes_up > 10,
                    author=steamid,
                    lang=language,
                ))
                found_in_gap += 1

            logger.info(f'Steam p{page}: {found_in_gap} in gap (oldest: {oldest.date() if oldest else "?"})')

            if oldest and oldest < GAP_START:
                break
            next_cursor = data.get('cursor')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(0.5)

    except Exception as e:
        logger.warning(f'Steam reviews backfill failed: {e}')

    if items:
        _archive_items('steam', items)
    logger.info(f'Steam total: {len(items)} items in gap period')
    return len(items)


def cleanup_empty_placeholders():
    """Remove empty placeholder files created by repair_gaps.py for the gap period."""
    removed = 0
    gap_dates = []
    d = GAP_START
    while d.date() <= GAP_END.date():
        gap_dates.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    for source_dir in sorted(ARCHIVE_DIR.iterdir()):
        if not source_dir.is_dir():
            continue
        for date_str in gap_dates:
            path = source_dir / f'{date_str}.json'
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data.get('_gap_repaired') and not data.get('items'):
                    path.unlink()
                    removed += 1
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
    logger.info(f'Cleaned up {removed} empty placeholder files')
    return removed


def main():
    logger.info(f'=== Gap Backfill: {GAP_START.date()} ~ {GAP_END.date()} ===')

    cleanup_empty_placeholders()

    total = 0
    total += backfill_reddit()
    total += backfill_youtube()
    total += backfill_bilibili()
    total += backfill_weibo()
    total += backfill_steam_reviews()

    logger.info(f'=== Gap backfill complete: {total} new items ===')


if __name__ == '__main__':
    main()
