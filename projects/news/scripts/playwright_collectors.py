#!/usr/bin/env python3
"""
Playwright-based collectors for Morimens community news.
Final fixed version based on actual page structure analysis.

Tested and working:
- NGA: Using .topicrow selector, TD 1 for title
- Weibo: Using article selector on mobile version
- Xiaohongshu: ⚠ Requires login/special handling
"""

import logging
import re
from urllib.parse import urlencode, urljoin
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # 时间归一单一真源（H4）

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

try:
    from collection_state import get_lookback_hours
    HOURS_LOOKBACK = get_lookback_hours()
except ImportError:
    HOURS_LOOKBACK = 24
TIMEOUT_MS = 30000
WEIBO_KEYWORDS = ("忘却前夜", "忘卻前夜")
WEIBO_MAX_SCROLLS = max(0, min(30, news_common.env_int("WEIBO_MAX_SCROLLS", 6)))


def _parse_relative_time(text: str) -> tuple[str, bool]:
    """Parse relative/absolute time strings into ISO datetime.

    委托 news_common.parse_relative_time（H4 收敛后的单一真源）。
    Returns (iso_string, is_approximate).
    """
    return news_common.parse_relative_time(text)


# ── 纯解析函数（从各 fetch_* 抽出，对 DOM 元素接口做映射，便于单测） ──────────
# 这些函数只依赖被传入对象的 query_selector / inner_text / get_attribute 接口，
# 不触碰网络或浏览器，保持原 fetch_* 内联逻辑的行为不变。

def _weibo_metric(text: str, label: str):
    """读取可见计数；没有数字就是未知，不把标签本身当作零。"""
    number = r'(\d[\d,]*(?:\.\d+)?\s*(?:万|亿|[kKmM])?)'
    match = None
    for row in text.splitlines():
        if label not in row:
            continue
        first = re.search(r'转发|评论|赞|\d', row)
        if first and first.group().isdigit():
            match = re.search(number + r'\s*' + re.escape(label), row)
        else:
            match = re.search(re.escape(label) + r'\s*' + number, row)
        if match:
            break
    if not match:
        return None
    raw = match.group(1).replace(',', '').replace(' ', '')
    unit = raw[-1]
    factors = {'万': 10000, '亿': 100000000, 'k': 1000, 'm': 1000000}
    factor = factors.get(unit.lower(), 1)
    return int(float(raw[:-1] if factor != 1 else raw) * factor)


def _parse_weibo_article(article) -> dict:
    """从一个 Weibo article 元素解析出 item dict；不合格返回 None。"""
    text_el = article.query_selector('.weibo-text, .content, p')
    if not text_el:
        return None
    text = text_el.inner_text().strip()
    if len(text) < 10:
        return None

    time_el = article.query_selector('time, [class*="time"], [class*="date"]')
    time_text = ''
    if time_el:
        time_text = time_el.get_attribute('datetime') or time_el.inner_text().strip()
    parsed_time, time_approx = _parse_relative_time(time_text)

    link_el = article.query_selector('a[href*="status"]')
    href = ''
    if link_el:
        href = link_el.get_attribute('href') or ''
        if href and not href.startswith('http'):
            href = urljoin('https://m.weibo.cn/', href)

    author_el = article.query_selector('.weibo-top .m-text-cut, .card-wrap .name, .name, .m-text-cut')
    author = author_el.inner_text().strip() if author_el else ''
    footer_el = article.query_selector('footer, .m-ctrl-box, .card-act')
    footer = footer_el.inner_text() if footer_el else ''
    counts = {key: _weibo_metric(footer, label)
              for key, label in (('reposts', '转发'), ('comments', '评论'), ('likes', '赞'))}
    engagement = sum(value for value in counts.values() if value is not None)

    item = {
        'title': text[:80],
        'summary': text[:500],
        'source': 'weibo',
        'time': parsed_time,
        'url': href,
        'engagement': engagement,
        'engagement_is_unknown': any(value is None for value in counts.values()),
        'metadata': {'engagement_components': counts},
        'is_hot': engagement >= 10,
        'author': author,
        'author_is_unknown': not bool(author),
        'tags': ['weibo'],
    }
    if time_approx:
        item['time_is_approximate'] = True
    return item


def _parse_ruliweb_link(link) -> dict:
    """从一个 Ruliweb a.subject_link 元素解析出 item dict；无标题返回 None。"""
    title = link.inner_text().strip()
    href = link.get_attribute('href') or ''
    if not title:
        return None
    if not href.startswith('http'):
        href = f'https://bbs.ruliweb.com{href}'
    return {
        'title': title[:100],
        'summary': '',
        'source': 'ruliweb',
        'time': datetime.now(UTC).isoformat(),
        'time_is_approximate': True,
        'url': href,
        'engagement': 0,
        'is_hot': False,
        'author': '',
        'tags': ['ruliweb'],
        'lang': 'ko',
        'platform_region': 'kr',
    }


def _parse_bahamut_row(row) -> dict:
    """从一个 Bahamut 搜索结果行元素解析出 item dict；无标题返回 None。"""
    title_el = row.query_selector('.b-list__main__title, a[href*="C.php"]')
    if not title_el:
        return None
    title = title_el.inner_text().strip()
    href = title_el.get_attribute('href') or ''
    if not title:
        return None
    if href and not href.startswith('http'):
        href = f'https://forum.gamer.com.tw/{href}'
    return {
        'title': title[:100],
        'summary': '',
        'source': 'bahamut',
        'time': datetime.now(UTC).isoformat(),
        'time_is_approximate': True,
        'url': href,
        'engagement': 0,
        'is_hot': False,
        'author': '',
        'tags': ['bahamut'],
        'lang': 'zh',
        'platform_region': 'tw',
    }


def fetch_weibo_playwright() -> list[dict]:
    """采集简繁关键词；有限滚动，跨轮 / 跨关键词按稳定身份去重。"""
    items = []
    seen = set()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(TIMEOUT_MS)
                for keyword in WEIBO_KEYWORDS:
                    url = 'https://m.weibo.cn/search?' + urlencode({
                        'containerid': f'100103type=1&q={keyword}'})
                    try:
                        page.goto(url, wait_until='networkidle')
                        page.wait_for_timeout(3000)
                        keyword_seen = set()
                        stagnant = 0
                        for turn in range(WEIBO_MAX_SCROLLS + 1):
                            before = len(keyword_seen)
                            articles = page.query_selector_all('article')
                            for article in articles:
                                try:
                                    item = _parse_weibo_article(article)
                                    if item is None:
                                        continue
                                    # 没直链时沿用归档的稳定身份；近似 now 不参与去重。
                                    key = item['url'] or (
                                        item['title'],
                                        '' if item.get('time_is_approximate') else item['time'],
                                        item['author'])
                                    keyword_seen.add(key)
                                    if key not in seen:
                                        seen.add(key)
                                        items.append(item)
                                except Exception as exc:
                                    logger.debug(f'跳过一条解析失败的 article: {type(exc).__name__}: {exc}')
                            stagnant = stagnant + 1 if len(keyword_seen) == before else 0
                            if stagnant >= 2 or turn == WEIBO_MAX_SCROLLS:
                                break
                            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                            page.wait_for_timeout(1500)
                        logger.info(f'微博 "{keyword}": 解析 {len(keyword_seen)} 条，累计 {len(items)} 条')
                    except Exception as exc:
                        logger.warning(f'微博关键词 "{keyword}" 采集失败: {exc}')
            finally:
                browser.close()
    except Exception as exc:
        logger.warning(f'微博 Playwright 失败: {exc}')
    logger.info(f'微博 Playwright: fetched {len(items)} items')
    return items


def fetch_ruliweb_playwright() -> list[dict]:
    """
    Fetch Ruliweb search results via Playwright.
    """
    items = []
    keywords = ["망각전야", "모리멘스", "Morimens"]

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(TIMEOUT_MS)

            for keyword in keywords:
                try:
                    page.goto(
                        f'https://bbs.ruliweb.com/search?q={keyword}',
                        wait_until='networkidle',
                    )
                    page.wait_for_timeout(2000)

                    links = page.query_selector_all('a.subject_link')
                    for link in links:
                        item = _parse_ruliweb_link(link)
                        if item is not None:
                            items.append(item)
                    logger.info(f'Ruliweb PW "{keyword}": {len(items)} total')
                except Exception as e:
                    logger.warning(f'Ruliweb PW "{keyword}" failed: {e}')

            browser.close()
    except Exception as e:
        logger.warning(f'Ruliweb Playwright failed: {e}')

    logger.info(f'Ruliweb Playwright: fetched {len(items)} items')
    return items


# ── Japanese platforms ────────────────────────────────────────────────────

def fetch_bahamut_playwright() -> list[dict]:
    """
    Fetch Bahamut (gamer.com.tw) search results via Playwright.
    """
    items = []
    keywords = ["忘却前夜", "忘卻前夜", "Morimens"]

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(TIMEOUT_MS)

            for keyword in keywords:
                try:
                    page.goto(
                        f'https://forum.gamer.com.tw/search.php?q={keyword}',
                        wait_until='networkidle',
                    )
                    page.wait_for_timeout(2000)

                    rows = page.query_selector_all('.b-list__row, .FM-blist3A')
                    for row in rows[:20]:
                        item = _parse_bahamut_row(row)
                        if item is not None:
                            items.append(item)
                    logger.info(f'Bahamut PW "{keyword}": {len(items)} total')
                except Exception as e:
                    logger.warning(f'Bahamut PW "{keyword}" failed: {e}')

            browser.close()
    except Exception as e:
        logger.warning(f'Bahamut Playwright failed: {e}')

    logger.info(f'Bahamut Playwright: fetched {len(items)} items')
    return items


def main():
    """Test all Playwright collectors."""
    print("Playwright collectors test")
    print("=" * 60)

    results = {
        'weibo': fetch_weibo_playwright(),
        'ruliweb': fetch_ruliweb_playwright(),
        'bahamut': fetch_bahamut_playwright(),
    }

    for source, items in results.items():
        status = 'OK' if items else 'EMPTY'
        print(f"\n[{status}] {source}: {len(items)} items")
        if items:
            print(f"  example: {items[0]['title'][:50]}...")

    total = sum(len(v) for v in results.values())
    print(f"\ntotal: {total} items")


if __name__ == '__main__':
    main()
