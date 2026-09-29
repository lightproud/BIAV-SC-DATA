#!/usr/bin/env python3
"""
backfill_platforms.py — 多平台历史数据回溯采集

类似 Discord 归档器的双轨制：
  Track 1: 增量采集（由 collect_global.py 处理，每 6 小时）
  Track 2: 历史回溯（本脚本，每小时一次，每平台独立 agent，30 分钟限时）

状态文件: projects/news/data/backfill/state.json
  {
    "bilibili": {"page": 5, "done": false, "total": 230},
    "appstore": {"page": 3, "done": true, "total": 89},
    ...
  }

存储: 回溯数据直接写入 data/platforms/{source}/backfill-{batch}.json
      然后合并到对应日期文件

运行方式:
  python backfill_platforms.py                    # 所有平台各翻几页
  python backfill_platforms.py --platform bilibili  # 仅指定平台
  python backfill_platforms.py --pages 10           # 每平台翻10页（默认5）
  python backfill_platforms.py --status             # 显示回溯进度
"""

import json
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta, UTC
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATE_PATH = _REPO_ROOT / 'projects' / 'news' / 'data' / 'backfill' / 'state.json'

# Sibling scripts dir — global_collectors lives here
sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_layout  # noqa: E402  归档布局单一真相源（2026-07-02 P0-1）
import news_common  # noqa: E402  原子写单一真源（dump_json_atomic）
ARCHIVE_DIR = archive_layout.community_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认

# Max runtime per invocation (30 minutes, leaves buffer for workflow)
MAX_RUNTIME_SECONDS = 1800
_start_time = time.time()

REQUEST_DELAY = 1.5  # seconds between requests to avoid rate limits


def _is_time_up() -> bool:
    return (time.time() - _start_time) > MAX_RUNTIME_SECONDS


def _load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _platform_state(state: dict, name: str) -> dict:
    if name not in state:
        state[name] = {"page": 1, "done": False, "total": 0}
    return state[name]


def _archive_items(source: str, items: list[dict]):
    """Archive items into per-date files under <community_root>/<平台>[/区服][/类型]/。

    落点由 archive_layout.resolve_write_layout + build_relpath 单一真相源算出；
    旧注释写的 `data/platforms/{source}/` 已不是任何真实落点（读方照抄会全空）。
    """
    if not items:
        return

    # Group items by date
    by_date: dict[str, list] = {}
    for item in items:
        t = item.get('time', '')
        try:
            # 分桶基准走 archive_layout（日期 SSOT）：原手写换算对已带非 UTC 偏移的
            # 时间戳（jp 源常见 +09:00）会把偏移算两遍，整批落进错的日期桶。
            date_str = archive_layout.archive_date_str(datetime.fromisoformat(t))
        except (ValueError, TypeError):
            # 兜底日期原为 UTC 日期，与上面的北京日期桶名不同基准——每天有 8 小时
            # 会把坏时间戳的条目丢进相邻一天的桶里。
            date_str = archive_layout.archive_date_str()
        by_date.setdefault(date_str, []).append(item)

    for date_str, date_items in by_date.items():
        # 落点走布局单一真相源：分层平台（steam 家族/appstore/google_play/youtube）
        # 回填也落区服/类型子目录，与主线写方一致，杜绝平级文件复活（lesson #42）
        platform, region, subtype = archive_layout.resolve_write_layout(source)
        path = ARCHIVE_DIR / archive_layout.build_relpath(platform, region, subtype, date_str)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Merge with existing
        existing_items = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                existing_items = data.get('items', [])
            except (OSError, json.JSONDecodeError, AttributeError):
                pass

        # Dedup by URL or title
        def _key(it: dict) -> str:
            return it.get('url', '').strip() or f"{it.get('title', '')[:60]}|{it.get('source', '')}"

        # 冷层旁车语义：回填打到冷月时 path 是 .gz 旁边的裸旁车，只能放 .gz 里没有的
        # 增量。把冷层条目预置进 seen（但不写回旁车），否则同一条在 .gz 与旁车各一份,
        # 而读方 dated_files 冷热并出、两个都读 —— 全量档案层双计。
        seen = {_key(i) for i in archive_layout.read_cold_doc(path).get('items', [])
                if isinstance(i, dict)}
        merged = []
        for item in existing_items + date_items:
            key = _key(item)
            if key not in seen:
                seen.add(key)
                merged.append(item)

        if not merged:  # 全部已在冷层，无增量可落——不动既有文件
            continue

        merged.sort(key=lambda x: x.get('engagement', 0), reverse=True)

        archive_data = {
            'date': date_str,
            'archived_at': datetime.now(UTC).isoformat(),
            'source': source,
            'item_count': len(merged),
            'items': merged,
        }
        # 原子替换：直写若在中途被中断（回溯作业有 30 分钟硬预算，超时即被杀）
        # 会留下半截 JSON——读方 `except JSONDecodeError` 静默跳过，这一天的归档就此消失。
        news_common.dump_json_atomic(path, archive_data)


# ── Platform-specific backfill functions ──────────────────────────────────

def backfill_bilibili(state: dict, max_pages: int) -> int:
    """Backfill Bilibili search results page by page."""
    from global_collectors import _get, _make_item, KEYWORDS
    ps = _platform_state(state, 'bilibili')
    if ps['done']:
        return 0

    total = 0
    start_page = ps['page']

    for keyword in KEYWORDS['zh']:
        for page in range(start_page, start_page + max_pages):
            if _is_time_up():
                break
            try:
                data = _get(
                    "https://api.bilibili.com/x/web-interface/search/type",
                    params={"keyword": keyword, "search_type": "video", "page": page, "pagesize": 50,
                            "order": "pubdate"},
                ).json()

                results = data.get("data", {}).get("result", [])
                if not results:
                    ps['done'] = True
                    break

                items = []
                for v in results:
                    items.append(_make_item(
                        title=v.get("title", "").replace('<em class="keyword">', '').replace('</em>', ''),
                        summary=v.get("description", ""),
                        source="bilibili",
                        platform_region="cn",
                        time_str=datetime.fromtimestamp(v.get("pubdate", 0), tz=UTC).isoformat()
                            if v.get("pubdate") else "",
                        url=v.get("arcurl", ""),
                        engagement=v.get("play", 0) + v.get("favorites", 0),
                        is_hot=v.get("play", 0) > 10000,
                        author=v.get("author", ""),
                        lang="zh",
                    ))

                _archive_items('bilibili', items)
                total += len(items)
                ps['page'] = page + 1
                ps['total'] += len(items)
                _save_state(state)

                logger.info(f'Bilibili backfill p{page} "{keyword}": +{len(items)}')
                time.sleep(REQUEST_DELAY)

            except Exception as e:
                logger.warning(f'Bilibili backfill p{page} "{keyword}" failed: {e}')
                break

    return total


def backfill_appstore(state: dict, max_pages: int) -> int:
    """Backfill App Store reviews across all regions and pages."""
    from global_collectors import _get, _make_item
    ps = _platform_state(state, 'appstore')
    if ps['done']:
        return 0

    app_id = "6447354150"
    regions = ["cn", "us", "jp", "kr", "hk", "tw", "sg", "gb", "de", "fr", "ru", "th", "vn"]
    total = 0
    start_page = ps['page']

    for country in regions:
        for page in range(start_page, start_page + max_pages):
            if _is_time_up():
                break
            try:
                data = _get(
                    f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortBy=mostRecent/json",
                ).json()

                entries = data.get("feed", {}).get("entry", [])
                if not entries:
                    break

                items = []
                for entry in entries:
                    if isinstance(entry.get("im:rating"), dict):
                        rating = int(entry["im:rating"].get("label", "0"))
                    else:
                        continue
                    title = entry.get("title", {}).get("label", "") if isinstance(entry.get("title"), dict) else ""
                    content = entry.get("content", {}).get("label", "") if isinstance(entry.get("content"), dict) else ""
                    author_name = ""
                    if isinstance(entry.get("author"), dict):
                        author_name = entry["author"].get("name", {}).get("label", "")

                    sentiment = '好评' if rating >= 4 else ('中评' if rating == 3 else '差评')
                    items.append(_make_item(
                        title=title or f"[{sentiment}] ★{rating}",
                        summary=content,
                        source="appstore",
                        platform_region=country,
                        time_str=entry.get("updated", {}).get("label", "")
                            if isinstance(entry.get("updated"), dict) else "",
                        url="",
                        engagement=rating,
                        is_hot=False,
                        author=author_name,
                        lang="",
                    ))

                _archive_items('appstore', items)
                total += len(items)
                ps['total'] += len(items)
                logger.info(f'App Store backfill {country} p{page}: +{len(items)}')
                time.sleep(REQUEST_DELAY)

            except Exception as e:
                logger.warning(f'App Store backfill {country} p{page} failed: {e}')
                break

    ps['page'] = start_page + max_pages
    if start_page + max_pages > 10:  # App Store max ~10 pages per region
        ps['done'] = True
    _save_state(state)
    return total


def backfill_arca_live(state: dict, max_pages: int) -> int:
    """Backfill Arca.live forgettingeve channel page by page."""
    from global_collectors import _get, _make_item
    import re as _re
    ps = _platform_state(state, 'arca_live')
    if ps['done']:
        return 0

    channel = "forgettingeve"
    total = 0
    start_page = ps['page']

    for page in range(start_page, start_page + max_pages):
        if _is_time_up():
            break
        try:
            resp = _get(
                f"https://arca.live/b/{channel}",
                params={"p": page},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            html = resp.text

            items = []
            for match in _re.finditer(
                r'data-url="(/b/[^"]+/(\d+))"[^>]*>.*?'
                r'class="title"[^>]*>([^<]+)</a>.*?'
                r'class="col-time"[^>]*>([^<]+)',
                html, _re.DOTALL
            ):
                path, article_id, title, time_text = match.groups()
                title = title.strip()
                if not title:
                    continue
                items.append(_make_item(
                    title=title,
                    summary="",
                    source="arca_live",
                    platform_region="kr",
                    time_str=time_text.strip(),
                    url=f"https://arca.live{path}",
                    engagement=0,
                    is_hot=False,
                    author="",
                    lang="ko",
                ))

            if not items:
                ps['done'] = True
                break

            _archive_items('arca_live', items)
            total += len(items)
            ps['page'] = page + 1
            ps['total'] += len(items)
            _save_state(state)

            logger.info(f'Arca.live backfill p{page}: +{len(items)}')
            time.sleep(REQUEST_DELAY)

        except Exception as e:
            logger.warning(f'Arca.live backfill p{page} failed: {e}')
            break

    return total


def backfill_steam_reviews(state: dict, max_pages: int) -> int:
    """Backfill all Steam reviews using cursor pagination."""
    import subprocess as _sp
    from global_collectors import _make_item
    ps = _platform_state(state, 'steam_review')
    if ps['done']:
        return 0

    app_id = 3052450
    cursor = ps.get('cursor', '*')
    total = 0

    for _ in range(max_pages):
        if _is_time_up():
            break
        try:
            import urllib.parse
            encoded_cursor = urllib.parse.quote(cursor, safe='')
            url = (f'https://store.steampowered.com/appreviews/{app_id}?json=1'
                   f'&filter=recent&num_per_page=100&language=all&purchase_type=all'
                   f'&cursor={encoded_cursor}')

            result = _sp.run(
                ['curl', '-s', '-H', 'User-Agent: Mozilla/5.0', url],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                break
            if not result.stdout.strip():
                break
            data = json.loads(result.stdout)

            reviews = data.get('reviews', [])
            if not reviews:
                ps['done'] = True
                break

            new_cursor = data.get('cursor', '')
            if not new_cursor or new_cursor == cursor:
                ps['done'] = True
                break

            items = []
            for review in reviews:
                ts = review.get('timestamp_created', 0)
                created = datetime.fromtimestamp(ts, tz=UTC)
                voted_up = review.get('voted_up', False)
                sentiment = '正面' if voted_up else '负面'
                review_text = review.get('review', '')
                language = review.get('language', 'unknown')
                author_info = review.get('author', {})
                steamid = author_info.get('steamid', '')
                votes_up = review.get('votes_up', 0)

                items.append(_make_item(
                    title=f'[{sentiment}] {review_text[:50]}...' if len(review_text) > 50
                          else f'[{sentiment}] {review_text}',
                    summary=review_text,
                    source="steam_review",
                    platform_region="global",
                    time_str=created.isoformat(),
                    url=f'https://steamcommunity.com/profiles/{steamid}/recommended/{app_id}',
                    engagement=votes_up,
                    is_hot=votes_up > 10,
                    author=steamid,
                    lang=language,
                ))

            # Steam 评论归一进 steam/（item.source 仍为 steam_review，靠 SOURCE_ALIASES 显示归一）。
            # 2026-06-15 起不再单独写 steam_review/；历史归档已合并入 steam/。
            _archive_items('steam', items)
            total += len(items)
            cursor = new_cursor
            ps['cursor'] = cursor
            ps['page'] += 1
            ps['total'] += len(items)
            _save_state(state)

            logger.info(f'Steam reviews backfill batch {ps["page"]}: +{len(items)}')
            time.sleep(REQUEST_DELAY)

        except Exception as e:
            logger.warning(f'Steam reviews backfill failed: {e}')
            break

    return total


def backfill_pixiv(state: dict, max_pages: int) -> int:
    """Backfill Pixiv search results page by page."""
    from global_collectors import _get, _make_item, KEYWORDS
    ps = _platform_state(state, 'pixiv')
    if ps['done']:
        return 0

    total = 0
    start_page = ps['page']

    for keyword in KEYWORDS['ja'] + KEYWORDS['zh']:
        for page in range(start_page, start_page + max_pages):
            if _is_time_up():
                break
            try:
                data = _get(
                    "https://www.pixiv.net/ajax/search/artworks/" + keyword,
                    params={"p": page, "order": "date_d", "mode": "all", "s_mode": "s_tag"},
                    headers={"Referer": "https://www.pixiv.net", "User-Agent": "Mozilla/5.0"},
                )
                if data.status_code != 200:
                    break

                result = data.json()
                works = result.get("body", {}).get("illustManga", {}).get("data", []) or []
                if not works:
                    ps['done'] = True
                    break

                items = []
                for work in works:
                    items.append(_make_item(
                        title=work.get("title", ""),
                        summary=work.get("description", ""),
                        source="pixiv",
                        platform_region="jp",
                        time_str=work.get("createDate", ""),
                        url=f"https://www.pixiv.net/artworks/{work.get('id', '')}",
                        engagement=work.get("bookmarkCount", 0),
                        is_hot=work.get("bookmarkCount", 0) > 100,
                        author=work.get("userName", ""),
                        lang="ja",
                    ))

                _archive_items('pixiv', items)
                total += len(items)
                ps['page'] = page + 1
                ps['total'] += len(items)
                _save_state(state)

                logger.info(f'Pixiv backfill "{keyword}" p{page}: +{len(items)}')
                time.sleep(REQUEST_DELAY)

            except Exception as e:
                logger.warning(f'Pixiv backfill "{keyword}" p{page} failed: {e}')
                break

    return total


def backfill_ruliweb(state: dict, max_pages: int) -> int:
    """Backfill Ruliweb search results page by page."""
    from global_collectors import _get, _make_item, KEYWORDS
    import re as _re
    ps = _platform_state(state, 'ruliweb')
    if ps['done']:
        return 0

    total = 0
    start_page = ps['page']

    for keyword in KEYWORDS['ko']:
        for page in range(start_page, start_page + max_pages):
            if _is_time_up():
                break
            try:
                resp = _get(
                    "https://bbs.ruliweb.com/search",
                    params={"q": keyword, "page": page},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                html = resp.text

                items = []
                # Extract dates near each search result (Ruliweb shows dates like "YYYY.MM.DD")
                result_dates = _re.findall(
                    r'class="[^"]*date[^"]*"[^>]*>([^<]+)<',
                    html,
                )

                result_idx = 0
                for match in _re.finditer(
                    r'class="subject_link"[^>]*href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
                    html
                ):
                    url, title = match.groups()
                    title = title.strip()
                    if not title:
                        continue
                    if not url.startswith("http"):
                        url = f"https://bbs.ruliweb.com{url}"

                    time_str = datetime.now(UTC).isoformat()
                    time_approx = True
                    if result_idx < len(result_dates):
                        date_text = result_dates[result_idx].strip()
                        date_m = _re.match(r'(\d{4})\.(\d{1,2})\.(\d{1,2})', date_text)
                        if date_m:
                            try:
                                dt = datetime(int(date_m.group(1)), int(date_m.group(2)),
                                              int(date_m.group(3)), tzinfo=UTC)
                                time_str = dt.isoformat()
                                time_approx = False
                            except ValueError:
                                pass

                    item = _make_item(
                        title=title,
                        summary="",
                        source="ruliweb",
                        platform_region="kr",
                        time_str=time_str,
                        url=url,
                        engagement=0,
                        is_hot=False,
                        author="",
                        lang="ko",
                        time_is_approximate=time_approx,
                    )
                    items.append(item)
                    result_idx += 1

                if not items:
                    ps['done'] = True
                    break

                _archive_items('ruliweb', items)
                total += len(items)
                ps['page'] = page + 1
                ps['total'] += len(items)
                _save_state(state)

                logger.info(f'Ruliweb backfill "{keyword}" p{page}: +{len(items)}')
                time.sleep(REQUEST_DELAY)

            except Exception as e:
                logger.warning(f'Ruliweb backfill "{keyword}" p{page} failed: {e}')
                break

    return total


def backfill_weixin(state: dict, max_pages: int) -> int:
    """Backfill Sogou WeChat search results page by page."""
    from global_collectors import _get, _make_item, KEYWORDS
    import re as _re
    ps = _platform_state(state, 'weixin')
    if ps['done']:
        return 0

    total = 0
    start_page = ps['page']

    for keyword in KEYWORDS['zh']:
        for page in range(start_page, start_page + max_pages):
            if _is_time_up():
                break
            try:
                resp = _get(
                    "https://weixin.sogou.com/weixin",
                    params={"type": 2, "query": keyword, "ie": "utf8", "page": page},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://weixin.sogou.com/"},
                )
                html = resp.text

                # Extract Sogou timestamps (timeConvert('EPOCH') or data-t="EPOCH")
                sogou_timestamps = _re.findall(
                    r"(?:timeConvert\(['\"](\d{10})['\"]|data-t=['\"](\d{10})['\"]"
                    r"|lastModified['\"]?\s*[:=]\s*['\"]?(\d{10}))",
                    html,
                )
                ts_list = [int(t1 or t2 or t3) for t1, t2, t3 in sogou_timestamps]

                items = []
                result_idx = 0
                for match in _re.finditer(
                    r'<h3>.*?<a[^>]*href="([^"]+)"[^>]*>(.+?)</a>',
                    html, _re.DOTALL
                ):
                    url, title_html = match.groups()
                    title = _re.sub(r'<[^>]+>', '', title_html).strip()
                    if not title:
                        continue

                    time_str = datetime.now(UTC).isoformat()
                    time_approx = True
                    if result_idx < len(ts_list):
                        try:
                            dt = datetime.fromtimestamp(ts_list[result_idx], tz=UTC)
                            time_str = dt.isoformat()
                            time_approx = False
                        except (ValueError, OSError):
                            pass

                    item = _make_item(
                        title=f"[微信] {title}",
                        summary="",
                        source="weixin",
                        platform_region="cn",
                        time_str=time_str,
                        url=url,
                        engagement=0,
                        is_hot=False,
                        author="",
                        lang="zh",
                        time_is_approximate=time_approx,
                    )
                    items.append(item)
                    result_idx += 1

                if not items:
                    ps['done'] = True
                    break

                _archive_items('weixin', items)
                total += len(items)
                ps['page'] = page + 1
                ps['total'] += len(items)
                _save_state(state)

                logger.info(f'WeChat backfill "{keyword}" p{page}: +{len(items)}')
                time.sleep(REQUEST_DELAY)

            except Exception as e:
                logger.warning(f'WeChat backfill "{keyword}" p{page} failed: {e}')
                break

    return total


def backfill_taptap(state: dict, max_pages: int) -> int:
    """TapTap 帖子/评价历史回溯。

    TapTap 无可用官方 API（webapiv2 端点全 404），复用 taptap_collector 的
    Playwright 滚动深采：回溯模式下用深 cutoff（180 天）+ 放大滚动轮次
    （max_pages*4），一次尽量多抓历史，并绕过增量短路（backfill=True）。
    帖子归 taptap/、评价归 taptap_review/，与日常增量同目录、按 URL 去重合并。

    注意：实际可补深度受 TapTap 懒加载放出的历史上限约束，无法保证补到任意
    久远；浏览器采集需 CI 带 Chromium 环境，本地无浏览器时返回 0 不报错。
    """
    ps = _platform_state(state, 'taptap')
    cutoff = datetime.now(UTC) - timedelta(days=180)
    try:
        import asyncio
        import taptap_collector
        topics, reviews = asyncio.run(
            taptap_collector.collect(
                cutoff=cutoff, max_scrolls=max_pages * 4, backfill=True
            )
        )
    except Exception as e:
        logger.warning(f'TapTap backfill failed (browser env required): {e}')
        return 0

    _archive_items('taptap', topics)
    _archive_items('taptap_review', reviews)
    count = len(topics) + len(reviews)
    ps['total'] = ps.get('total', 0) + count
    ps['page'] = ps.get('page', 1) + 1
    ps['last_run'] = datetime.now(UTC).isoformat()
    return count


# ── Registry ──────────────────────────────────────────────────────────────

BACKFILL_REGISTRY = {
    'bilibili': backfill_bilibili,
    'appstore': backfill_appstore,
    'steam_review': backfill_steam_reviews,
    # arca_live 已摘除（守密人 2026-08-16 裁定）：backfill_arca_live 实现保留在本模块
    # 待复用，但不再登记——CF 封死 Actions 出口，登记着只会让手动回填每次空手而归。
    'pixiv': backfill_pixiv,
    'ruliweb': backfill_ruliweb,
    'weixin': backfill_weixin,
    'taptap': backfill_taptap,
}


def show_status(state: dict):
    """Display backfill progress for all platforms."""
    print('=== 历史回溯进度 ===\n')
    for name in BACKFILL_REGISTRY:
        ps = state.get(name, {"page": 1, "done": False, "total": 0})
        status = '完成' if ps.get('done') else f'第 {ps.get("page", 1)} 页'
        total = ps.get('total', 0)
        print(f'  {name:15s}  {status:12s}  共 {total:5d} 条')
    print()


def main():
    parser = argparse.ArgumentParser(description='多平台历史数据回溯采集')
    parser.add_argument('--platform', type=str, default=None, help='仅回溯指定平台')
    parser.add_argument('--pages', type=int, default=5, help='每平台翻几页（默认5）')
    parser.add_argument('--status', action='store_true', help='显示回溯进度')
    args = parser.parse_args()

    state = _load_state()

    if args.status:
        show_status(state)
        return

    logger.info('=== 历史回溯采集开始 ===')

    if args.platform:
        if args.platform not in BACKFILL_REGISTRY:
            logger.error(f'未知平台: {args.platform}，可选: {", ".join(BACKFILL_REGISTRY.keys())}')
            return
        fn = BACKFILL_REGISTRY[args.platform]
        count = fn(state, args.pages)
        logger.info(f'{args.platform}: +{count} items')
    else:
        total = 0
        for name, fn in BACKFILL_REGISTRY.items():
            if _is_time_up():
                logger.warning('运行时间已达上限，剩余平台下次继续')
                break
            ps = _platform_state(state, name)
            if ps.get('done'):
                logger.info(f'{name}: 已完成')
                continue
            count = fn(state, args.pages)
            total += count
            logger.info(f'{name}: +{count} items')

        logger.info(f'=== 历史回溯完成: +{total} items ===')

    show_status(state)


if __name__ == '__main__':
    main()
