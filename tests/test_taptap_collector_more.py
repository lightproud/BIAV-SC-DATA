"""taptap_collector 补充覆盖：response 拦截 / 滚动异常 / DOM 提取 / 边界解析。

补 tests/test_taptap_collector.py + test_taptap_scroll.py 未触及的：
_extract_topics/_extract_reviews 的 response handler、_autoscroll 异常分支、
DOM 调试写入异常、review/topic body 的额外解析分支。
不开浏览器、不触网；文件写入 monkeypatch 到 tmp。
"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import taptap_collector as tc  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ─── async response 桩 ────────────────────────────────────────

class FakeResponse:
    def __init__(self, url, status=200, content_type="application/json", body=None, raise_json=False):
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body if body is not None else {}
        self._raise_json = raise_json

    async def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._body


class CapturingPage:
    """捕获 page.on('response', handler) 注册的 handler，便于直接喂响应。

    goto/wait_for_timeout 可配置抛异常；content() 返回固定 HTML；
    evaluate() 返回固定 DOM 结果。
    """

    def __init__(self, goto_raises=False, dom_result=None, content_text="<html>x</html>"):
        self._handler = None
        self._goto_raises = goto_raises
        self._dom_result = dom_result
        self._content_text = content_text

    def on(self, event, handler):
        self._handler = handler

    async def goto(self, url, **kwargs):
        if self._goto_raises:
            raise RuntimeError("goto failed")

    async def wait_for_timeout(self, ms):
        pass

    async def evaluate(self, js):
        return self._dom_result

    async def content(self):
        return self._content_text


# ─── _autoscroll_collect 异常分支 ─────────────────────────────

class TestAutoscrollEdge(unittest.TestCase):
    def test_reached_cutoff_swallows_bad_created(self):
        # merged 中条目 created 非法 → min() 内 fromisoformat 抛异常 → except False (113-114)
        captured = [("u", {"items": [{"item_id": "1", "created": "garbage"}]})]

        class P:
            async def evaluate(self, _):
                return None

            async def wait_for_timeout(self, _):
                pass

        cutoff = datetime.now(UTC) - timedelta(days=1)
        items = _run(tc._autoscroll_collect(
            P(), lambda b: b.get("items", []), captured, max_scrolls=1, cutoff=cutoff))
        self.assertEqual(len(items), 1)

    def test_scroll_evaluate_exception_swallowed(self):
        # page.evaluate 抛异常 → except pass (124-125)，仍正常合并首屏
        captured = [("u", {"items": [{"item_id": "1",
                    "created": datetime.now(UTC).isoformat()}]})]

        class P:
            async def evaluate(self, _):
                raise RuntimeError("scroll boom")

            async def wait_for_timeout(self, _):
                pass

        items = _run(tc._autoscroll_collect(
            P(), lambda b: b.get("items", []), captured, max_scrolls=2, cutoff=None))
        self.assertEqual(len(items), 1)


# ─── _extract_topics response handler ─────────────────────────

class TestExtractTopicsHandler(unittest.TestCase):
    def _drive(self, page, autoscroll_return=None):
        # _extract_topics 内会调用 _autoscroll_collect；打桩它，让我们手动喂响应给 handler
        with mock.patch.object(tc, "_autoscroll_collect",
                               new=mock.AsyncMock(return_value=autoscroll_return or [])):
            return _run(tc._extract_topics(page, max_scrolls=1, cutoff=None))

    def test_handler_captures_matching_topic_response(self):
        page = CapturingPage()
        # 触发注册 handler：返回 [] 让其 fallback DOM，但我们只关心 handler 执行
        with mock.patch.object(tc, "_extract_topics_dom",
                               new=mock.AsyncMock(return_value=[])):
            with mock.patch.object(tc, "_autoscroll_collect",
                                   new=mock.AsyncMock(return_value=[])):
                _run(tc._extract_topics(page, max_scrolls=1, cutoff=None))
        # 现在手动喂各种响应给捕获的 handler，覆盖 216-229 的过滤分支
        h = page._handler
        # 非 200 → return (217-218)
        _run(h(FakeResponse("https://x/topic", status=404)))
        # 非 json → return (219-220)
        _run(h(FakeResponse("https://x/topic", content_type="text/html")))
        # url 不匹配模式 → return (222-224)
        _run(h(FakeResponse("https://x/other")))
        # json 抛异常 → except pass (228-229)
        _run(h(FakeResponse("https://x/topic", raise_json=True)))
        # 命中：匹配 + json ok → captured.append (225-227)
        _run(h(FakeResponse("https://x/topic", body={"data": []})))

    def test_goto_warning_then_autoscroll_result(self):
        # goto 抛异常 → warning (235-236)；_autoscroll 有结果 → 直接返回 (243-245)
        page = CapturingPage(goto_raises=True)
        out = self._drive(page, autoscroll_return=[{"title": "x"}])
        self.assertEqual(out, [{"title": "x"}])

    def test_fallback_to_dom_when_empty(self):
        # _autoscroll 空 → fallback DOM (248-249)
        page = CapturingPage()
        with mock.patch.object(tc, "_autoscroll_collect",
                               new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(tc, "_extract_topics_dom",
                                  new=mock.AsyncMock(return_value=[{"title": "dom"}])) as dom:
            out = _run(tc._extract_topics(page, max_scrolls=1, cutoff=None))
        self.assertEqual(out, [{"title": "dom"}])
        dom.assert_awaited()


# ─── _extract_reviews response handler ────────────────────────

class TestExtractReviewsHandler(unittest.TestCase):
    def test_handler_branches_and_goto_warning(self):
        page = CapturingPage(goto_raises=True)
        with mock.patch.object(tc, "_autoscroll_collect",
                               new=mock.AsyncMock(return_value=[{"title": "r"}])):
            out = _run(tc._extract_reviews(page, max_scrolls=1, cutoff=None))
        self.assertEqual(out, [{"title": "r"}])
        h = page._handler
        _run(h(FakeResponse("https://x/review", status=500)))      # 非 200
        _run(h(FakeResponse("https://x/review", content_type="text/plain")))  # 非 json
        _run(h(FakeResponse("https://x/nomatch")))                 # url 不匹配
        _run(h(FakeResponse("https://x/review", raise_json=True))) # json 抛异常
        _run(h(FakeResponse("https://x/rating", body={"data": []})))  # 命中

    def test_reviews_fallback_to_dom(self):
        page = CapturingPage()
        with mock.patch.object(tc, "_autoscroll_collect",
                               new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(tc, "_extract_reviews_dom",
                                  new=mock.AsyncMock(return_value=[{"title": "domr"}])) as dom:
            out = _run(tc._extract_reviews(page, max_scrolls=1, cutoff=None))
        self.assertEqual(out, [{"title": "domr"}])
        dom.assert_awaited()


# ─── DOM 提取：调试写入异常分支 ───────────────────────────────

class TestExtractDomDebugWriteError(unittest.TestCase):
    def test_topics_dom_write_exception_swallowed(self):
        # debug_path.write_text 抛异常 → except pass (260-261)，仍返回解析结果
        dom_result = [{"title": "T", "time_str": "", "likes": "5", "comments": "2",
                       "url": "u", "author": "a"}]
        page = CapturingPage(dom_result=dom_result)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(tc, "DATA_DIR", Path(d)), \
                    mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
                items = _run(tc._extract_topics_dom(page))
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["time_is_approximate"])

    def test_reviews_dom_write_exception_swallowed(self):
        # 评价 DOM debug 写入异常 (458-459)
        dom_result = [{"content": "great review text", "time_str": "",
                       "likes": "3", "author": "a", "score": "5", "url": "u"}]
        page = CapturingPage(dom_result=dom_result)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(tc, "DATA_DIR", Path(d)), \
                    mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
                items = _run(tc._extract_reviews_dom(page))
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["time_is_approximate"])

    def test_topics_dom_empty_result_warns(self):
        # DOM 解析为空 → warning 分支 (330-331)
        page = CapturingPage(dom_result=[])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(tc, "DATA_DIR", Path(d)):
                items = _run(tc._extract_topics_dom(page))
        self.assertEqual(items, [])

    def test_reviews_dom_empty_result_warns(self):
        page = CapturingPage(dom_result=[])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(tc, "DATA_DIR", Path(d)):
                items = _run(tc._extract_reviews_dom(page))
        self.assertEqual(items, [])


# ─── body 解析额外分支 ────────────────────────────────────────

class TestParseBodyExtraBranches(unittest.TestCase):
    def test_review_list_at_top_level(self):
        # data 本身是 list → review_list = data (342)
        body = {"data": [{"content": "hi", "id": 1}]}
        out = tc._parse_review_api_body(body)
        self.assertEqual(len(out), 1)

    def test_review_non_dict_item_skipped(self):
        # 列表内非 dict 元素被跳过 (364)
        body = {"data": {"reviews": ["junk", {"content": "ok", "id": 2}]}}
        out = tc._parse_review_api_body(body)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["summary"], "ok")

    def test_review_string_timestamp(self):
        # ts 为数字字符串 → int(ts) (380)
        sec = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
        body = {"data": {"reviews": [{"content": "x", "id": 1, "created_at": str(sec)}]}}
        out = tc._parse_review_api_body(body)
        self.assertTrue(out[0]["created"].startswith("2026-01-01"))

    def test_review_ms_timestamp_converted(self):
        # ts > 1e12 → 毫秒转秒 (382)
        ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
        body = {"data": {"reviews": [{"content": "x", "id": 1, "created_time": ms}]}}
        out = tc._parse_review_api_body(body)
        self.assertTrue(out[0]["created"].startswith("2026-01-01"))


if __name__ == "__main__":
    unittest.main()
