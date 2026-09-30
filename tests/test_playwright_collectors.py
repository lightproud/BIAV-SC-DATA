import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import playwright_collectors as pc


# ─── DOM 元素桩 ────────────────────────────────────────────
# 模拟 playwright 元素接口：query_selector / inner_text / get_attribute。
# 绝不启动真实浏览器。

class FakeEl:
    """模拟单个 DOM 元素。"""

    def __init__(self, text="", attrs=None, children=None):
        self._text = text
        self._attrs = attrs or {}
        # children: {selector: FakeEl or None}
        self._children = children or {}

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def query_selector(self, selector):
        return self._children.get(selector)


class TestParseRelativeTime(unittest.TestCase):
    def test_delegates_to_news_common(self):
        iso, approx = pc._parse_relative_time("2026-06-09T12:00:00Z")
        self.assertFalse(approx)
        self.assertTrue(iso.startswith("2026-06-09"))

    def test_empty_is_approximate(self):
        iso, approx = pc._parse_relative_time("")
        self.assertTrue(approx)


class TestParseWeiboArticle(unittest.TestCase):
    def _article(self, text, time_el=None, link_el=None):
        children = {'.weibo-text, .content, p': FakeEl(text=text)}
        children['time, [class*="time"], [class*="date"]'] = time_el
        children['a[href*="status"]'] = link_el
        return FakeEl(children=children)

    def test_no_text_element_returns_none(self):
        art = FakeEl(children={'.weibo-text, .content, p': None})
        self.assertIsNone(pc._parse_weibo_article(art))

    def test_short_text_skipped(self):
        self.assertIsNone(pc._parse_weibo_article(self._article("short")))

    def test_valid_article_basic_fields(self):
        art = self._article("这是一条足够长的微博正文内容测试")
        item = pc._parse_weibo_article(art)
        self.assertIsNotNone(item)
        self.assertEqual(item["source"], "weibo")
        self.assertEqual(item["tags"], ["weibo"])
        self.assertEqual(item["engagement"], 0)
        self.assertEqual(item["url"], "")
        # 无时间 → 近似
        self.assertTrue(item.get("time_is_approximate"))

    def test_title_and_summary_truncation(self):
        long_text = "x" * 600
        item = pc._parse_weibo_article(self._article(long_text))
        self.assertEqual(len(item["title"]), 80)
        self.assertEqual(len(item["summary"]), 500)

    def test_time_from_datetime_attribute(self):
        time_el = FakeEl(attrs={"datetime": "2026-06-09T12:00:00Z"})
        item = pc._parse_weibo_article(
            self._article("足够长的正文内容用于测试时间解析", time_el=time_el)
        )
        self.assertNotIn("time_is_approximate", item)
        self.assertTrue(item["time"].startswith("2026-06-09"))

    def test_time_from_inner_text_when_no_datetime(self):
        time_el = FakeEl(text="2026-06-09T12:00:00Z", attrs={})
        item = pc._parse_weibo_article(
            self._article("足够长的正文内容用于时间文本解析", time_el=time_el)
        )
        self.assertTrue(item["time"].startswith("2026-06-09"))

    def test_relative_href_prefixed(self):
        link_el = FakeEl(attrs={"href": "/status/12345"})
        item = pc._parse_weibo_article(
            self._article("足够长的正文内容用于链接解析测试", link_el=link_el)
        )
        self.assertEqual(item["url"], "https://m.weibo.cn/status/12345")

    def test_absolute_href_kept(self):
        link_el = FakeEl(attrs={"href": "https://m.weibo.cn/status/9"})
        item = pc._parse_weibo_article(
            self._article("足够长的正文内容用于绝对链接测试", link_el=link_el)
        )
        self.assertEqual(item["url"], "https://m.weibo.cn/status/9")


class TestParseTaptapCard(unittest.TestCase):
    def _card(self, title_text=None, href=None):
        title_el = FakeEl(text=title_text) if title_text is not None else None
        link_el = FakeEl(attrs={"href": href}) if href is not None else None
        return FakeEl(children={
            '.app-name, .title, h3': title_el,
            'a[href*="/app/"]': link_el,
        })

    def test_no_app_link_returns_none(self):
        self.assertIsNone(pc._parse_taptap_card(self._card(title_text="X", href=None)))

    def test_link_without_app_path_returns_none(self):
        self.assertIsNone(pc._parse_taptap_card(self._card(title_text="X", href="/other/1")))

    def test_valid_card_relative_href(self):
        item = pc._parse_taptap_card(self._card(title_text="忘却前夜", href="/app/123"))
        self.assertEqual(item["url"], "https://www.taptap.cn/app/123")
        self.assertEqual(item["title"], "[TapTap] 忘却前夜")
        self.assertTrue(item["time_is_approximate"])

    def test_absolute_href_kept(self):
        item = pc._parse_taptap_card(
            self._card(title_text="Title", href="https://www.taptap.cn/app/9")
        )
        self.assertEqual(item["url"], "https://www.taptap.cn/app/9")

    def test_missing_title_uses_default(self):
        card = self._card(title_text=None, href="/app/5")
        item = pc._parse_taptap_card(card)
        self.assertEqual(item["title"], "[TapTap] 忘却前夜")


class TestParseArcaRow(unittest.TestCase):
    def _row(self, title=None, time_text=None, href=None):
        title_el = FakeEl(text=title) if title is not None else None
        time_el = FakeEl(text=time_text) if time_text is not None else None
        link_el = FakeEl(attrs={"href": href}) if href is not None else None
        return FakeEl(children={
            '.title': title_el,
            '.col-time': time_el,
            'a.vrow-top': link_el,
        })

    def test_no_title_element_returns_none(self):
        self.assertIsNone(pc._parse_arca_row(self._row(title=None), ""))

    def test_empty_title_returns_none(self):
        self.assertIsNone(pc._parse_arca_row(self._row(title="   "), ""))

    def test_best_mode_is_hot(self):
        item = pc._parse_arca_row(self._row(title="제목", href="/b/1"), "best")
        self.assertTrue(item["is_hot"])
        self.assertEqual(item["url"], "https://arca.live/b/1")
        self.assertEqual(item["lang"], "ko")
        self.assertEqual(item["platform_region"], "kr")

    def test_latest_mode_not_hot(self):
        item = pc._parse_arca_row(self._row(title="제목", href="https://arca.live/b/2"), "")
        self.assertFalse(item["is_hot"])
        self.assertEqual(item["url"], "https://arca.live/b/2")

    def test_title_truncated_to_100(self):
        item = pc._parse_arca_row(self._row(title="가" * 150), "")
        self.assertEqual(len(item["title"]), 100)

    def test_time_parsed(self):
        item = pc._parse_arca_row(
            self._row(title="제목", time_text="2026-06-09T12:00:00Z"), ""
        )
        self.assertNotIn("time_is_approximate", item)


class TestParseRuliwebLink(unittest.TestCase):
    def test_empty_title_returns_none(self):
        self.assertIsNone(pc._parse_ruliweb_link(FakeEl(text="  ", attrs={"href": "/x"})))

    def test_relative_href_prefixed(self):
        item = pc._parse_ruliweb_link(FakeEl(text="title", attrs={"href": "/board/1"}))
        self.assertEqual(item["url"], "https://bbs.ruliweb.com/board/1")
        self.assertEqual(item["source"], "ruliweb")
        self.assertTrue(item["time_is_approximate"])

    def test_absolute_href_kept(self):
        item = pc._parse_ruliweb_link(
            FakeEl(text="title", attrs={"href": "https://bbs.ruliweb.com/board/2"})
        )
        self.assertEqual(item["url"], "https://bbs.ruliweb.com/board/2")

    def test_missing_href_defaults_empty_prefixed(self):
        item = pc._parse_ruliweb_link(FakeEl(text="title", attrs={}))
        self.assertEqual(item["url"], "https://bbs.ruliweb.com")

    def test_title_truncated(self):
        item = pc._parse_ruliweb_link(FakeEl(text="a" * 200, attrs={"href": "/x"}))
        self.assertEqual(len(item["title"]), 100)


class TestParseBahamutRow(unittest.TestCase):
    def _row(self, title=None, href=None):
        title_el = None
        if title is not None:
            title_el = FakeEl(text=title, attrs={"href": href} if href is not None else {})
        return FakeEl(children={'.b-list__main__title, a[href*="C.php"]': title_el})

    def test_no_title_element_returns_none(self):
        self.assertIsNone(pc._parse_bahamut_row(self._row(title=None)))

    def test_empty_title_returns_none(self):
        self.assertIsNone(pc._parse_bahamut_row(self._row(title="  ", href="C.php?bsn=1")))

    def test_relative_href_prefixed(self):
        item = pc._parse_bahamut_row(self._row(title="標題", href="C.php?bsn=1&snA=2"))
        self.assertEqual(item["url"], "https://forum.gamer.com.tw/C.php?bsn=1&snA=2")
        self.assertEqual(item["lang"], "zh")
        self.assertEqual(item["platform_region"], "tw")

    def test_absolute_href_kept(self):
        item = pc._parse_bahamut_row(
            self._row(title="標題", href="https://forum.gamer.com.tw/C.php?bsn=3")
        )
        self.assertEqual(item["url"], "https://forum.gamer.com.tw/C.php?bsn=3")

    def test_title_truncated(self):
        item = pc._parse_bahamut_row(self._row(title="标" * 150, href="C.php?bsn=1"))
        self.assertEqual(len(item["title"]), 100)


# ─── 全 fetch_* 函数：mock 整个 playwright sync API ──────────
# 用一个假的 playwright.sync_api 模块注入 sys.modules，使
# `from playwright.sync_api import sync_playwright` 在函数内可用。
# 绝不启动真实浏览器。

class FakePage:
    def __init__(self, selector_results=None):
        # selector_results: {selector: [FakeEl, ...]}
        self._selector_results = selector_results or {}

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, selector, **kwargs):
        pass

    def query_selector_all(self, selector):
        return self._selector_results.get(selector, [])


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_page(self, **kwargs):
        return self._page

    def close(self):
        pass


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, **kwargs):
        return self._browser


class FakePlaywrightCM:
    """sync_playwright() 返回的对象，支持上下文管理器协议。"""

    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_playwright(page):
    """构造一个假的 playwright.sync_api 模块。返回 (module, sync_playwright_fn)。"""
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)

    def sync_playwright():
        return FakePlaywrightCM(chromium)

    fake_sync_api = mock.MagicMock()
    fake_sync_api.sync_playwright = sync_playwright
    return fake_sync_api


class FetchPlaywrightTestMixin:
    def _run_with_page(self, page):
        fake_sync_api = _install_fake_playwright(page)
        modules = {
            "playwright": mock.MagicMock(),
            "playwright.sync_api": fake_sync_api,
        }
        return mock.patch.dict(sys.modules, modules)


class TestFetchWeiboPlaywright(unittest.TestCase, FetchPlaywrightTestMixin):
    def test_collects_valid_articles(self):
        art = FakeEl(children={
            '.weibo-text, .content, p': FakeEl(text="足够长的微博正文内容用于采集测试"),
            'time, [class*="time"], [class*="date"]': None,
            'a[href*="status"]': None,
        })
        page = FakePage({'article': [art]})
        with self._run_with_page(page):
            items = pc.fetch_weibo_playwright()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "weibo")

    def test_empty_when_no_articles(self):
        page = FakePage({'article': []})
        with self._run_with_page(page):
            self.assertEqual(pc.fetch_weibo_playwright(), [])

    def test_import_error_returns_empty(self):
        # 不注入 playwright → import 失败 → 返回空
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            self.assertEqual(pc.fetch_weibo_playwright(), [])


class TestFetchTaptapPlaywright(unittest.TestCase, FetchPlaywrightTestMixin):
    def test_collects_cards(self):
        card = FakeEl(children={
            '.app-name, .title, h3': FakeEl(text="忘却前夜"),
            'a[href*="/app/"]': FakeEl(attrs={"href": "/app/1"}),
        })
        page = FakePage({'.app-card, .search-item, [class*="app"]': [card]})
        with self._run_with_page(page):
            items = pc.fetch_taptap_playwright()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "taptap")

    def test_empty(self):
        page = FakePage({'.app-card, .search-item, [class*="app"]': []})
        with self._run_with_page(page):
            self.assertEqual(pc.fetch_taptap_playwright(), [])


class TestFetchArcaPlaywright(unittest.TestCase, FetchPlaywrightTestMixin):
    def test_collects_rows_both_modes(self):
        row = FakeEl(children={
            '.title': FakeEl(text="제목"),
            '.col-time': None,
            'a.vrow-top': FakeEl(attrs={"href": "/b/1"}),
        })
        page = FakePage({'.vrow:not(.notice)': [row]})
        with self._run_with_page(page):
            items = pc.fetch_arca_live_playwright()
        # 两个 mode 各采一条
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["source"], "arca_live")


class TestFetchRuliwebPlaywright(unittest.TestCase, FetchPlaywrightTestMixin):
    def test_collects_links(self):
        link = FakeEl(text="검색결과", attrs={"href": "/board/1"})
        page = FakePage({'a.subject_link': [link]})
        with self._run_with_page(page):
            items = pc.fetch_ruliweb_playwright()
        # 三个 keyword 各采一条
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["source"], "ruliweb")


class TestFetchBahamutPlaywright(unittest.TestCase, FetchPlaywrightTestMixin):
    def test_collects_rows(self):
        row = FakeEl(children={
            '.b-list__main__title, a[href*="C.php"]': FakeEl(
                text="標題", attrs={"href": "C.php?bsn=1"}
            ),
        })
        page = FakePage({'.b-list__row, .FM-blist3A': [row]})
        with self._run_with_page(page):
            items = pc.fetch_bahamut_playwright()
        # 三个 keyword 各采一条
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["source"], "bahamut")


if __name__ == "__main__":
    unittest.main()
