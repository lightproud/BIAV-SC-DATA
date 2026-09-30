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


def _parse_relative_time(text: str) -> tuple[str, bool]:
    """Parse relative/absolute time strings into ISO datetime.

    委托 news_common.parse_relative_time（H4 收敛后的单一真源）。
    Returns (iso_string, is_approximate).
    """
    return news_common.parse_relative_time(text)


# ── 纯解析函数（从各 fetch_* 抽出，对 DOM 元素接口做映射，便于单测） ──────────
# 这些函数只依赖被传入对象的 query_selector / inner_text / get_attribute 接口，
# 不触碰网络或浏览器，保持原 fetch_* 内联逻辑的行为不变。

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
            href = f'https://m.weibo.cn{href}'

    item = {
        'title': text[:80],
        'summary': text[:500],
        'source': 'weibo',
        'time': parsed_time,
        'url': href,
        'engagement': 0,
        'is_hot': False,
        'author': '',
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
    """
    Fetch Weibo search results using mobile version.
    Tested: 15 articles found with content.
    """
    items = []

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(TIMEOUT_MS)

            # 移动版无需登录
            url = 'https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D%E5%BF%98%E5%8D%B4%E5%89%8D%E5%A4%9C'
            logger.info('微博: 访问移动版')
            page.goto(url, wait_until='networkidle')
            page.wait_for_timeout(3000)

            articles = page.query_selector_all('article')
            logger.info(f'微博: 找到 {len(articles)} 条微博')

            for article in articles[:20]:
                try:
                    item = _parse_weibo_article(article)
                    if item is not None:
                        items.append(item)
                # 逐条解析是 best-effort（页面结构常变，单条失败不该毁掉整轮），但
                # 原先的裸 `except Exception: continue` 连解析器**自己写崩**都一起
                # 吞掉——结构改版导致的全军覆没与「今天就是没几条」长得一模一样。
                # 照旧不中断，但出声：条数对不上时日志里有据可查。
                except Exception as exc:
                    logger.debug(f'跳过一条解析失败的article: {type(exc).__name__}: {exc}')
                    continue

            browser.close()
    except Exception as e:
        logger.warning(f'微博 Playwright 失败: {e}')

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
