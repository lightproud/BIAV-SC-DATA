"""playwright_collectors 补充覆盖：各 fetch_* 的异常分支 + main 编排。

补 tests/test_playwright_collectors.py 未触及的：per-元素解析异常 continue、
per-mode/per-keyword 异常 warning、顶层 Playwright 异常 warning、main()。
绝不启动真实浏览器；用可控的假 page/element 触发各异常路径。
"""

import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import playwright_collectors as pc  # noqa: E402


class RaisingEl:
    """query_selector 抛异常的元素，用于触发 per-元素 except continue。"""

    def query_selector(self, selector):
        raise RuntimeError("dom boom")

    def inner_text(self):
        raise RuntimeError("dom boom")

    def get_attribute(self, name):
        raise RuntimeError("dom boom")


class ConfigurablePage:
    """可配置各方法行为的假 page。

    selector_results: {selector: [els]}；raise_on: 方法名集合，调用即抛异常。
    """

    def __init__(self, selector_results=None, raise_on=None):
        self._results = selector_results or {}
        self._raise_on = raise_on or set()

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, **kwargs):
        if "goto" in self._raise_on:
            raise RuntimeError("goto boom")

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, selector, **kwargs):
        if "wait_for_selector" in self._raise_on:
            raise RuntimeError("selector boom")

    def query_selector_all(self, selector):
        if "query_selector_all" in self._raise_on:
            raise RuntimeError("qsa boom")
        return self._results.get(selector, [])


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
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(page):
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)

    def sync_playwright():
        return FakePlaywrightCM(chromium)

    fake_sync_api = mock.MagicMock()
    fake_sync_api.sync_playwright = sync_playwright
    return mock.patch.dict(sys.modules, {
        "playwright": mock.MagicMock(),
        "playwright.sync_api": fake_sync_api,
    })


def _install_raising_sync():
    """sync_playwright() 本身抛异常，触发顶层 except。"""
    def sync_playwright():
        raise RuntimeError("launch boom")

    fake_sync_api = mock.MagicMock()
    fake_sync_api.sync_playwright = sync_playwright
    return mock.patch.dict(sys.modules, {
        "playwright": mock.MagicMock(),
        "playwright.sync_api": fake_sync_api,
    })


class TestWeiboExceptionBranches(unittest.TestCase):
    def test_article_parse_exception_continues(self):
        # _parse_weibo_article 内 query_selector 抛异常 → per-article except continue (226-227)
        page = ConfigurablePage({"article": [RaisingEl()]})
        with _install(page):
            items = pc.fetch_weibo_playwright()
        self.assertEqual(items, [])

    def test_top_level_exception_warns(self):
        with _install_raising_sync():
            self.assertEqual(pc.fetch_weibo_playwright(), [])


class TestTaptapExceptionBranches(unittest.TestCase):
    SEL = '.app-card, .search-item, [class*="app"]'

    def test_card_parse_exception_continues(self):
        # _parse_taptap_card 抛异常 → per-card except continue (267-268)
        page = ConfigurablePage({self.SEL: [RaisingEl()]})
        with _install(page):
            items = pc.fetch_taptap_playwright()
        self.assertEqual(items, [])

    def test_top_level_exception_warns(self):
        # query_selector_all 抛异常 → 顶层 except warning (271-272)
        page = ConfigurablePage(raise_on={"query_selector_all"})
        with _install(page):
            self.assertEqual(pc.fetch_taptap_playwright(), [])


class TestArcaExceptionBranches(unittest.TestCase):
    def test_per_mode_exception_warns(self):
        # wait_for_selector 抛异常 → per-mode except warning (314-315)，两 mode 均失败
        page = ConfigurablePage(raise_on={"wait_for_selector"})
        with _install(page):
            self.assertEqual(pc.fetch_arca_live_playwright(), [])

    def test_top_level_exception_warns(self):
        with _install_raising_sync():
            self.assertEqual(pc.fetch_arca_live_playwright(), [])


class TestRuliwebExceptionBranches(unittest.TestCase):
    def test_per_keyword_exception_warns(self):
        # goto 抛异常 → per-keyword except warning (354-355)
        page = ConfigurablePage(raise_on={"goto"})
        with _install(page):
            self.assertEqual(pc.fetch_ruliweb_playwright(), [])

    def test_top_level_exception_warns(self):
        with _install_raising_sync():
            self.assertEqual(pc.fetch_ruliweb_playwright(), [])


class TestBahamutExceptionBranches(unittest.TestCase):
    def test_per_keyword_exception_warns(self):
        # goto 抛异常 → per-keyword except warning (396-397)
        page = ConfigurablePage(raise_on={"goto"})
        with _install(page):
            self.assertEqual(pc.fetch_bahamut_playwright(), [])

    def test_top_level_exception_warns(self):
        with _install_raising_sync():
            self.assertEqual(pc.fetch_bahamut_playwright(), [])


class TestMain(unittest.TestCase):
    def test_main_runs_all_collectors(self):
        # main() 调用全部 fetch_*，打桩返回固定结果并走打印分支 (409-427)
        with mock.patch.object(pc, "fetch_weibo_playwright", return_value=[{"title": "weibo item example"}]), \
                mock.patch.object(pc, "fetch_taptap_playwright", return_value=[]), \
                mock.patch.object(pc, "fetch_arca_live_playwright", return_value=[]), \
                mock.patch.object(pc, "fetch_ruliweb_playwright", return_value=[]), \
                mock.patch.object(pc, "fetch_bahamut_playwright", return_value=[]), \
                mock.patch("builtins.print"):
            pc.main()


if __name__ == "__main__":
    unittest.main()
