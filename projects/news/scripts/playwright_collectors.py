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
import os
from urllib.parse import urlencode, urljoin
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # 时间归一单一真源（H4）
from weibo_common import metrics

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

    link_el = article.query_selector('a[href*="status"], a[href*="/detail/"]')
    raw_href = link_el.get_attribute('href') if link_el else ''
    href = urljoin('https://m.weibo.cn', raw_href) if raw_href else ''
    href = href.replace('/status/', '/detail/').split('?')[0]
    author_el = article.query_selector('.m-text-cut, .username, a[href*="/u/"]')
    author = author_el.inner_text().strip() if author_el else ''
    labels = {
        'reposts': '[data-count="reposts"], .reposts, [class*="repost"]',
        'comments': '[data-count="comments"], .comments, [class*="comment-count"]',
        'likes': '[data-count="likes"], .likes, [class*="like-count"]',
    }
    counts = {}
    for key, selector in labels.items():
        element = article.query_selector(selector)
        counts[key] = (element.get_attribute('data-value') or element.inner_text()) if element else None
    # Mobile footer buttons identify the metric by label or icon, never by order.
    for button in article.query_selector_all('.m-diy-btn, .card-act li'):
        text = button.inner_text().strip()
        for key, words, icon in (
            ('reposts', ('转发', '轉發'), 'i[class*="retweet"]'),
            ('comments', ('评论', '評論'), 'i[class*="comment"]'),
            ('likes', ('赞', '讚'), 'i[class*="like"]'),
        ):
            if any(word in text for word in words) or button.query_selector(icon):
                counts[key] = text
                break
    engagement, metadata = metrics(**counts)
    metadata['author_is_unknown'] = not bool(author)

    item = {
        'title': text[:80],
        'summary': text[:500],
        'source': 'weibo',
        'time': parsed_time,
        'url': href,
        'engagement': engagement,
        'is_hot': (metadata['likes_count'] or 0) > 500,
        'author': author,
        'metadata': metadata,
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


def _collect_weibo_search(page, seen, max_rounds):
    """Bounded scrolling; stop after two consecutive snapshots add no new items."""
    items = []
    stalled = 0
    for _ in range(max_rounds):
        added = 0
        for article in page.query_selector_all('article'):
            try:
                item = _parse_weibo_article(article)
                if item is None:
                    continue
                key = item['url'] or (item['summary'], item['author'])
                if key not in seen:
                    seen.add(key)
                    items.append(item)
                    added += 1
            except Exception as exc:
                logger.debug('微博 article 解析失败: %s', type(exc).__name__)
        stalled = 0 if added else stalled + 1
        if stalled >= 2:
            break
        try:
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(2000)
        except Exception as exc:
            logger.warning('微博滚动中断: %s', type(exc).__name__)
            break  # Preserve entries already read from earlier snapshots.
    return items


def fetch_weibo_playwright() -> list[dict]:
    """API fallback: simplified/traditional searches with bounded infinite scroll."""
    items = []
    seen = set()
    max_rounds = max(1, min(news_common.env_int('WEIBO_MAX_PAGES', 5), 20))
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(TIMEOUT_MS)
                cookie = os.environ.get('WEIBO_COOKIE', '')
                if cookie:
                    page.context.add_cookies([
                        {'name': name.strip(), 'value': value.strip(),
                         'domain': '.weibo.cn', 'path': '/'}
                        for part in cookie.split(';') if '=' in part
                        for name, value in [part.split('=', 1)] if name.strip()
                    ])
                for keyword in ('忘却前夜', '忘卻前夜'):
                    try:
                        url = 'https://m.weibo.cn/search?' + urlencode({
                            'containerid': f'100103type=1&q={keyword}'})
                        page.goto(url, wait_until='domcontentloaded')
                        page.wait_for_timeout(3000)
                        items.extend(_collect_weibo_search(page, seen, max_rounds))
                    except Exception as exc:
                        # Avoid echoing a request/header that could contain cookies.
                        logger.warning('微博 PW 搜索失败 (%s): %s', keyword, type(exc).__name__)
            finally:
                browser.close()
    except Exception as exc:
        logger.warning('微博 Playwright 失败: %s', type(exc).__name__)
    logger.info('微博 Playwright: fetched %s items', len(items))
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
