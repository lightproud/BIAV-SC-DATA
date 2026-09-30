"""backfill_gap 补充覆盖：异常分支 / 边界过滤 / 分页短路 / main 编排。

补 tests/test_backfill_gap.py 未触及的错误/边界路径。网络全打桩，归档写入
monkeypatch ARCHIVE_DIR 到 tmp，绝不触网、绝不污染真实 Community 归档。
"""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import backfill_gap  # noqa: E402


class TestArchiveItemsEdge(unittest.TestCase):
    def _patch_dir(self, d):
        return mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d))

    def test_gap_repaired_placeholder_existing_cleared(self):
        # 已有文件标记 _gap_repaired 且 items 为空 → existing 被清空，新条目正常合并 (81-83)
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d) / "reddit"
            pdir.mkdir(parents=True)
            (pdir / "2026-04-14.json").write_text(json.dumps(
                {"_gap_repaired": True, "items": []}), encoding="utf-8")
            with self._patch_dir(d):
                backfill_gap._archive_items("reddit", [
                    {"url": "u1", "time": "2026-04-13T20:00:00+00:00"},
                ])
            data = json.loads((pdir / "2026-04-14.json").read_text())
        self.assertEqual(data["item_count"], 1)

    def test_existing_file_bad_json_swallowed(self):
        # 已有文件 JSON 损坏 → except 吞掉，仍写入新条目 (82-83)
        with tempfile.TemporaryDirectory() as d:
            pdir = Path(d) / "reddit"
            pdir.mkdir(parents=True)
            (pdir / "2026-04-14.json").write_text("{broken", encoding="utf-8")
            with self._patch_dir(d):
                backfill_gap._archive_items("reddit", [
                    {"url": "u1", "time": "2026-04-13T20:00:00+00:00"},
                ])
            data = json.loads((pdir / "2026-04-14.json").read_text())
        self.assertEqual(data["item_count"], 1)


def _reddit_resp(children, after=None, status=200):
    r = mock.MagicMock()
    r.status_code = status
    r.json.return_value = {"data": {"children": children, "after": after}}
    return r


class TestBackfillRedditEdge(unittest.TestCase):
    # backfill_reddit() archives collected in-gap posts via _archive_items, which
    # writes under ARCHIVE_DIR. Redirect it to a tmp dir for EVERY test in this
    # class so an in-gap collection (e.g. test_pagination_sleep_and_continue) can
    # never leak a real file into projects/news/data/platforms/ (test isolation).
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            backfill_gap, "ARCHIVE_DIR", Path(self._tmp.name))
        self._patch.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._patch.stop)

    def _post(self, ts, score=10):
        return {"data": {"created_utc": ts, "title": "t", "permalink": "/p",
                         "score": score, "num_comments": 1, "author": "a", "selftext": "x"}}

    def test_empty_posts_breaks(self):
        # posts 空 → break (128)
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            with mock.patch.object(_req, "get", return_value=_reddit_resp([], after="next")):
                n = backfill_gap.backfill_reddit()
        self.assertEqual(n, 0)

    def test_after_gap_post_skipped(self):
        # created > GAP_END → continue (139)；oldest 仍在 gap 内、有 after → 第二页空 break
        after_gap = backfill_gap.GAP_END.timestamp() + 86400
        page1 = _reddit_resp([self._post(after_gap)], after="cur")
        page2 = _reddit_resp([], after=None)
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            with mock.patch.object(_req, "get", side_effect=[page1, page2] * 2):
                n = backfill_gap.backfill_reddit()
        self.assertEqual(n, 0)

    def test_pagination_sleep_and_continue(self):
        # in-gap 帖子 + after 存在 → 走到 time.sleep(1) (164)，第二页空 break
        in_gap = backfill_gap.GAP_START.timestamp() + 3600
        page1 = _reddit_resp([self._post(in_gap)], after="cur")
        page2 = _reddit_resp([], after=None)
        slept = []
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *a: slept.append(a)
            import requests as _req
            with mock.patch.object(_req, "get", side_effect=[page1, page2] * 2):
                n = backfill_gap.backfill_reddit()
        self.assertEqual(n, 2)
        self.assertTrue(slept)

    def test_request_exception_breaks(self):
        # requests.get 抛异常 → except break (165-167)
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            with mock.patch.object(_req, "get", side_effect=RuntimeError("net")):
                n = backfill_gap.backfill_reddit()
        self.assertEqual(n, 0)


class TestBackfillBilibiliEdge(unittest.TestCase):
    def test_zero_pubdate_and_out_of_gap_skipped(self):
        # pubdate=0 continue (260)，pubdate 在 gap 外 continue (263)
        out_of_gap = int(backfill_gap.GAP_END.timestamp() + 86400)
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"result": [
            {"pubdate": 0, "title": "zero"},
            {"pubdate": out_of_gap, "title": "old", "play": 1, "favorites": 0},
        ]}}
        empty = mock.MagicMock()
        empty.json.return_value = {"data": {"result": []}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import global_collectors as gc
            with mock.patch.object(gc, "_get", side_effect=[resp, empty] * 20):
                n = backfill_gap.backfill_bilibili()
        self.assertEqual(n, 0)

    def test_exception_sleeps_and_breaks(self):
        # _get 抛异常 → except: time.sleep(5); break (280-283)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import global_collectors as gc
            with mock.patch.object(gc, "_get", side_effect=RuntimeError("412")):
                n = backfill_gap.backfill_bilibili()
        self.assertEqual(n, 0)


class TestBackfillWeiboEdge(unittest.TestCase):
    def test_cookie_header_and_filters(self):
        # 设置 WEIBO_COOKIE → 走 headers['Cookie'] (304)
        # 一卡缺 mblog (320)、一卡 created 空 (323)、一卡 dt 出界 (335)
        iso_out = (backfill_gap.GAP_END + timedelta(days=2)).isoformat()
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"cards": [
            {},  # 无 mblog
            {"mblog": {"created_at": ""}},  # created 空
            {"mblog": {"created_at": "out", "text": "x", "id": "9"}},  # 出界
        ]}}
        empty = mock.MagicMock()
        empty.json.return_value = {"data": {"cards": []}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(backfill_gap.os.environ, {"WEIBO_COOKIE": "SUB=abc"}), \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            import global_collectors as gc
            with mock.patch.object(_req, "get", side_effect=[resp, empty] * 20), \
                    mock.patch.object(gc, "_parse_weibo_time", return_value=(iso_out, None)):
                n = backfill_gap.backfill_weibo()
        self.assertEqual(n, 0)

    def test_parse_weibo_time_exception_fallback(self):
        # _parse_weibo_time 抛异常 → time_str = created 原值 (327-328)，
        # 该原值无法 fromisoformat → continue (336-337)
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"cards": [
            {"mblog": {"created_at": "garbage-time", "text": "x", "id": "1"}},
        ]}}
        empty = mock.MagicMock()
        empty.json.return_value = {"data": {"cards": []}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(backfill_gap.os.environ, {}, clear=True), \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            import global_collectors as gc
            with mock.patch.object(_req, "get", side_effect=[resp, empty] * 20), \
                    mock.patch.object(gc, "_parse_weibo_time", side_effect=ValueError("bad")):
                n = backfill_gap.backfill_weibo()
        self.assertEqual(n, 0)

    def test_naive_dt_in_gap_collected(self):
        # _parse_weibo_time 返回 naive ISO 且在 gap 内 → dt.replace(utc) (333) 并采集
        naive = backfill_gap.GAP_START.replace(hour=12, tzinfo=None).isoformat()
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"cards": [
            {"mblog": {"created_at": "x", "text": "<a>hi</a>", "id": "1",
                       "reposts_count": 1, "comments_count": 2, "attitudes_count": 3,
                       "user": {"screen_name": "u"}}},
        ]}}
        empty = mock.MagicMock()
        empty.json.return_value = {"data": {"cards": []}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(backfill_gap.os.environ, {}, clear=True), \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            import global_collectors as gc
            with mock.patch.object(_req, "get", side_effect=[resp, empty] * 20), \
                    mock.patch.object(gc, "_parse_weibo_time", return_value=(naive, None)):
                n = backfill_gap.backfill_weibo()
        self.assertGreaterEqual(n, 1)

    def test_request_exception_breaks(self):
        # requests.get 抛异常 → except break (356-358)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(backfill_gap.os.environ, {}, clear=True), \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            with mock.patch.object(_req, "get", side_effect=RuntimeError("net")):
                n = backfill_gap.backfill_weibo()
        self.assertEqual(n, 0)


class TestBackfillSteamEdge(unittest.TestCase):
    def _result(self, body, rc=0):
        r = mock.MagicMock()
        r.returncode = rc
        r.stdout = body
        return r

    def test_empty_body_breaks(self):
        # curl 返回空 body → break (392-393)
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import subprocess as sp
            with mock.patch.object(sp, "run", return_value=self._result("   ")):
                n = backfill_gap.backfill_steam_reviews()
        self.assertEqual(n, 0)

    def test_after_gap_review_then_old_breaks(self):
        # 第一条 created > GAP_END → continue (409)；第二条 < GAP_START 设 oldest →
        # 循环末 oldest < GAP_START → break (439)
        after_gap = int(backfill_gap.GAP_END.timestamp() + 86400)
        before_gap = int(backfill_gap.GAP_START.timestamp() - 86400)
        body = json.dumps({"reviews": [
            {"timestamp_created": after_gap, "review": "x", "author": {"steamid": "s"}},
            {"timestamp_created": before_gap, "review": "y", "author": {"steamid": "s2"}},
        ], "cursor": "next"})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import subprocess as sp
            with mock.patch.object(sp, "run", return_value=self._result(body)):
                n = backfill_gap.backfill_steam_reviews()
        self.assertEqual(n, 0)

    def test_no_next_cursor_breaks(self):
        # in-gap 条目 + cursor 与当前相同 → break (442)
        in_gap = int(backfill_gap.GAP_START.timestamp() + 3600)
        body = json.dumps({"reviews": [
            {"timestamp_created": in_gap, "review": "good", "voted_up": True,
             "author": {"steamid": "s1"}, "votes_up": 5},
        ], "cursor": "*"})  # 与初始 cursor '*' 相同 → break
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import subprocess as sp
            with mock.patch.object(sp, "run", return_value=self._result(body)):
                n = backfill_gap.backfill_steam_reviews()
        self.assertEqual(n, 1)

    def test_exception_swallowed(self):
        # json.loads 抛异常 → 顶层 except (446-447)
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import subprocess as sp
            with mock.patch.object(sp, "run", return_value=self._result("not json")):
                n = backfill_gap.backfill_steam_reviews()
        self.assertEqual(n, 0)


class TestCleanupEdge(unittest.TestCase):
    def test_bad_json_swallowed(self):
        # 占位文件 JSON 损坏 → except continue (471-472)
        with tempfile.TemporaryDirectory() as d:
            sdir = Path(d) / "reddit"
            sdir.mkdir(parents=True)
            (sdir / "2026-04-15.json").write_text("{broken", encoding="utf-8")
            with mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)):
                removed = backfill_gap.cleanup_empty_placeholders()
        self.assertEqual(removed, 0)


class TestMain(unittest.TestCase):
    def test_main_orchestrates_all(self):
        # main() 调用 cleanup + 5 个 backfill_*，全部打桩 (478-489)
        with mock.patch.object(backfill_gap, "cleanup_empty_placeholders", return_value=0) as cl, \
                mock.patch.object(backfill_gap, "backfill_reddit", return_value=1) as r, \
                mock.patch.object(backfill_gap, "backfill_youtube", return_value=2) as y, \
                mock.patch.object(backfill_gap, "backfill_bilibili", return_value=3) as b, \
                mock.patch.object(backfill_gap, "backfill_weibo", return_value=4) as w, \
                mock.patch.object(backfill_gap, "backfill_steam_reviews", return_value=5) as s:
            backfill_gap.main()
        for m in (cl, r, y, b, w, s):
            self.assertTrue(m.called)


if __name__ == "__main__":
    unittest.main()
