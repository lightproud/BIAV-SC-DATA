#!/usr/bin/env python3
"""
忘却前夜 Morimens - 全球信息收集器
从全球多个社区平台收集忘却前夜相关信息，输出结构化 JSON 数据。

支持平台 (29个):
  中文: Bilibili, NGA, TapTap, Weibo, Xiaohongshu, Douyin, Tieba, QQ频道, Zhihu, Bahamut(巴哈姆特)
  同人: Pixiv, Lofter
  周边: 闲鱼, 淘宝
  全球: Reddit, Twitter/X, YouTube, Discord, Facebook, TikTok, Twitch, Instagram
  韩国: Naver Cafe, Arca.live
  日本: 5ch
  商店: App Store, Google Play

使用: python scripts/collector.py
输出: data/collected_raw.json
"""

import asyncio
import hashlib
import html as _html
import json
import os
import re
import sys
import logging
import time
from datetime import datetime, timezone, timedelta, UTC
from pathlib import Path


import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # 采集层共享工具（HTTP/HTML-strip/item 单一真源，ARCH-01/02）
from sources import REGION_APPS  # 区服 app 标识单一真相源（2026-06-21 采集源命名规范）

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("collector")

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_PATH = BASE_DIR / "data" / "collected_raw.json"

# Adaptive lookback: expands automatically if CI was down
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_state import get_lookback_hours
    HOURS_LOOKBACK = news_common.env_int("HOURS_LOOKBACK", 0) or get_lookback_hours()
except ImportError:
    HOURS_LOOKBACK = news_common.env_int("HOURS_LOOKBACK", 24)
CUTOFF = datetime.now(UTC) - timedelta(hours=HOURS_LOOKBACK)


def _refresh_cutoff():
    """Refresh the global CUTOFF so long-running processes (scheduler) use current time."""
    global CUTOFF
    CUTOFF = datetime.now(UTC) - timedelta(hours=HOURS_LOOKBACK)

# 微博搜索每个关键词最多向后翻几页。取 5：单页约十余条，5 页 ≈ 每关键词每轮 50~70
# 条，足以覆盖高峰日的刷新速度；再深就开始大量撞到时窗外的旧贴，纯烧请求。
WEIBO_MAX_PAGES = int(os.environ.get("WEIBO_MAX_PAGES") or 5)

# 多语言搜索关键词
KEYWORDS = {
    "zh": ["忘却前夜", "忘卻前夜"],
    "en": ["Morimens", "morimens"],
    "ja": ["忘却前夜", "モリメンス"],
    "ko": ["망각전야", "모리멘스", "Morimens"],
    "vi": ["Morimens"],
    "th": ["Morimens"],
    "es": ["Morimens"],
    "pt": ["Morimens"],
    "ru": ["Morimens"],
    "de": ["Morimens"],
    "fr": ["Morimens"],
}
ALL_KEYWORDS = [kw for group in KEYWORDS.values() for kw in group]

# 通用请求 headers
DEFAULT_HEADERS = {"User-Agent": "MorimensReportBot/2.0"}


# ─── 工具函数 ───────────────────────────────────────────────

def _get(url, params=None, headers=None, timeout=15):
    """带重试的 GET 请求 (间隔 1s/2s)。委托 news_common.get_with_retry（单一真源）。"""
    return news_common.get_with_retry(
        url, params=params, headers=headers, timeout=timeout,
        default_headers=DEFAULT_HEADERS,
    )


def _get_cf(url, params=None, headers=None, timeout=15):
    """GET request using cloudscraper for Cloudflare-protected sites."""
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'linux'})
        h = {**DEFAULT_HEADERS, **(headers or {})}
        resp = scraper.get(url, params=params, headers=h, timeout=timeout)
        resp.raise_for_status()
        return resp
    except ImportError:
        logger.warning("cloudscraper not installed, falling back to requests")
        return _get(url, params=params, headers=headers, timeout=timeout)


def _post(url, json_data=None, headers=None, timeout=30):
    """带重试的 POST 请求 (间隔 1s/2s)。"""
    h = {**DEFAULT_HEADERS, **(headers or {})}
    for attempt in range(3):
        try:
            resp = requests.post(url, json=json_data, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt == 2:
                raise
            logger.debug(f"Retry {attempt + 1} for {url}: {e}")
            time.sleep(attempt + 1)
    # 循环内每条路径都 return 或 raise，落到这里说明重试上限与 `attempt == 2` 守卫
    # 被改得不再匹配。不加这一行的话，函数会静默返回 None，调用方在 resp.json() 处
    # 才炸出一句与真因无关的 AttributeError。
    raise RuntimeError(f"_post 重试逻辑不自洽：未返回也未抛出（url={url}）")


def _strip_html(text):
    """移除 HTML 标签。委托 news_common.strip_html（单一真源）。"""
    return news_common.strip_html(text)


# 创建标准化信息条目：直接复用 news_common.make_item（单一真源，签名等价）。
_make_item = news_common.make_item


# ─── 数据源采集器 ──────────────────────────────────────────

def _strip_html_tags(html: str) -> str:
    """Remove HTML tags and return plain text. 委托 news_common.strip_html。"""
    return news_common.strip_html(html).strip()


def _parse_reddit_rss(xml_text: str, sub: str) -> list:
    """Parse Reddit Atom RSS feed and return list of items."""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    # 远端不可信 XML 走共享护栏（体积上限 + 拒 DOCTYPE/ENTITY），见 news_common
    root = news_common.parse_xml_safely(xml_text)
    items = []

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        link_el = entry.find("atom:link", ns)
        updated_el = entry.find("atom:updated", ns)
        author_el = entry.find("atom:author/atom:name", ns)
        content_el = entry.find("atom:content", ns)

        title = title_el.text if title_el is not None else ""
        link = link_el.get("href", "") if link_el is not None else ""
        updated_str = updated_el.text if updated_el is not None else ""
        author = author_el.text if author_el is not None else ""
        content_html = content_el.text if content_el is not None else ""

        # Parse ISO timestamp
        if not updated_str:
            continue
        try:
            created = datetime.fromisoformat(updated_str)
        except ValueError:
            continue
        if created < CUTOFF:
            continue

        # Filter non-dedicated subreddits by keyword
        if sub.lower() not in ("morimens", "morimensgame"):
            title_lower = (title or "").lower()
            if not any(kw.lower() in title_lower for kw in ALL_KEYWORDS):
                continue

        summary = _strip_html_tags(content_html) if content_html else ""

        items.append(_make_item(
            title=title,
            summary=summary,
            source="reddit",
            platform_region="global",
            time_str=created.isoformat(),
            url=link,
            engagement=0,
            is_hot=False,
            author=f"u/{author}" if author else "",
            tags=[],
            lang="en",
        ))

    return items


# NOTE: divergent from aggregator_collectors.fetch_reddit — see audit ARCH-01.
# 该栈是「广覆盖」实现（RSS 优先、无评论抓取）；aggregator 栈是「富数据」实现
# （JSON 分页 + 评论 + 媒体 + search 回退，117 行 vs 54 行）。行为不同，不强行合并。
def fetch_reddit(subreddits=None):
    """从 Reddit 获取热门帖子（公开 JSON API，无需认证；失败时回退到 RSS）。"""
    subreddits = subreddits or ["Morimens", "MorimensGame", "gachagaming"]
    items = []

    for sub in subreddits:
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit=30"
            data = _get(url).json()
            posts = data.get("data", {}).get("children", [])

            for post in posts:
                d = post["data"]
                created = datetime.fromtimestamp(d["created_utc"], tz=UTC)
                if created < CUTOFF:
                    continue

                # 对 gachagaming 等综合版块，只取相关帖子
                if sub.lower() not in ("morimens", "morimensgame"):
                    title_lower = d["title"].lower()
                    if not any(kw.lower() in title_lower for kw in ALL_KEYWORDS):
                        continue

                score = d.get("score", 0)
                comments = d.get("num_comments", 0)
                items.append(_make_item(
                    title=d["title"],
                    summary=(d.get("selftext") or ""),
                    source="reddit",
                    platform_region="global",
                    time_str=created.isoformat(),
                    url=f"https://reddit.com{d['permalink']}",
                    engagement=score + comments,
                    is_hot=score > 100,
                    author=f"u/{d.get('author', '')}",
                    tags=[f.get("text", "") for f in d.get("link_flair_richtext", []) if f.get("text")],
                    lang="en",
                ))

            logger.info(f"Reddit r/{sub}: {len(items)} items collected (JSON)")
        except Exception as e:
            logger.warning(f"Reddit r/{sub} JSON API failed: {e}, trying RSS fallback")
            try:
                rss_url = f"https://www.reddit.com/r/{sub}/.rss"
                rss_resp = _get(rss_url)
                rss_items = _parse_reddit_rss(rss_resp.text, sub)
                items.extend(rss_items)
                logger.info(f"Reddit r/{sub}: {len(rss_items)} items collected (RSS fallback)")
            except Exception as rss_e:
                logger.warning(f"Reddit r/{sub} RSS fallback also failed: {rss_e}")

    return items


# NOTE: divergent from aggregator_collectors.fetch_bilibili — see audit ARCH-01 (behavior differs, not merged).
def fetch_bilibili():
    """从 Bilibili 搜索忘却前夜相关视频。

    搜索接口需 wbi 签名 + 服务端签发 buvid（spi），否则返回风控 HTML。
    签名实现共享自 news_common（与 aggregator 栈同源）。
    """
    items = []
    headers = {
        "Referer": "https://www.bilibili.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    spi = news_common.bilibili_spi_cookies(headers)
    if spi:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in spi.items())
    mixin_key = news_common.get_wbi_mixin_key(headers)

    for keyword in KEYWORDS["zh"]:
        try:
            params = {"search_type": "video", "keyword": keyword, "order": "pubdate", "page": 1}
            if mixin_key:
                params = news_common.sign_wbi_params(params, mixin_key)
            data = _get(
                "https://api.bilibili.com/x/web-interface/wbi/search/type",
                params=params,
                headers=headers,
            ).json()

            for v in (data.get("data", {}).get("result") or [])[:25]:
                pubdate = v.get("pubdate", 0)
                if not pubdate:
                    continue
                created = datetime.fromtimestamp(pubdate, tz=UTC)
                if created < CUTOFF:
                    continue

                play = v.get("play", 0)
                items.append(_make_item(
                    title=v.get("title", ""),
                    summary=v.get("description", ""),
                    source="bilibili",
                    platform_region="cn",
                    time_str=created.isoformat(),
                    url=v.get("arcurl", ""),
                    engagement=play + v.get("danmaku", 0),
                    is_hot=play > 10000,
                    author=v.get("author", ""),
                    tags=[v.get("typename", "")] if v.get("typename") else [],
                    lang="zh",
                    content_type="video",
                    media_url=v.get("pic", ""),
                ))

            logger.info(f'Bilibili "{keyword}": {len(items)} items')
        except Exception as e:
            logger.warning(f'Bilibili "{keyword}" failed: {e}')

    return items


# 官方 X 账号 → 区服（global = @MorimensOfcl / jp = @bokyakuzenya，AltPlus 日本版独立账号）。
# twitter 已摘除源注册表（守密人 2026-07-30 裁定：1,126 天零产出）——句柄/区服知识随之
# 从 sources.REGION_APPS 内聚到本模块（fetch_twitter 保留但退出编排，经 TWITTER_HANDLES
# 环境变量仍可手动唤起）；重新登记采集时应把这两处迁回注册表单一真相源。
_TWITTER_REGION_BY_HANDLE = {'morimensofcl': 'global', 'bokyakuzenya': 'jp'}
# 默认清单置空（不再随注册表派生）；可用 TWITTER_HANDLES 环境变量覆盖（逗号分隔）。
TWITTER_OFFICIAL_HANDLES: list[str] = []


def _parse_twitter_time(created_at):
    """解析 X 的 created_at（'Fri May 22 10:29:33 +0000 2026'）为 ISO 串。"""
    try:
        return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except (ValueError, TypeError):
        return None


def _twitter_walk_tweets(obj, acc):
    """递归收集 __NEXT_DATA__ 里所有含 full_text 的推文对象。"""
    if isinstance(obj, dict):
        if "full_text" in obj and "id_str" in obj:
            acc.append(obj)
        for v in obj.values():
            _twitter_walk_tweets(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _twitter_walk_tweets(v, acc)


def fetch_twitter():
    """从 X/Twitter 抓取官方账号时间线。

    无需 API Key：走 X 自家的嵌入式时间线接口
    syndication.twitter.com/srv/timeline-profile/screen-name/<handle>，
    解析页面内嵌的 __NEXT_DATA__ JSON。仅覆盖**官方账号公告**这一面。

    局限（务实标注）：该端点只回单账号时间线，**无法做关键词搜索**（玩家提及
    需官方 API v2 recent search，付费档）；nitter 公开实例已基本全灭，无可靠
    免费搜索替代。仅抓取公开可见推文，只读，不做任何用户面操作。

    合规：自动化访问 X 端点受 X 服务条款约束（与数据公开/内部无关）。本采集器
    仅用于第一方监测自家游戏官方号，限速、只读、不转售、不绕过登录墙。
    """
    handles = [h.strip() for h in os.environ.get(
        "TWITTER_HANDLES", ",".join(TWITTER_OFFICIAL_HANDLES)
    ).split(",") if h.strip()]

    items = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    for idx, handle in enumerate(handles):
        if idx > 0:
            time.sleep(1.0)  # 限速：账号间隔 1s
        # 区服：按 handle 查单一真相源；未登记的自定义 handle → None（archive 回落扁平）
        region = _TWITTER_REGION_BY_HANDLE.get(handle.lower().lstrip("@"))
        try:
            resp = _get(
                f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}",
                headers=headers,
            )
            m = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                resp.text, re.DOTALL,
            )
            if not m:
                logger.warning(f'Twitter @{handle}: no __NEXT_DATA__ payload')
                continue

            entries = (json.loads(m.group(1)).get("props", {})
                       .get("pageProps", {}).get("timeline", {}).get("entries", []))
            tweets = []
            _twitter_walk_tweets(entries, tweets)

            handle_added = 0
            for tw in tweets:
                iso = _parse_twitter_time(tw.get("created_at", ""))
                if not iso:
                    continue
                created = datetime.fromisoformat(iso)
                if created < CUTOFF:
                    continue
                text = tw.get("full_text", "") or ""
                tid = tw.get("id_str", "")
                likes = tw.get("favorite_count", 0) or 0
                rts = tw.get("retweet_count", 0) or 0
                replies = tw.get("reply_count", 0) or 0
                user = tw.get("user", {}) or {}
                screen = user.get("screen_name", handle)
                # 媒体（首图）
                media_url = ""
                media = ((tw.get("entities", {}) or {}).get("media")
                         or (tw.get("extended_entities", {}) or {}).get("media") or [])
                if media:
                    media_url = media[0].get("media_url_https", "") or ""

                items.append(_make_item(
                    title=text[:100],
                    summary=text,
                    source="twitter",
                    platform_region=region or "global",
                    time_str=iso,
                    url=f"https://x.com/{screen}/status/{tid}" if tid else f"https://x.com/{screen}",
                    engagement=likes + rts * 3 + replies * 2,
                    is_hot=(likes + rts) > 500,
                    author=f"@{screen}",
                    lang=user.get("lang", "") or "",
                    content_type="image" if media_url else "text",
                    media_url=media_url,
                    region=region,  # 甲方案：global/jp 驱动 archive 分桶；None 则回落扁平
                ))
                handle_added += 1

            logger.info(f'Twitter @{handle}: +{handle_added} tweets ({len(tweets)} in timeline)')
        except Exception as e:
            logger.warning(f'Twitter @{handle} failed: {e}')

    logger.info(f'Twitter: {len(items)} tweets total')
    return items
# NOTE: divergent from aggregator_collectors.fetch_youtube — see audit ARCH-01 (behavior differs, not merged).
def fetch_youtube():
    """YouTube 视频：社区关键词流（global）+ 日本官方频道（jp）（甲方案双源，归档子类 video）。"""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        logger.info("YouTube: YOUTUBE_API_KEY not set, skipping")
        return []

    items = []
    published_after = CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ")
    # global：社区关键词搜索（多频道社区视频流）
    for keyword in ["Morimens", "忘却前夜"]:
        items.extend(_fetch_youtube_videos(api_key, published_after, {"q": keyword}, "global"))
    # jp：日本官方频道（channelId，AltPlus 独立运营）→ 拆 jp 区服
    jp_channel = REGION_APPS.get("youtube", {}).get("jp")
    if jp_channel:
        items.extend(_fetch_youtube_videos(api_key, published_after, {"channelId": jp_channel}, "jp"))
    return items


def _fetch_youtube_videos(api_key, published_after, query_params, region):
    """单次 search.list（按 q 关键词或 channelId）+ statistics，标 region + archive_subtype=video。"""
    items = []
    label = query_params.get("q") or query_params.get("channelId") or "?"
    try:
        data = _get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "type": "video",
                "order": "date",
                "publishedAfter": published_after,
                "maxResults": 15,
                "key": api_key,
                **query_params,
            },
        ).json()

        video_ids = [item["id"]["videoId"] for item in data.get("items", []) if item.get("id", {}).get("videoId")]

        # 获取视频统计数据
        stats = {}
        if video_ids:
            stats_data = _get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "part": "statistics",
                    "id": ",".join(video_ids),
                    "key": api_key,
                },
            ).json()
            for v in stats_data.get("items", []):
                s = v.get("statistics", {})
                stats[v["id"]] = int(s.get("viewCount", 0)) + int(s.get("likeCount", 0))

        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if not vid:
                continue
            snippet = item.get("snippet", {})
            engagement = stats.get(vid, 0)
            items.append(_make_item(
                title=snippet.get("title", ""),
                summary=snippet.get("description", ""),
                source="youtube",
                platform_region="global",
                time_str=snippet.get("publishedAt", ""),
                url=f"https://www.youtube.com/watch?v={vid}",
                engagement=engagement,
                is_hot=engagement > 5000,
                author=snippet.get("channelTitle", ""),
                lang="",
                content_type="video",
                media_url=snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                region=region,            # 甲方案：global 社区流 / jp 官方频道
                archive_subtype="video",  # 归档 youtube/<区服>/video
            ))

        logger.info(f'YouTube [{region}] {label}: {len(items)} videos')
    except Exception as e:
        # H3: 异常文本含完整请求 URL（key=<API key>），脱敏后再进公开日志
        logger.warning(f'YouTube [{region}] {label} failed: {news_common.redact_secrets(e)}')

    return items


# NOTE: divergent from aggregator_collectors.fetch_taptap — see audit ARCH-01 (behavior differs, not merged).
def fetch_taptap():
    """从 TapTap 获取忘却前夜社区帖子和评价（Playwright 无头浏览器方案）。

    TapTap 已废弃 webapiv2 端点，改用 taptap_collector 模块通过 headless Chromium
    渲染页面后拦截 API 响应或提取 DOM 来获取数据。
    source 字段：帖子为 "taptap_post"，评价为 "taptap_review"。
    """
    try:
        import taptap_collector as _tc
    except ImportError:
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import taptap_collector as _tc
        except ImportError:
            logger.warning("TapTap: taptap_collector not available (playwright not installed?), skipping")
            return []

    try:
        topic_items, review_items = asyncio.run(_tc.collect(cutoff=CUTOFF))
        items = topic_items + review_items
        logger.info(f"TapTap: {len(topic_items)} posts + {len(review_items)} reviews")
        return items
    except Exception as e:
        logger.warning(f"TapTap failed: {e}")
        return []


# ─── 新增数据源 ──────────────────────────────────────────

def _parse_weibo_time(created_str):
    """Parse Weibo's created_at field into ISO datetime string.

    Weibo mobile API returns times in various formats:
    - "刚刚" (just now)
    - "x分钟前" (x minutes ago)
    - "x小时前" (x hours ago)
    - "昨天 HH:MM" (yesterday HH:MM)
    - "MM-DD" (month-day, current year)
    - "Wed Jan 01 00:00:00 +0800 2025" (full date, rare)
    - "yyyy-MM-DD" (standard date)

    Returns (iso_string, is_approximate) tuple.

    微博特有格式（"昨天 HH:MM"、"Wed Jan 01 00:00:00 +0800 2025"）在此先行解析，
    其余通用相对/绝对格式委托 news_common.parse_relative_time（H4 收敛）。
    """
    s = (created_str or "").strip()
    if s:
        # "昨天 HH:MM" = yesterday HH:MM（精确到分钟，通用函数只到天级）
        # 微博给的是**北京墙钟**（UTC+8）。原实现拿 UTC 的「昨天」+ 直接 replace 时分,
        # 等于把 14:30 当成 UTC 14:30 —— 整条时间戳偏 8 小时，既可能把昨夜的帖挤出
        # 24h 窗口，也会让归档按北京日分桶时落错日期。故按 UTC+8 构造再原样输出。
        m = re.match(r"昨天\s*(\d{1,2}):(\d{2})", s)
        if m:
            cst = timezone(timedelta(hours=8))
            yesterday = datetime.now(cst) - timedelta(days=1)
            dt = yesterday.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return dt.isoformat(), False

        # Full date format: "Wed Jan 01 00:00:00 +0800 2025"
        try:
            dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
            return dt.isoformat(), False
        except ValueError:
            pass

    return news_common.parse_relative_time(s)


def fetch_weibo():
    """从微博搜索忘却前夜相关热帖。支持 WEIBO_COOKIE 环境变量提升成功率。

    翻页（2026-08-16）：原实现每个关键词只取搜索结果第一页（约十余条），一轮采集
    的可见面就是首屏那点内容——高峰日刷得快就整段刷过去了，实测 2026-08-13 单日
    归档为 0，其后三天只有 33/10/9 条。现按 page 参数向后翻至多 WEIBO_MAX_PAGES 页，
    空页或重复页即停。
    """
    cookie = os.environ.get("WEIBO_COOKIE", "")
    items = []
    for keyword in KEYWORDS["zh"]:
        seen_ids: set[str] = set()
        for page in range(1, WEIBO_MAX_PAGES + 1):
            try:
                headers = {"Referer": "https://m.weibo.cn"}
                if cookie:
                    headers["Cookie"] = cookie
                data = _get(
                    "https://m.weibo.cn/api/container/getIndex",
                    params={"containerid": f"100103type=1&q={keyword}",
                            "page_type": "searchall", "page": page},
                    headers=headers,
                ).json()

                cards = [c for c in data.get("data", {}).get("cards", [])
                         if c.get("card_type") == 9]
                if not cards:
                    break  # 到底了

                # 微博在越界翻页时会重复回吐上一页——整页无新 id 即停，
                # 否则会把同一批内容反复计入（且白烧请求）。
                page_ids = {str((c.get("mblog") or {}).get("id", "")) for c in cards}
                if page_ids and page_ids <= seen_ids:
                    break
                seen_ids |= page_ids

                _collect_weibo_cards(cards, items)
                logger.info(f'Weibo "{keyword}" p{page}: {len(cards)} cards')
            except Exception as e:
                logger.warning(f'Weibo "{keyword}" p{page} failed: {e}')
                break

    return items


def _collect_weibo_cards(cards, items):
    """把一页搜索卡片解析成标准 item 追加进 items。"""
    for card in cards:
        mblog = card.get("mblog", {})
        parsed_time, time_approx = _parse_weibo_time(mblog.get("created_at", ""))
        text_clean = re.sub(r"<[^>]+>", "", mblog.get("text", ""))

        item = _make_item(
            title=text_clean[:100],
            summary=text_clean,
            source="weibo",
            platform_region="cn",
            time_str=parsed_time,
            url=f"https://m.weibo.cn/detail/{mblog.get('id', '')}",
            engagement=(mblog.get("reposts_count", 0) + mblog.get("comments_count", 0)
                        + mblog.get("attitudes_count", 0)),
            is_hot=mblog.get("attitudes_count", 0) > 500,
            author=mblog.get("user", {}).get("screen_name", ""),
            lang="zh",
        )
        if time_approx:
            item["time_is_approximate"] = True
        items.append(item)


def fetch_arca_live():
    """从 Arca.live 抓取韩国忘却前夜频道 (forgettingeve)。

    Cloudflare 对裸 `Mozilla/5.0` UA 直接 403，须用完整浏览器头。
    2026-06 实测 DOM：每帖为 `<a class="vrow column" href="/b/.../id?p=1">`，
    含 <time datetime="ISO">、col-view 浏览数、col-rate 推荐数、comment-count。
    （公告行 class 为 "vrow column notice ..."，精确匹配普通行即自动排除。）
    """
    arca_channel = os.environ.get("ARCA_CHANNEL", "forgettingeve")
    items = []
    seen_urls = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://arca.live/",
    }
    import re as _re

    for mode in ("best", ""):  # best=인기, ""=최신
        try:
            params = {"p": 1}
            if mode:
                params["mode"] = mode
            resp = _get(
                f"https://arca.live/b/{arca_channel}",
                params=params,
                headers=headers,
            )
            html = resp.text

            rows = _re.split(r'<a class="vrow column" href="', html)[1:]
            mode_added = 0
            for row in rows:
                m_href = _re.match(r'(/b/[^"?]+)', row)
                m_title = _re.search(r'<span class="title">(.*?)</span>\s*<span class="info">', row, _re.DOTALL)
                if not m_href or not m_title:
                    continue
                title = news_common.strip_html(m_title.group(1)).strip()
                if not title:
                    continue
                url = f"https://arca.live{m_href.group(1)}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                m_time = _re.search(r'<time datetime="([^"]+)"', row)
                time_str = m_time.group(1) if m_time else datetime.now(UTC).isoformat()
                m_view = _re.search(r'class="vcol col-view">\s*([\d,]+)', row)
                m_rate = _re.search(r'class="vcol col-rate">\s*(-?[\d,]+)', row)
                m_cmt = _re.search(r'class="comment-count">\s*\[(\d+)\]', row)
                views = int(m_view.group(1).replace(",", "")) if m_view else 0
                rate = int(m_rate.group(1).replace(",", "")) if m_rate else 0
                comments = int(m_cmt.group(1)) if m_cmt else 0
                m_author = _re.search(r'data-filter="([^"]*)"', row)

                items.append(_make_item(
                    title=title,
                    summary="",
                    source="arca_live",
                    platform_region="kr",
                    time_str=time_str,
                    url=url,
                    engagement=views + rate * 5 + comments * 2,
                    is_hot=(mode == "best") or rate >= 10,
                    author=m_author.group(1) if m_author else "",
                    lang="ko",
                    time_is_approximate=not m_time,
                ))
                mode_added += 1

            logger.info(f'Arca.live "{arca_channel}" mode={mode or "latest"}: +{mode_added} items')
        except Exception as e:
            logger.warning(f'Arca.live "{arca_channel}" mode={mode or "latest"} failed: {e}')

    return items


# NOTE: divergent from aggregator_collectors.fetch_discord_local — see audit ARCH-01:
# this is a live-API fetch; the aggregator stack reads a local Discord archive instead. Not merged.
def fetch_discord():
    """从 Discord Webhook / Bot 获取官方服务器讨论摘要。"""
    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    discord_channels = os.environ.get("DISCORD_CHANNEL_IDS", "").split(",")
    if not discord_token or not discord_channels[0]:
        logger.info("Discord: DISCORD_BOT_TOKEN or DISCORD_CHANNEL_IDS not set, skipping")
        return []

    items = []
    for channel_id in discord_channels:
        channel_id = channel_id.strip()
        if not channel_id:
            continue
        try:
            data = _get(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                params={"limit": 50},
                headers={"Authorization": f"Bot {discord_token}"},
            ).json()

            for msg in data if isinstance(data, list) else []:
                # 只取有一定反应数的消息
                reactions = sum(r.get("count", 0) for r in msg.get("reactions", []))
                if reactions < 3:
                    continue
                created = msg.get("timestamp", "")
                items.append(_make_item(
                    title=msg.get("content", "")[:100],
                    summary=msg.get("content", ""),
                    source="discord",
                    platform_region="global",
                    time_str=created,
                    url=f"https://discord.com/channels/{msg.get('guild_id', '')}/{channel_id}/{msg.get('id', '')}",
                    engagement=reactions,
                    is_hot=reactions > 10,
                    author=msg.get("author", {}).get("username", ""),
                    lang="en",
                ))

            logger.info(f"Discord channel {channel_id}: {len(items)} messages")
        except Exception as e:
            logger.warning(f"Discord channel {channel_id} failed: {e}")

    return items
# App Store 评论覆盖地区（global app 多国走 platform_region 字段；甲方案 region 另标区服 global/jp）。
_APPSTORE_COUNTRIES = [
    # 中文圈
    "cn", "tw", "hk",
    # 英语圈
    "us", "gb", "ca", "au", "nz", "ie", "sg",
    # 日韩
    "jp", "kr",
    # 东南亚
    "my", "ph", "th", "id", "vn",
    # 欧洲（非英）
    "de", "fr", "es", "it", "ru",
    # 拉美
    "br", "mx",
]
_APPSTORE_LANG_MAP = {
    "cn": "zh", "tw": "zh", "hk": "zh",
    "us": "en", "gb": "en", "ca": "en", "au": "en", "nz": "en", "ie": "en", "sg": "en",
    "jp": "ja", "kr": "ko",
    "my": "ms", "ph": "en", "th": "th", "id": "id", "vn": "vi",
    "de": "de", "fr": "fr", "es": "es", "it": "it", "ru": "ru",
    "br": "pt", "mx": "es",
}


def fetch_appstore_reviews():
    """从 App Store 获取近期评论——global 多国 + jp 独立 app（甲方案双 appid，区服 global/jp）。"""
    # 兼容旧 APPSTORE_APP_ID kill-switch：显式空串 = 整体禁用，回落空
    if os.environ.get("APPSTORE_APP_ID", "x") == "":
        return []
    apps = dict(REGION_APPS.get("appstore", {"global": "6447354150"}))
    ov = os.environ.get("APPSTORE_APP_ID")  # 非空则覆盖 global appid（测试/灰度 hook）
    if ov:
        apps["global"] = ov
    items = []
    for region, appstore_id in apps.items():
        if not appstore_id:
            continue
        # global app 覆盖全 24 区；jp 独立 app（AltPlus）仅日本商店有评价
        countries = _APPSTORE_COUNTRIES if region == "global" else ["jp"]
        items.extend(_fetch_appstore_reviews_one(str(appstore_id), region, countries))
    return items


def _pacific_wallclock_iso(raw):
    """把苹果 RSS `updated` 的墙钟按**真实**太平洋时区规则重标，返回 ISO 8601。

    苹果这个字段的偏移标签全年恒为 `-07:00`（2026-09-17 实测：归档层 713 条无一例外，
    连 2023-12 / 2024-01 这些 PST=-08:00 的冬季条目也照标 -07:00）。墙钟本身是太平洋
    当地时间，所以冬令时期间整条时间戳偏 1 小时——卡在日界附近的评论会因此落进错误的
    归档桶。做法：剥掉那个固定标签，把墙钟交给 `America/Los_Angeles` 按当日真实规则
    （PST / PDT）重新定位。

    tzdata 不可用时**原值返回**，不猜：宁可维持已知偏差，也不引入一个新的猜测。
    DST 回拨那一小时的歧义按 fold=0 取前一次，比现在全年差 1 小时小得多。
    """
    if not raw:
        return raw
    try:
        from zoneinfo import ZoneInfo
        wall = datetime.fromisoformat(raw).replace(tzinfo=None)
        return wall.replace(tzinfo=ZoneInfo("America/Los_Angeles")).isoformat()
    except Exception:  # noqa: BLE001  tzdata 缺失 / 格式意外，一律回落原值
        return raw


def _fetch_appstore_reviews_one(appstore_id, region, countries):
    """抓取单个 (appstore_id, region) 的多国评论。"""
    items = []
    for country in countries:
        try:
            data = _get(
                f"https://itunes.apple.com/{country}/rss/customerreviews/id={appstore_id}/sortBy=mostRecent/json",
            ).json()
            entries = data.get("feed", {}).get("entry", [])
            # Apple 的 JSON-ified RSS 在「只有一条 entry」时把数组塌成对象。原写法直接
            # for 遍历：拿到 dict 就变成遍历它的**键字符串**，下一行 str.get 抛
            # AttributeError，被本函数的 except 吞成一条 debug ——该国当轮 0 条评论,
            # 与「今天真没人评价」完全无法区分。故先归一为列表，再逐条守 dict。
            if isinstance(entries, dict):
                entries = [entries]
            review_url = f"https://apps.apple.com/{country}/app/id{appstore_id}?see-all=reviews"
            for entry in entries:
                # feed 首条是 app 自身元信息（无 im:rating），不是评论，放行会造出
                # 一条标题为游戏名、评分 0 的假评论。
                if not isinstance(entry, dict) or "im:rating" not in entry:
                    continue
                rating = int(entry.get("im:rating", {}).get("label", "0"))
                # RSS entry id 是评论唯一标识。不追加锚点时同 country 数十条评论共用 review_url，
                # 致 dedup_key（URL 优先）碰撞、每 country 仅存活 1 条。fragment 使每条 key 唯一。
                entry_id = entry.get("id", {}).get("label", "")
                items.append(_make_item(
                    title=entry.get("title", {}).get("label", ""),
                    summary=entry.get("content", {}).get("label", ""),
                    source="appstore",
                    platform_region=country,
                    time_str=_pacific_wallclock_iso(entry.get("updated", {}).get("label", "")),
                    url=f"{review_url}#as-{entry_id}" if entry_id else review_url,
                    engagement=rating,
                    is_hot=False,
                    author=entry.get("author", {}).get("name", {}).get("label", ""),
                    lang=_APPSTORE_LANG_MAP.get(country, ""),
                    region=region,  # 甲方案：global（多国）/ jp（AltPlus 独立 app）→ appstore/<区服>/
                ))
            logger.info(f"App Store ({country}): {len(entries)} reviews")
        except Exception as e:
            logger.debug(f"App Store ({country}) failed: {e}")

    return items
def fetch_pixiv():
    """从 Pixiv 搜索忘却前夜同人创作。"""
    items = []
    for keyword in ["忘却前夜", "Morimens", "モリメンス"]:
        try:
            data = _get(
                "https://www.pixiv.net/ajax/search/artworks/" + keyword,
                params={"order": "date_d", "mode": "all", "p": 1, "s_mode": "s_tag"},
                headers={"Referer": "https://www.pixiv.net"},
            ).json()

            body = data.get("body", {})
            if not isinstance(body, dict):
                body = {}
            illust_manga = body.get("illustManga", {})
            if not isinstance(illust_manga, dict):
                illust_manga = {}
            illust_data = illust_manga.get("data", [])
            if not isinstance(illust_data, list):
                illust_data = []
            for illust in illust_data[:20]:
                if not isinstance(illust, dict):
                    continue
                illust_id = illust.get("id", "")
                bookmark = illust.get("bookmarkCount", 0)
                like = illust.get("likeCount", 0)
                # search_artworks 返回 tags 为 ["tag1", "tag2"]（字符串列表），
                # 而单个 illust ajax 返回 tags 为 [{"tag": "..."}, ...]，两者都要兼容。
                raw_tags = illust.get("tags", []) or []
                if isinstance(raw_tags, list):
                    tag_list = []
                    for t in raw_tags[:5]:
                        if isinstance(t, dict):
                            tag_list.append(t.get("tag", ""))
                        elif isinstance(t, str):
                            tag_list.append(t)
                else:
                    tag_list = []
                items.append(_make_item(
                    title=illust.get("title", ""),
                    summary=illust.get("description", "") if illust.get("description") else "",
                    source="pixiv",
                    platform_region="global",
                    time_str=illust.get("createDate") or datetime.now(UTC).isoformat(),
                    url=f"https://www.pixiv.net/artworks/{illust_id}",
                    engagement=bookmark + like,
                    is_hot=bookmark > 500,
                    author=illust.get("userName", ""),
                    tags=tag_list,
                    lang="",
                    content_type="image",
                    media_url=illust.get("url", ""),
                    time_is_approximate=not illust.get("createDate"),
                ))

            logger.info(f'Pixiv "{keyword}": {len(items)} artworks')
        except Exception as e:
            logger.warning(f'Pixiv "{keyword}" failed: {e}')

    return items
# ─── 第三波新增数据源 ─────────────────────────────────────


# (lang_code, country_code, region_label) — Google Play 同时按 lang+country 隔离评论
_GP_LOCALES = [
    # 中文圈
    ("zh_CN", "cn", "cn"), ("zh_TW", "tw", "tw"), ("zh_HK", "hk", "hk"),
    # 英语圈
    ("en", "us", "us"), ("en", "gb", "gb"), ("en", "ca", "ca"),
    ("en", "au", "au"), ("en", "sg", "sg"), ("en", "ph", "ph"),
    # 日韩
    ("ja", "jp", "jp"), ("ko", "kr", "kr"),
    # 东南亚
    ("th", "th", "th"), ("id", "id", "id"), ("vi", "vn", "vn"),
    ("ms", "my", "my"),
    # 欧洲（非英）
    ("de", "de", "de"), ("fr", "fr", "fr"), ("es", "es", "es"),
    ("it", "it", "it"), ("ru", "ru", "ru"),
    # 拉美
    ("pt", "br", "br"), ("es", "mx", "mx"),
]


def fetch_google_play():
    """从 Google Play 获取评论——global 多国 + jp 独立包（甲方案双包，区服 global/jp）。"""
    try:
        from google_play_scraper import reviews as gp_reviews, Sort as GPSort  # noqa: F401
    except ImportError:
        logger.warning("Google Play: google-play-scraper not installed, skipping")
        return []
    packages = dict(REGION_APPS.get("google_play", {"global": "com.qookkagames.z1.gp.hk"}))
    ov = os.environ.get("GOOGLE_PLAY_PACKAGE")  # 非空覆盖 global 包名（测试/灰度 hook）
    if ov:
        packages["global"] = ov
    items = []
    for arch_region, gp_package in packages.items():
        if not gp_package:
            continue
        # global 包覆盖全 locale；jp 独立包（AltPlus）仅日本 locale
        locales = _GP_LOCALES if arch_region == "global" else [("ja", "jp", "jp")]
        items.extend(_fetch_google_play_one(gp_package, arch_region, locales))
    return items


def _google_play_at_iso(at):
    """把 google_play_scraper 的 `at` 折成带时区的 ISO 8601（UTC）。

    该库返回的是 **naive** datetime（内部 `datetime.fromtimestamp(...)`，取运行机器
    **本地时区**的墙钟）。直接 `isoformat()` 落档就是一个不带时区的字符串，而
    `archive_layout.archive_date_str` 对 naive 一律按 UTC 解释——只要采集始终跑在 UTC
    的 CI runner 上就碰巧对上，换台非 UTC 机器做回填，日期整体歪掉且不报任何错
    （2026-09-17 实测：归档层 1,509 条 google_play 时间戳全部无时区标注）。

    `astimezone()` 对 naive 值按运行机器本地时区定位，再折 UTC：隐性假设变显式换算。
    """
    if at is None:
        return None
    return (at if at.tzinfo else at.astimezone()).astimezone(UTC).isoformat()


def _fetch_google_play_one(gp_package, arch_region, locales):
    """抓取单个 (gp_package, arch_region) 的多 locale 评论。"""
    from google_play_scraper import reviews as gp_reviews, Sort as GPSort
    items = []
    for lang_code, country, region in locales:
        try:
            result, _ = gp_reviews(
                gp_package,
                lang=lang_code,
                country=country,
                count=50,
                sort=GPSort.NEWEST,
            )
            for review in result:
                rating = review.get("score", 0)
                text = review.get("content", "")
                sentiment = '好评' if rating >= 4 else ('中评' if rating == 3 else '差评')
                items.append(_make_item(
                    title=f"[Google Play {sentiment}] ★{rating} {text[:40]}",
                    summary=text,
                    source="google_play",
                    platform_region=region,
                    region=arch_region,  # 甲方案：global（多 locale）/ jp（AltPlus 独立包）→ google_play/<区服>/
                    time_str=_google_play_at_iso(review.get("at")) or datetime.now(UTC).isoformat(),
                    # URL 追加 reviewId 锚点：评论页 URL 仅含 id+hl，同语言数十条评论会共用同一
                    # URL，致 dedup_key（URL 优先）碰撞、每语言仅存活 1 条（丢失 ~98% 评论）。
                    # fragment 使每条 key 唯一，不影响链接访问，跨轮次去重仍按恒定 reviewId 生效。
                    url=f"https://play.google.com/store/apps/details?id={gp_package}&hl={lang_code}#gp-{review.get('reviewId', '')}",
                    engagement=review.get("thumbsUpCount", 0),
                    is_hot=False,
                    author=review.get("userName", ""),
                    lang=lang_code.split("_")[0],
                    time_is_approximate=not review.get("at"),
                ))

            logger.info(f"Google Play ({lang_code}/{country}): {len(result)} reviews")
        except Exception as e:
            logger.debug(f"Google Play ({lang_code}/{country}) failed: {e}")

    return items
# 忘卻前夜 Morimens 哈啦板板編（2026-07-10 实测 https://forum.gamer.com.tw/A.php?bsn=78829）
BAHAMUT_DEFAULT_BSN = "78829"

# 巴哈姆特是台湾站点，列表页 `b-list__time__edittime` 对**当日**帖只给裸墙钟
# "HH:MM"（台北时 UTC+8），更早的帖才给 "MM/DD"。
_BAHAMUT_TZ = timezone(timedelta(hours=8))


def _parse_bahamut_time(text):
    """解析巴哈姆特 edittime，返回 (ISO, is_approximate)。

    裸 "HH:MM" 必须按台北时（UTC+8）构造：共享的 parse_relative_time 把它当 UTC
    墙钟，于是台北 20:00 的帖被记成 20:00 UTC —— 彼时 UTC 才 12:00，判定为「未来」
    再整体回退一天，时间戳偏早 32 小时（台北 00:00–07:59 的帖不触发回退，仍偏早
    16 小时）。后果是该帖落进前一天甚至前两天的归档桶（归档按北京日分桶）。
    其余格式（"MM/DD" 日期级 / 相对时间 / ISO）时区无涉，仍走共享真源。
    """
    s = (text or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        now_tpe = datetime.now(_BAHAMUT_TZ)
        try:
            dt = now_tpe.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                 second=0, microsecond=0)
        except ValueError:
            return news_common.parse_relative_time(s)
        if dt > now_tpe:  # 显示时刻晚于当前台北时间 = 昨天的帖
            dt -= timedelta(days=1)
        return dt.isoformat(), False
    return news_common.parse_relative_time(s)


def fetch_bahamut():
    """巴哈姆特忘却前夜专板帖列表（B.php 列表页 HTML）。台湾最大游戏社区。

    2026-07-10 修复（三年零产出真因，CI 日志 + 本地双实测）：
    - 旧方式1 `B.php?ajax=1` JSON 接口已退役——现返回整页 HTML；
    - 旧方式2 `search.php` 全站搜索需板編（bsn=0 报「沒有傳入板編」系統訊息），
      新版全站搜索 search.gamer.com.tw 为纯 JS 渲染，服务端 HTML 零结果。
    改为解析专板列表页的 `b-list__row` 行结构（默认板編 78829，可用
    BAHAMUT_BSN 覆盖）。专板即游戏本板，无需关键词过滤；置顶帖一并采集。
    """
    baha_bsn = os.environ.get("BAHAMUT_BSN") or BAHAMUT_DEFAULT_BSN
    items = []
    import re as _re
    try:
        resp = _get(
            "https://forum.gamer.com.tw/B.php",
            params={"bsn": baha_bsn},
            headers={
                "Referer": "https://forum.gamer.com.tw",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        rows = _re.split(r'<tr class="b-list__row', resp.text)[1:]
        for row in rows:
            # 标题两形态：置顶行 <a class="b-list__main__title">…</a>；
            # 普通行外层大锚点持 href、标题在 <p class="b-list__main__title">…</p>
            m_title = _re.search(
                r'b-list__main__title[^>]*>(.*?)</(?:a|p)>', row, _re.DOTALL)
            m_href = _re.search(r'href="(C\.php\?bsn=\d+&(?:amp;)?snA=\d+[^"]*)"', row)
            if not m_title or not m_href:
                continue
            title = news_common.strip_html(m_title.group(1)).strip()
            if not title:
                continue
            url = "https://forum.gamer.com.tw/" + m_href.group(1).replace("&amp;", "&")
            m_gp = _re.search(r'b-list__summary__gp[^>]*>\s*([\d,]+)', row)
            m_int = _re.search(r'title="互動：([\d,]+)"', row)
            m_author = _re.search(r'b-list__count__user">\s*<a[^>]*>([^<]+)</a>', row, _re.DOTALL)
            m_time = _re.search(r'b-list__time__edittime">\s*<a[^>]*>([^<]+)</a>', row, _re.DOTALL)
            gp = int(m_gp.group(1).replace(",", "")) if m_gp else 0
            interact = int(m_int.group(1).replace(",", "")) if m_int else 0
            baha_time, baha_approx = _parse_bahamut_time(
                m_time.group(1).strip() if m_time else None)
            items.append(_make_item(
                title=title,
                summary="",
                source="bahamut",
                platform_region="tw",
                time_str=baha_time,
                url=url,
                engagement=gp + interact,
                is_hot=gp > 50,
                author=m_author.group(1).strip() if m_author else "",
                lang="zh",
                time_is_approximate=baha_approx,
            ))
        logger.info(f"Bahamut bsn={baha_bsn}: {len(items)} threads")
    except Exception as e:
        logger.warning(f"Bahamut bsn={baha_bsn} failed: {e}")

    return items


def fetch_weixin():
    """通过搜狗微信搜索抓取忘却前夜相关公众号文章。

    搜狗是唯一公开索引微信公众号文章的搜索引擎。

    已知限制（技术原因，非 bug）：
    - engagement 始终为 0：搜狗搜索结果不包含阅读量/点赞等互动指标，
      微信官方 API 需要企业号 + 腾讯审批，暂不可行。
    - summary 为空：搜狗结果页仅提供标题和链接，不含正文摘要。
    - 文章 URL 为搜狗中转链接，非微信直链。
    """
    items = []
    for keyword in KEYWORDS["zh"]:
        try:
            resp = _get_cf(
                "https://weixin.sogou.com/weixin",
                params={"type": 2, "query": keyword, "ie": "utf8", "s_from": "input", "page": 1},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://weixin.sogou.com/",
                },
            )
            html = resp.text
            import re as _re

            # Sogou WeChat embeds Unix timestamps via timeConvert('EPOCH')
            # or data-t="EPOCH" attributes near each result. Collect them
            # so we can correlate with result order.
            sogou_timestamps = _re.findall(
                r"(?:timeConvert\(['\"](\d{10})['\"]|data-t=['\"](\d{10})['\"]"
                r"|lastModified['\"]?\s*[:=]\s*['\"]?(\d{10}))",
                html,
            )
            ts_list = [int(t1 or t2 or t3) for t1, t2, t3 in sogou_timestamps]

            # Parse search results — capture snippet text between <p> after title
            result_idx = 0
            for match in _re.finditer(
                r'<h3>.*?<a[^>]*href="([^"]+)"[^>]*>(.+?)</a>.*?'
                r'(?:class="txt-info"[^>]*>(.+?)</p>)?.*?'
                r'class="s-p"[^>]*>([^<]*)',
                html, _re.DOTALL
            ):
                url, title_html, snippet_html, meta = match.groups()
                # Clean HTML tags from title
                title = _re.sub(r'<[^>]+>', '', title_html).strip()
                if not title:
                    continue

                # Extract summary from snippet
                summary = ""
                if snippet_html:
                    summary = _re.sub(r'<[^>]+>', '', snippet_html).strip()[:300]

                # Extract author from meta
                author_match = _re.search(r'微信公众号\s*[:：]\s*([^\s<]+)', meta)
                author = author_match.group(1) if author_match else ""

                # Extract publish time from Sogou timestamps or meta text
                time_str = ""
                time_approx = True
                if result_idx < len(ts_list):
                    try:
                        dt = datetime.fromtimestamp(ts_list[result_idx], tz=UTC)
                        time_str = dt.isoformat()
                        time_approx = False
                    except (ValueError, OSError):
                        pass

                # Fallback: look for date patterns in meta text (e.g. "2025-03-15", "2025年3月15日")
                if not time_str and meta:
                    date_m = _re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', meta)
                    if date_m:
                        try:
                            dt = datetime(int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3)), tzinfo=UTC)
                            time_str = dt.isoformat()
                            time_approx = False
                        except ValueError:
                            pass

                if not time_str:
                    time_str = datetime.now(UTC).isoformat()
                    time_approx = True

                # href 取自 HTML 属性，实体未解码时链接会带字面量 &amp;（归档里
                # 的微信链接因此全是 `?url=…&amp;token=…` 这种点不开的坏链）。
                url = _html.unescape(url)

                item = _make_item(
                    title=f"[微信] {title}",
                    summary=summary,
                    source="weixin",
                    platform_region="cn",
                    time_str=time_str,
                    url=url,
                    engagement=0,
                    is_hot=False,
                    author=author,
                    lang="zh",
                )
                if time_approx:
                    item["time_is_approximate"] = True
                items.append(item)
                result_idx += 1

            logger.info(f'搜狗微信 "{keyword}": {len(items)} articles')
        except Exception as e:
            logger.warning(f'搜狗微信 "{keyword}" failed: {e}')

    return items


# ─── 日本語プラットフォーム ────────────────────────────────

def fetch_note_com():
    """从 note.com 拉忘却前夜/モリメンス hashtag RSS（各返回最新 25 条）。

    2026-07-10 修复（三年零产出真因）：旧路径 /api/v3/searches 已对非浏览器
    请求一律 403 "Access denied"（完整浏览器头与 cloudscraper 均被拒，本地与
    CI 双实测）；hashtag RSS（/hashtag/<tag>/rss）为普通网页路由、无此防护。
    RSS 不含点赞数 → engagement 恒 0、is_hot 恒 False（同 weixin 的已知限制）。
    """
    from urllib.parse import quote as _quote
    from email.utils import parsedate_to_datetime as _rfc822
    import re as _re
    items = []
    seen_urls = set()
    for tag in KEYWORDS["ja"]:
        try:
            resp = _get(
                f"https://note.com/hashtag/{_quote(tag)}/rss",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept-Language": "ja-JP,ja;q=0.9",
                },
            )
            added = 0
            for m in _re.finditer(r'<item>(.*?)</item>', resp.text, _re.DOTALL):
                block = m.group(1)

                def _field(name, _block=block):
                    mm = _re.search(rf'<{name}>(.*?)</{name}>', _block, _re.DOTALL)
                    if not mm:
                        return ""
                    val = mm.group(1).strip()
                    return _re.sub(r'^<!\[CDATA\[|\]\]>$', '', val).strip()

                url = _field("link") or _field("guid")
                title = news_common.strip_html(_field("title")).strip()
                if not url or not title or url in seen_urls:
                    continue
                seen_urls.add(url)
                pub = _field("pubDate")
                try:
                    time_str = _rfc822(pub).isoformat() if pub else ""
                except (TypeError, ValueError):
                    time_str = ""
                items.append(_make_item(
                    title=title,
                    summary=news_common.strip_html(_field("description"))[:200],
                    source="note_com",
                    platform_region="jp",
                    time_str=time_str or datetime.now(UTC).isoformat(),
                    url=url,
                    engagement=0,
                    is_hot=False,
                    author=_field("note:creatorName"),
                    lang="ja",
                    time_is_approximate=not time_str,
                ))
                added += 1
            logger.info(f'Note.com #{tag} RSS: +{added} notes')
        except Exception as e:
            # 标签不存在（尚无文章）时 RSS 返回 404，属正常态而非故障
            if '404' in str(e):
                logger.info(f'Note.com #{tag}: no such hashtag yet (404)')
            else:
                logger.warning(f'Note.com #{tag} RSS failed: {e}')

    return items


# ─── 韓国追加プラットフォーム ──────────────────────────────

def fetch_ruliweb():
    """从 Ruliweb 搜索韩国忘却前夜讨论。

    搜索结果页结构：`<div id="board_search">` 段下含若干 `<li class="search_result_item">`，
    每条含 `<a class="title text_over">`（标题+链接）、`<span class="time">YYYY.MM.DD</span>`
    （发布日期）、`<span class="desc">`（摘要）、`<a class="name">[板块名]</a>`。
    """
    items = []
    seen_urls: set[str] = set()
    for keyword in KEYWORDS["ko"]:
        try:
            resp = _get_cf(
                "https://bbs.ruliweb.com/search",
                params={"q": keyword, "page": 1},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            html = resp.text
            import re as _re

            # Limit parsing to the actual post-search section to skip game-category
            # aggregation blocks at the top of the page.
            board_match = _re.search(
                r'<div id="board_search"[^>]*>(.+?)(?=<div id=|</body>|$)',
                html, _re.DOTALL,
            )
            if not board_match:
                logger.info(f'Ruliweb "{keyword}": board_search section missing, skipping')
                continue
            board_html = board_match.group(1)

            count = 0
            for item_match in _re.finditer(
                r'<li class="search_result_item">(.+?)</li>',
                board_html, _re.DOTALL,
            ):
                block = item_match.group(1)

                title_m = _re.search(
                    r'<a class="title[^"]*" href="([^"]+)"[^>]*>([^<]+)</a>',
                    block,
                )
                if not title_m:
                    continue
                url, title = title_m.group(1).strip(), title_m.group(2).strip()
                if not title or url in seen_urls:
                    continue

                time_m = _re.search(
                    r'<span class="time">(\d{4}\.\d{2}\.\d{2}(?:\s+\d{2}:\d{2})?)</span>',
                    block,
                )
                time_str = ""
                time_approx = True
                if time_m:
                    raw = time_m.group(1).strip()
                    # Normalize to ISO 8601 (UTC, since publish times shown are KST date-only —
                    # interpret as KST 00:00 then convert to UTC for consistency).
                    try:
                        if " " in raw:
                            dt = datetime.strptime(raw, "%Y.%m.%d %H:%M")
                        else:
                            # 日期级（只有 YYYY.MM.DD）：取**当地正午**作代表时刻。
                            # 原写法取 KST 00:00——零点正站在时区分界线上，折成北京
                            # 日期就掉进前一天的桶，整批帖子日期系统性早一天（2026-09-17
                            # 实测 301/448 条，67.2%；2026-07-02 记的「ruliweb 沉默 7 天、
                            # 帖子内容日期偏旧致归档桶不新，非故障」真因即此）。正午离
                            # 两侧日界各 12 小时，任何常见时区折算都还落在当天。
                            dt = datetime.strptime(raw, "%Y.%m.%d").replace(hour=12)
                        # KST = UTC+9
                        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
                        time_str = dt.astimezone(UTC).isoformat()
                        time_approx = False
                    except ValueError:
                        pass

                if not time_str:
                    time_str = datetime.now(UTC).isoformat()
                    time_approx = True

                desc_m = _re.search(r'<span class="desc">\s*(.+?)\s*</span>', block, _re.DOTALL)
                summary = _re.sub(r'\s+', ' ', desc_m.group(1)).strip()[:300] if desc_m else ""

                if not url.startswith("http"):
                    url = f"https://bbs.ruliweb.com{url}"
                seen_urls.add(url)

                items.append(_make_item(
                    title=title,
                    summary=summary,
                    source="ruliweb",
                    platform_region="kr",
                    time_str=time_str,
                    url=url,
                    engagement=0,
                    is_hot=False,
                    author="",
                    lang="ko",
                    time_is_approximate=time_approx,
                ))
                count += 1

            logger.info(f'Ruliweb "{keyword}": {count} posts')
        except Exception as e:
            logger.warning(f'Ruliweb "{keyword}" failed: {e}')

    return items


# ─── Русские платформы ─────────────────────────────────────
def fetch_stopgame():
    """从 StopGame.ru 获取忘却前夜评测和评分。"""
    items = []
    try:
        resp = _get(
            "https://stopgame.ru/game/morimens",
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
        )
        html = resp.text
        import re as _re

        # Extract game rating
        rating_match = _re.search(r'class="[^"]*rating[^"]*"[^>]*>(\d+\.?\d*)', html)
        review_count_match = _re.search(r'(\d+)\s*(?:отзыв|оцен)', html)

        # Try to extract a description/summary from the page
        desc_match = _re.search(
            r'class="[^"]*(?:game-?desc|description|about)[^"]*"[^>]*>\s*(?:<[^>]*>)*\s*([^<]{10,500})',
            html, _re.DOTALL | _re.IGNORECASE
        )
        page_summary = desc_match.group(1).strip()[:300] if desc_match else ""
        # Fallback: try meta description
        if not page_summary:
            meta_match = _re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)', html, _re.IGNORECASE)
            if meta_match:
                page_summary = meta_match.group(1).strip()[:300]

        # Try to extract a page-level date from <time> elements or date patterns
        page_time_str = ""
        page_time_approx = True
        time_tag = _re.search(r'<time[^>]*datetime=["\']([^"\']+)["\']', html)
        if time_tag:
            page_time_str = time_tag.group(1)
            page_time_approx = False
        else:
            # Look for date patterns like "DD.MM.YYYY" (Russian format) or "YYYY-MM-DD"
            date_ru = _re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', html)
            date_iso = _re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', html)
            if date_ru:
                try:
                    dt = datetime(int(date_ru.group(3)), int(date_ru.group(2)), int(date_ru.group(1)), tzinfo=UTC)
                    page_time_str = dt.isoformat()
                    page_time_approx = False
                except ValueError:
                    pass
            elif date_iso:
                try:
                    dt = datetime(int(date_iso.group(1)), int(date_iso.group(2)), int(date_iso.group(3)), tzinfo=UTC)
                    page_time_str = dt.isoformat()
                    page_time_approx = False
                except ValueError:
                    pass

        if not page_time_str:
            page_time_str = datetime.now(UTC).isoformat()
            page_time_approx = True

        if rating_match:
            rating = float(rating_match.group(1))
            count = int(review_count_match.group(1)) if review_count_match else 0
            item = _make_item(
                title=f"[StopGame] Morimens — {rating}/10 ({count} оценок)",
                summary=page_summary or f"Рейтинг игры Morimens на StopGame: {rating}/10 на основе {count} оценок",
                source="stopgame",
                platform_region="ru",
                time_str=page_time_str,
                url="https://stopgame.ru/game/morimens",
                engagement=count,
                is_hot=count > 50,
                author="StopGame.ru",
                lang="ru",
            )
            if page_time_approx:
                item["time_is_approximate"] = True
            items.append(item)

        # Extract user reviews
        # Collect per-review dates from nearby <time> or date elements
        review_dates = _re.findall(
            r'class="[^"]*review[^"]*"[^>]*>.*?'
            r'(?:<time[^>]*datetime=["\']([^"\']+)["\']|(\d{1,2}\.\d{1,2}\.\d{4}))',
            html, _re.DOTALL
        )

        # enumerate 取代手写计数器（循环体内无 continue，语义等价）
        for review_idx, match in enumerate(_re.finditer(
            r'class="[^"]*review-text[^"]*"[^>]*>([^<]{10,300})',
            html, _re.DOTALL
        )):
            text = match.group(1).strip()
            review_time = ""
            review_approx = True
            if review_idx < len(review_dates):
                rd_iso, rd_ru = review_dates[review_idx]
                if rd_iso:
                    review_time = rd_iso
                    review_approx = False
                elif rd_ru:
                    parts = rd_ru.split(".")
                    try:
                        dt = datetime(int(parts[2]), int(parts[1]), int(parts[0]), tzinfo=UTC)
                        review_time = dt.isoformat()
                        review_approx = False
                    except (ValueError, IndexError):
                        pass
            if not review_time:
                review_time = page_time_str
                review_approx = page_time_approx

            # URL 追加内容哈希锚点：所有评测原本共用同一个游戏页 URL，而下游
            # dedup_key 以 URL 优先 —— 整批评测会塌成 1 条（且被评分条挤掉，因为
            # 它 engagement 更高），玩家评测实际上一条都存不下来。哈希取自正文,
            # 跨轮次恒定，故不会把同一条评测反复当新条目采。
            review_anchor = hashlib.md5(text.encode("utf-8"),
                                        usedforsecurity=False).hexdigest()[:10]
            item = _make_item(
                title=f"[StopGame] {text[:60]}",
                summary=text,
                source="stopgame",
                platform_region="ru",
                time_str=review_time,
                url=f"https://stopgame.ru/game/morimens#sg-{review_anchor}",
                engagement=0,
                is_hot=False,
                author="",
                lang="ru",
            )
            if review_approx:
                item["time_is_approximate"] = True
            items.append(item)

        logger.info(f"StopGame: {len(items)} items")
    except Exception as e:
        logger.warning(f"StopGame failed: {e}")

    return items


# ─── 收入/数据平台 ─────────────────────────────────────────


# ---------------------------------------------------------------------------
# Steam（2026-08-22 从退役的 AC 栈迁入）
# ---------------------------------------------------------------------------
# 守密人 2026-08-22 裁定「采集 → 直接入湖」：新闻流编排（aggregator.py）与其展示件
# 退役，但 steam 三源（steam 官方新闻 / steam_review 评价 / steam_discussion 讨论区）
# 的采集实现**只存在于 AC 栈**——GC 栈无等价物。整段实现原样迁入本模块，改动仅限
# 依赖改名（strip_html_tags → 本模块的 _strip_html_tags 委托），采集行为逐字节不变。
# 覆盖面因此零缩窄：三源继续按日归档进 Record/Community/steam{,_review,_discussion}/。
MAX_ITEMS_PER_FETCHER = news_common.env_int('MAX_ITEMS_PER_FETCHER', 500)

def fetch_steam_reviews():
    """Fetch Steam reviews across all configured regions（甲方案：双 appid global/jp，归档子类 review）。"""
    items = []
    for region, app_id in REGION_APPS.get('steam', {'global': '3052450'}).items():
        items.extend(_fetch_steam_reviews_one(str(app_id), region))
    return items


def _fetch_steam_reviews_one(app_id, region):
    """Fetch recent Steam reviews for one (app_id, region).

    使用 cursor=* 分页一直翻到时间窗口外为止。Steam 按 recent 排序，
    一旦看到早于 cutoff 的 review 就可以停。
    """
    import subprocess as _sp
    from urllib.parse import quote
    cutoff = datetime.now(UTC) - timedelta(hours=HOURS_LOOKBACK)
    items = []
    cursor = '*'
    page = 0
    stopped_by_cutoff = False

    try:
        while len(items) < MAX_ITEMS_PER_FETCHER:
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
                logger.warning(f'Steam curl failed on page {page}: {result.stderr[:200]}')
                break
            if not result.stdout.strip():
                logger.warning(f'Steam curl empty body on page {page}')
                break
            data = json.loads(result.stdout)
            reviews = data.get('reviews', []) or []
            if not reviews:
                break

            for review in reviews:
                ts = review.get('timestamp_created', 0)
                created = datetime.fromtimestamp(ts, tz=UTC)
                if created < cutoff:
                    stopped_by_cutoff = True
                    break

                language = review.get('language', 'unknown')
                voted_up = review.get('voted_up', False)
                sentiment = '正面' if voted_up else '负面'
                review_text = review.get('review', '')
                summary_text = review_text[:50].strip()
                title = f'[{sentiment}] {summary_text}...' if len(review_text) > 50 else f'[{sentiment}] {summary_text}'

                author_info = review.get('author', {})
                steamid = author_info.get('steamid', '')
                review_url = f'https://steamcommunity.com/profiles/{steamid}/recommended/{app_id}'
                votes_up = review.get('votes_up', 0)

                items.append({
                    'title': title,
                    'summary': review_text,
                    'source': 'steam_review',
                    'region': region,             # 甲方案：global/jp 区服
                    'archive_subtype': 'review',  # 归档 steam/<区服>/review
                    'time': created.isoformat(),
                    'url': review_url,
                    'engagement': votes_up,
                    'is_hot': votes_up > 10,
                    'author': steamid,
                    'tags': [language],
                    'language': language,
                    'metadata': {
                        'voted_up': voted_up,
                        'playtime_forever': author_info.get('playtime_forever', 0),
                        'votes_up': votes_up,
                        'timestamp_created': ts,
                    },
                })

            if stopped_by_cutoff:
                break
            next_cursor = data.get('cursor')
            # cursor 不变或缺失 → 已到末尾
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(0.5)

        if len(items) == 0:
            logger.warning(f'Steam Reviews: 0 reviews found in last {HOURS_LOOKBACK}h (data source not blocked)')
        else:
            logger.info(f'Steam Reviews: fetched {len(items)} reviews in last {HOURS_LOOKBACK}h across {page} page(s)')
    except Exception as e:
        logger.warning(f'Steam Reviews failed: {e}')

    return items


def fetch_steam_news():
    """Fetch Steam official news across all configured regions（甲方案：双 appid，归档子类 news）。"""
    items = []
    for region, app_id in REGION_APPS.get('steam', {'global': '3052450'}).items():
        items.extend(_fetch_steam_news_one(str(app_id), region))
    return items


def _fetch_steam_news_one(app_id, region):
    """Fetch official Steam news/announcements for one (app_id, region).

    官方公告本身频率较低，通用 HOURS_LOOKBACK（24h）会经常过滤掉全部内容。
    使用更宽的 OFFICIAL_HOURS_LOOKBACK（默认 30 天）以保证日报至少能看到近期官方动态。
    """
    # Steam News 单次 API 调用即可拿足 30 天窗口；count=100 保证不截断。
    url = f'https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={app_id}&count=100&maxlength=500'
    official_hours = news_common.env_int('OFFICIAL_HOURS_LOOKBACK', max(HOURS_LOOKBACK, 30 * 24))
    cutoff = datetime.now(UTC) - timedelta(hours=official_hours)
    items = []

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        news_items = resp.json().get('appnews', {}).get('newsitems', [])

        for n in news_items:
            ts = n.get('date', 0)
            created = datetime.fromtimestamp(ts, tz=UTC) if ts else None
            if not created or created < cutoff:
                continue

            feed_type = n.get('feed_type', 0)
            feed_label = {0: '公告', 1: '新闻'}.get(feed_type, '资讯')

            items.append({
                'title': f'[Steam{feed_label}] {_strip_html_tags(n.get("title", ""))}',
                'summary': _strip_html_tags(n.get('contents', '')),
                'source': 'official',
                'region': region,            # 甲方案：global/jp 区服
                'archive_subtype': 'news',   # 归档 steam/<区服>/news（official 折叠到 steam）
                'time': created.isoformat(),
                'url': n.get('url', ''),
                'engagement': 0,
                'is_hot': True,  # Official announcements are always marked hot
                'author': n.get('author', 'Steam'),
                'tags': [n.get('feedlabel', '')],
            })

        logger.info(f'Steam News: fetched {len(items)} announcements')
    except Exception as e:
        logger.warning(f'Steam News failed: {e}')

    return items


def fetch_steam_discussions(max_pages: int = 3):
    """Fetch Steam discussions across all configured regions（甲方案：双 appid，归档子类 discussion）。"""
    items = []
    for region, app_id in REGION_APPS.get('steam', {'global': '3052450'}).items():
        items.extend(_fetch_steam_discussions_one(str(app_id), region, max_pages=max_pages))
    return items


def _fetch_steam_discussions_one(app_id, region, max_pages: int = 3):
    """Fetch recent Steam Community discussions for one (app_id, region).

    Steam has no public API for discussions, so we scrape the HTML listing page
    (默认按最后回复时间倒序，15 帖/页，?fp=N 翻页)。2026-06 实测 DOM：
    每帖为 <div class="forum_topic ..."> 块，内含 forum_topic_overlay 链接、
    forum_topic_name 标题、forum_topic_op 楼主、forum_topic_lastpost 的
    data-timestamp 真实时间戳，以及 data-tooltip-forum 里的正文预览。
    """
    import html as _html
    import re as _re

    base_url = f'https://steamcommunity.com/app/{app_id}/discussions/0/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,ko;q=0.7,ja;q=0.6',
    }
    cutoff = datetime.now(UTC) - timedelta(hours=HOURS_LOOKBACK)
    items = []

    try:
        for page in range(1, max_pages + 1):
            url = base_url if page == 1 else f'{base_url}?fp={page}'
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            html = resp.text

            # 按 forum_topic 块切分（每块以下一个块或容器结束为界）
            blocks = _re.split(r'<div[^>]+class="forum_topic\s', html)[1:]
            page_added = 0
            for block in blocks:
                m_url = _re.search(
                    r'class="forum_topic_overlay"\s+href="(https://steamcommunity\.com/app/\d+/discussions/[^"]+)"',
                    block)
                m_title = _re.search(r'class="forum_topic_name\s*"[^>]*>\s*(.*?)\s*</div>', block, _re.DOTALL)
                if not m_url or not m_title:
                    continue
                title = _strip_html_tags(m_title.group(1)).strip()
                if not title:
                    continue

                m_replies = _re.search(r'class="forum_topic_reply_count">.*?>\s*([\d,]+)\s*</div>', block, _re.DOTALL)
                replies = int(m_replies.group(1).replace(',', '')) if m_replies else 0

                m_ts = _re.search(r'class="forum_topic_lastpost"[^>]*data-timestamp="(\d+)"', block)
                if m_ts:
                    lastpost = datetime.fromtimestamp(int(m_ts.group(1)), tz=UTC)
                    if lastpost < cutoff:
                        # 列表页首部是**置顶帖**（class 同为 forum_topic），其最后回复
                        # 往往是几个月前。原实现一见旧帖就 break 整页 —— 只要板块挂着
                        # 一个陈旧置顶，第 0 个块就把整轮采集掐断，日志报
                        # 「fetched 0 threads」，与「今天真没人发帖」完全无法区分。
                        # 改为跳过该帖；整页无新帖时由下面 page_added == 0 收尾停翻。
                        continue
                    time_str, approx = lastpost.isoformat(), False
                else:
                    time_str, approx = datetime.now(UTC).isoformat(), True

                m_author = _re.search(r'class="forum_topic_op"[^>]*>\s*([^<]+?)\s*</div>', block)
                author = m_author.group(1).strip() if m_author else ''

                # 正文预览藏在 data-tooltip-forum 的转义 HTML 里
                summary = ''
                m_hover = _re.search(r'data-tooltip-forum="(.*?)">', block, _re.DOTALL)
                if m_hover:
                    hover = _html.unescape(m_hover.group(1))
                    m_text = _re.search(r'class="topic_hover_text"\s*>\s*(.*?)\s*</div>', hover, _re.DOTALL)
                    if m_text:
                        summary = _html.unescape(_strip_html_tags(m_text.group(1))).strip()[:500]

                item = {
                    'title': f'[Steam论坛] {title}',
                    'summary': summary,
                    'source': 'steam_discussion',
                    'region': region,                # 甲方案：global/jp 区服
                    'archive_subtype': 'discussion', # 归档 steam/<区服>/discussion
                    'time': time_str,
                    'url': m_url.group(1),
                    'engagement': replies,
                    'is_hot': replies >= 10,
                    'author': author,
                    'tags': ['steam_forum'],
                }
                if approx:
                    item['time_is_approximate'] = True
                items.append(item)
                page_added += 1

            if page_added == 0:
                break
            time.sleep(0.5)

        logger.info(f'Steam Discussions: fetched {len(items)} threads')
    except Exception as e:
        logger.warning(f'Steam Discussions failed: {e}')

    return items
