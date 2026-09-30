"""taptap_collector async-path coverage — the existing test covers only pure
parse/filter/state. Here we drive the Playwright-shaped async code with fake
async page/response objects (no browser, no network) to exercise
_autoscroll_collect, _extract_topics / _extract_reviews (API-merge + DOM
fallback), the DOM extractors, and the top-level collect() orchestration.

All file writes redirected to tmp DATA_DIR; asyncio.run used to drive coroutines.
"""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import taptap_collector as tt


def _run(coro):
    return asyncio.run(coro)


class FakePage:
    """Minimal async page double: records handlers, serves a scripted DOM
    evaluate result, and counts scrolls."""

    def __init__(self, dom_result=None, content="<html>x</html>"):
        self._dom_result = dom_result if dom_result is not None else []
        self._content = content
        self.scrolls = 0
        self.closed = False

    def on(self, event, handler):
        self._handler = handler

    async def goto(self, url, **kw):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, script):
        if "scrollTo" in script:
            self.scrolls += 1
            return None
        return self._dom_result

    async def content(self):
        return self._content

    async def close(self):
        self.closed = True


class TestAutoscrollCollect(unittest.TestCase):
    def test_merges_and_dedups(self):
        captured = [("u1", {"data": [{"title": "A", "id": 1}]})]

        def parse_fn(body):
            return tt._parse_topic_api_body(body)

        page = FakePage()
        out = _run(tt._autoscroll_collect(page, parse_fn, captured, 2, None))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "A")

    def test_stops_when_cutoff_reached(self):
        # an item older than cutoff in the very first merge → loop short-circuits
        old_iso = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        captured = [("u", [{"title": "old", "item_id": "1", "created": old_iso}])]

        def parse_fn(body):
            # body is already a list of pre-parsed items here
            return body if isinstance(body, list) else []

        cutoff = datetime.now(UTC) - timedelta(days=1)
        page = FakePage()
        out = _run(tt._autoscroll_collect(page, parse_fn, captured, 5, cutoff))
        self.assertEqual(len(out), 1)
        # cutoff reached on first check → no scrolling
        self.assertEqual(page.scrolls, 0)

    def test_stale_breaks_after_two_empty_rounds(self):
        captured = []  # nothing ever captured

        def parse_fn(body):
            return []

        page = FakePage()
        out = _run(tt._autoscroll_collect(page, parse_fn, captured, 10, None))
        self.assertEqual(out, [])
        # breaks after 2 stale rounds, well before 10
        self.assertLessEqual(page.scrolls, 3)


class TestExtractTopics(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(tt, "DATA_DIR", Path(self._d.name))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._d.cleanup()

    def test_api_merge_returns_items(self):
        page = FakePage()

        async def fake_autoscroll(*a, **k):
            return [{"title": "T", "item_id": "1", "created": "2026-01-01T00:00:00+00:00"}]

        with mock.patch.object(tt, "_autoscroll_collect", side_effect=fake_autoscroll):
            out = _run(tt._extract_topics(page, max_scrolls=2))
        self.assertEqual(out[0]["title"], "T")

    def test_falls_back_to_dom(self):
        dom = [{"title": "DomPost", "time_str": "", "likes": "5", "comments": "1",
                "url": "u", "author": "a"}]
        page = FakePage(dom_result=dom)

        async def empty_autoscroll(*a, **k):
            return []

        with mock.patch.object(tt, "_autoscroll_collect", side_effect=empty_autoscroll), \
                mock.patch.object(tt.news_common, "parse_relative_time",
                                  return_value=("2026-01-01T00:00:00+00:00", True)):
            out = _run(tt._extract_topics(page, max_scrolls=2))
        self.assertEqual(out[0]["title"], "DomPost")
        self.assertTrue(out[0]["time_is_approximate"])

    def test_goto_exception_tolerated(self):
        page = FakePage()

        async def boom_goto(url, **kw):
            raise RuntimeError("nav fail")

        page.goto = boom_goto

        async def empty(*a, **k):
            return []

        with mock.patch.object(tt, "_autoscroll_collect", side_effect=empty), \
                mock.patch.object(tt.news_common, "parse_relative_time",
                                  return_value=("2026-01-01T00:00:00+00:00", True)):
            out = _run(tt._extract_topics(page, max_scrolls=1))
        self.assertEqual(out, out or [])  # no crash


class TestExtractTopicsDom(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(tt, "DATA_DIR", Path(self._d.name))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._d.cleanup()

    def test_no_elements_warns_empty(self):
        page = FakePage(dom_result=[])
        out = _run(tt._extract_topics_dom(page))
        self.assertEqual(out, [])

    def test_extracts_with_time(self):
        dom = [{"title": "Has Time", "time_str": "2026-01-01", "likes": "10",
                "comments": "2", "url": "u", "author": "a"}]
        page = FakePage(dom_result=dom)
        with mock.patch.object(tt.news_common, "parse_relative_time",
                               return_value=("2026-01-01T00:00:00+00:00", False)):
            out = _run(tt._extract_topics_dom(page))
        self.assertEqual(out[0]["like_count"], 10)
        self.assertNotIn("time_is_approximate", out[0])

    def test_skips_titleless(self):
        dom = [{"title": "", "time_str": "x"}, {"title": "keep", "time_str": "x"}]
        page = FakePage(dom_result=dom)
        with mock.patch.object(tt.news_common, "parse_relative_time",
                               return_value=("2026-01-01T00:00:00+00:00", False)):
            out = _run(tt._extract_topics_dom(page))
        self.assertEqual([o["title"] for o in out], ["keep"])


class TestExtractReviews(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(tt, "DATA_DIR", Path(self._d.name))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._d.cleanup()

    def test_api_merge(self):
        page = FakePage()

        async def fake(*a, **k):
            return [{"title": "R", "item_id": "1", "created": "2026-01-01T00:00:00+00:00"}]

        with mock.patch.object(tt, "_autoscroll_collect", side_effect=fake):
            out = _run(tt._extract_reviews(page, max_scrolls=2))
        self.assertEqual(out[0]["title"], "R")

    def test_dom_fallback(self):
        dom = [{"content": "Nice review here", "time_str": "", "likes": "3",
                "author": "rev", "score": "4", "url": "u"}]
        page = FakePage(dom_result=dom)

        async def empty(*a, **k):
            return []

        with mock.patch.object(tt, "_autoscroll_collect", side_effect=empty), \
                mock.patch.object(tt.news_common, "parse_relative_time",
                                  return_value=("2026-01-01T00:00:00+00:00", True)):
            out = _run(tt._extract_reviews(page, max_scrolls=1))
        self.assertEqual(out[0]["summary"], "Nice review here")
        self.assertTrue(out[0]["time_is_approximate"])


class TestExtractReviewsDom(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(tt, "DATA_DIR", Path(self._d.name))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._d.cleanup()

    def test_skips_empty_content(self):
        dom = [{"content": "", "time_str": "x"}]
        page = FakePage(dom_result=dom)
        out = _run(tt._extract_reviews_dom(page))
        self.assertEqual(out, [])

    def test_keeps_with_time(self):
        dom = [{"content": "Good game", "time_str": "2026-01-01", "likes": "9",
                "author": "a", "score": "5", "url": "u"}]
        page = FakePage(dom_result=dom)
        with mock.patch.object(tt.news_common, "parse_relative_time",
                               return_value=("2026-01-01T00:00:00+00:00", False)):
            out = _run(tt._extract_reviews_dom(page))
        self.assertEqual(out[0]["like_count"], 9)
        self.assertNotIn("time_is_approximate", out[0])

    def test_drops_rows_where_content_echoes_author(self):
        """正文==用户名 = 选择器抓到了卡片头部块，必须丢弃而不是落档。

        回归实测：两周 1,185 条 TapTap「评论」全部 title=summary=author=用户名、
        无正文无评分——这批数据事后无法自动识别，只能靠人肉发现。
        """
        dom = [{"content": "葱爆牛排", "author": "葱爆牛排", "time_str": "2026-08-15",
                "url": "https://www.taptap.cn/review/50027316"}]
        page = FakePage(dom_result=dom)
        out = _run(tt._extract_reviews_dom(page))
        self.assertEqual(out, [])

    def test_dedups_same_review_url(self):
        dom = [
            {"content": "很好玩", "author": "a", "time_str": "2026-08-15", "url": "https://t/review/1"},
            {"content": "很好玩", "author": "a", "time_str": "2026-08-15", "url": "https://t/review/1"},
            {"content": "还行", "author": "b", "time_str": "2026-08-15", "url": "https://t/review/2"},
        ]
        page = FakePage(dom_result=dom)
        with mock.patch.object(tt.news_common, "parse_relative_time",
                               return_value=("2026-08-15T00:00:00+00:00", False)):
            out = _run(tt._extract_reviews_dom(page))
        self.assertEqual(len(out), 2)

    def test_score_lands_in_title_and_field(self):
        dom = [{"content": "很好玩", "author": "a", "time_str": "2026-08-15",
                "score": "4.5", "url": "https://t/review/1"}]
        page = FakePage(dom_result=dom)
        with mock.patch.object(tt.news_common, "parse_relative_time",
                               return_value=("2026-08-15T00:00:00+00:00", False)):
            out = _run(tt._extract_reviews_dom(page))
        self.assertEqual(out[0]["score"], 5)
        self.assertTrue(out[0]["title"].startswith("★★★★★"))


class TestParseTaptapDomScore(unittest.TestCase):
    def test_star_glyphs(self):
        self.assertEqual(tt._parse_taptap_dom_score("★★★★☆"), 4)

    def test_numeric_forms(self):
        self.assertEqual(tt._parse_taptap_dom_score("4.5"), 5)
        self.assertEqual(tt._parse_taptap_dom_score("3 分"), 3)

    def test_unparseable_returns_zero(self):
        """拿不准就返回 0（下游据此知道「没有评分」），不编一个出来。"""
        for bad in (None, "", "评分", "99"):
            self.assertEqual(tt._parse_taptap_dom_score(bad), 0)


# ── collect() orchestration (fully mocked playwright) ────────────────────────

class FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page

    async def close(self):
        return None


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_context(self, **kw):
        return FakeContext(self._page)

    async def close(self):
        return None


class FakeChromium:
    def __init__(self, page):
        self._page = page

    async def launch(self, **kw):
        return FakeBrowser(self._page)


class FakePW:
    def __init__(self, page):
        self.chromium = FakeChromium(page)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class TestCollect(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(tt, "DATA_DIR", Path(self._d.name)),
            mock.patch.object(tt, "STATE_PATH", Path(self._d.name) / "state.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._d.cleanup()

    def _fake_playwright_module(self, page):
        mod = mock.MagicMock()
        mod.async_playwright = lambda: FakePW(page)
        return mod

    def test_collect_happy_path(self):
        page = FakePage()
        recent = datetime.now(UTC).isoformat()

        async def fake_topics(p, ms, cutoff):
            return [{"title": "post", "item_id": "p1", "created": recent,
                     "like_count": 100, "comment_count": 5, "url": "u", "summary": "", "author": "a"}]

        async def fake_reviews(p, ms, cutoff):
            return [{"title": "rev", "item_id": "r1", "created": recent,
                     "like_count": 2, "comment_count": 0, "url": "u2", "summary": "s", "author": "b"}]

        with mock.patch.dict(sys.modules, {"playwright": mock.MagicMock(),
                                           "playwright.async_api": self._fake_playwright_module(page)}), \
                mock.patch.object(tt, "_extract_topics", side_effect=fake_topics), \
                mock.patch.object(tt, "_extract_reviews", side_effect=fake_reviews):
            topics, reviews = _run(tt.collect(max_scrolls=1))
        self.assertEqual(len(topics), 1)
        self.assertEqual(len(reviews), 1)
        # state persisted
        self.assertTrue((Path(self._d.name) / "state.json").exists())

    def test_collect_extraction_exception_tolerated(self):
        page = FakePage()

        async def boom(*a, **k):
            raise RuntimeError("extract fail")

        with mock.patch.dict(sys.modules, {"playwright": mock.MagicMock(),
                                           "playwright.async_api": self._fake_playwright_module(page)}), \
                mock.patch.object(tt, "_extract_topics", side_effect=boom), \
                mock.patch.object(tt, "_extract_reviews", side_effect=boom):
            topics, reviews = _run(tt.collect(max_scrolls=1))
        self.assertEqual(topics, [])
        self.assertEqual(reviews, [])


if __name__ == "__main__":
    unittest.main()
