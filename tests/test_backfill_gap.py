"""backfill_gap 纯逻辑 + 各平台回溯函数（网络全打桩）单测。

requests / global_collectors._get / subprocess (curl) 一律 mock；归档写入
monkeypatch ARCHIVE_DIR 到 tmp 目录，绝不污染真实 Community 归档、绝不触网。
"""

import json
import tempfile
import unittest
from datetime import datetime, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import backfill_gap  # noqa: E402


class TestGapBound(unittest.TestCase):
    def test_missing_env_uses_default(self):
        default = datetime(2026, 1, 1, tzinfo=UTC)
        with mock.patch.dict(backfill_gap.os.environ, {}, clear=True):
            self.assertEqual(backfill_gap._gap_bound("X", default, False), default)

    def test_valid_start_of_day(self):
        with mock.patch.dict(backfill_gap.os.environ, {"X": "2026-03-15"}):
            out = backfill_gap._gap_bound("X", datetime(2026, 1, 1, tzinfo=UTC), False)
        self.assertEqual((out.year, out.month, out.day, out.hour), (2026, 3, 15, 0))

    def test_valid_end_of_day(self):
        with mock.patch.dict(backfill_gap.os.environ, {"X": "2026-03-15"}):
            out = backfill_gap._gap_bound("X", datetime(2026, 1, 1, tzinfo=UTC), True)
        self.assertEqual((out.hour, out.minute, out.second), (23, 59, 59))

    def test_invalid_falls_back_to_default(self):
        default = datetime(2026, 1, 1, tzinfo=UTC)
        with mock.patch.dict(backfill_gap.os.environ, {"X": "not-a-date"}):
            self.assertEqual(backfill_gap._gap_bound("X", default, False), default)


class TestArchiveItems(unittest.TestCase):
    def _patch_dir(self, d):
        return mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d))

    def test_buckets_by_utc8_date(self):
        with tempfile.TemporaryDirectory() as d:
            with self._patch_dir(d):
                # 2026-04-13T20:00Z + 8h => 2026-04-14 bucket
                backfill_gap._archive_items("reddit", [
                    {"url": "u1", "time": "2026-04-13T20:00:00+00:00"},
                ])
                out = Path(d) / "reddit" / "2026-04-14.json"
                self.assertTrue(out.exists())
                data = json.loads(out.read_text())
                self.assertEqual(data["item_count"], 1)

    def test_items_without_time_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            with self._patch_dir(d):
                backfill_gap._archive_items("reddit", [{"url": "u1"}])
                self.assertFalse((Path(d) / "reddit").exists())

    def test_dedup_against_existing_urls(self):
        with tempfile.TemporaryDirectory() as d:
            with self._patch_dir(d):
                pdir = Path(d) / "reddit"
                pdir.mkdir(parents=True)
                (pdir / "2026-04-14.json").write_text(json.dumps({
                    "items": [{"url": "u1"}]
                }), encoding="utf-8")
                backfill_gap._archive_items("reddit", [
                    {"url": "u1", "time": "2026-04-13T20:00:00+00:00"},  # dup
                    {"url": "u2", "time": "2026-04-13T20:00:00+00:00"},  # new
                ])
                data = json.loads((pdir / "2026-04-14.json").read_text())
                urls = {i["url"] for i in data["items"]}
        self.assertEqual(urls, {"u1", "u2"})

    def test_naive_time_treated_as_utc(self):
        with tempfile.TemporaryDirectory() as d:
            with self._patch_dir(d):
                backfill_gap._archive_items("reddit", [
                    {"url": "u1", "time": "2026-04-13T20:00:00"},  # naive
                ])
                self.assertTrue((Path(d) / "reddit" / "2026-04-14.json").exists())

    def test_bad_time_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            with self._patch_dir(d):
                backfill_gap._archive_items("reddit", [{"url": "u1", "time": "garbage"}])
                self.assertFalse((Path(d) / "reddit").exists())


class TestCleanupEmptyPlaceholders(unittest.TestCase):
    def test_removes_empty_repaired_placeholders(self):
        with tempfile.TemporaryDirectory() as d:
            sdir = Path(d) / "reddit"
            sdir.mkdir(parents=True)
            (sdir / "2026-04-15.json").write_text(json.dumps(
                {"_gap_repaired": True, "items": []}), encoding="utf-8")
            (sdir / "2026-04-16.json").write_text(json.dumps(
                {"_gap_repaired": True, "items": [{"url": "x"}]}), encoding="utf-8")
            (sdir / "2026-04-17.json").write_text(json.dumps(
                {"items": []}), encoding="utf-8")  # not repaired flag
            with mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)):
                removed = backfill_gap.cleanup_empty_placeholders()
            self.assertEqual(removed, 1)
            self.assertFalse((sdir / "2026-04-15.json").exists())
            self.assertTrue((sdir / "2026-04-16.json").exists())
            self.assertTrue((sdir / "2026-04-17.json").exists())

    def test_ignores_non_directories(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "loose.txt").write_text("x")
            with mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)):
                self.assertEqual(backfill_gap.cleanup_empty_placeholders(), 0)


def _reddit_resp(children, after=None, status=200):
    r = mock.MagicMock()
    r.status_code = status
    r.json.return_value = {"data": {"children": children, "after": after}}
    return r


class TestBackfillReddit(unittest.TestCase):
    def _post(self, ts, title="t", permalink="/p", score=10):
        return {"data": {"created_utc": ts, "title": title, "permalink": permalink,
                         "score": score, "num_comments": 1, "author": "a", "selftext": "x"}}

    def test_collects_items_in_gap(self):
        in_gap = backfill_gap.GAP_START.timestamp() + 3600
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            with mock.patch.object(_req, "get",
                                   return_value=_reddit_resp([self._post(in_gap)], after=None)):
                n = backfill_gap.backfill_reddit()
        self.assertEqual(n, 2)  # two subreddits each yield 1

    def test_http_error_breaks(self):
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            with mock.patch.object(_req, "get", return_value=_reddit_resp([], status=500)):
                n = backfill_gap.backfill_reddit()
        self.assertEqual(n, 0)

    def test_items_before_gap_break_pagination(self):
        before_gap = backfill_gap.GAP_START.timestamp() - 86400
        with mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import requests as _req
            with mock.patch.object(_req, "get",
                                   return_value=_reddit_resp([self._post(before_gap)], after="next")):
                n = backfill_gap.backfill_reddit()
        self.assertEqual(n, 0)


class TestBackfillYoutube(unittest.TestCase):
    def test_no_api_key_skips(self):
        with mock.patch.dict(backfill_gap.os.environ, {}, clear=True):
            self.assertEqual(backfill_gap.backfill_youtube(), 0)

    def test_collects_with_api_key(self):
        in_gap = backfill_gap.GAP_START.strftime("%Y-%m-%dT%H:%M:%SZ")
        search_resp = mock.MagicMock()
        search_resp.json.return_value = {"items": [
            {"id": {"videoId": "v1"}, "snippet": {"title": "T", "description": "d",
                                                  "publishedAt": in_gap, "channelTitle": "c"}}
        ]}
        stats_resp = mock.MagicMock()
        stats_resp.json.return_value = {"items": [
            {"id": "v1", "statistics": {"viewCount": "100", "likeCount": "5"}}
        ]}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(backfill_gap.os.environ, {"YOUTUBE_API_KEY": "k"}), \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)):
            import global_collectors as gc
            with mock.patch.object(gc, "_get", side_effect=[search_resp, stats_resp] * 3):
                n = backfill_gap.backfill_youtube()
        self.assertEqual(n, 3)  # three keywords, one video each

    def test_exception_redacted_and_continues(self):
        with mock.patch.dict(backfill_gap.os.environ, {"YOUTUBE_API_KEY": "secret"}):
            import global_collectors as gc
            with mock.patch.object(gc, "_get", side_effect=RuntimeError("key=secret")), \
                    mock.patch.object(backfill_gap.news_common, "redact_secrets",
                                      return_value="[redacted]") as red:
                n = backfill_gap.backfill_youtube()
        self.assertEqual(n, 0)
        self.assertTrue(red.called)


class TestBackfillBilibili(unittest.TestCase):
    def test_collects_in_gap(self):
        pubdate = int(backfill_gap.GAP_START.timestamp() + 3600)
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"result": [
            {"pubdate": pubdate, "title": "<em class=\"keyword\">M</em>", "description": "d",
             "arcurl": "url", "play": 20000, "favorites": 1, "author": "a"}
        ]}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import global_collectors as gc
            # first page returns results, second page empty to break
            empty = mock.MagicMock()
            empty.json.return_value = {"data": {"result": []}}
            with mock.patch.object(gc, "_get", side_effect=[resp, empty] * 20):
                n = backfill_gap.backfill_bilibili()
        self.assertGreaterEqual(n, 1)


class TestBackfillWeibo(unittest.TestCase):
    def test_collects_in_gap(self):
        iso = backfill_gap.GAP_START.replace(hour=12).isoformat()
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"cards": [
            {"mblog": {"created_at": "ignored", "text": "<a>hi</a>", "id": "1",
                       "reposts_count": 1, "comments_count": 2, "attitudes_count": 3,
                       "user": {"screen_name": "u"}}}
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
                    mock.patch.object(gc, "_parse_weibo_time", return_value=(iso, None)):
                n = backfill_gap.backfill_weibo()
        self.assertGreaterEqual(n, 1)


class TestBackfillSteam(unittest.TestCase):
    def test_collects_in_gap(self):
        ts = int(backfill_gap.GAP_START.timestamp() + 3600)
        body = json.dumps({"reviews": [
            {"timestamp_created": ts, "language": "en", "voted_up": True,
             "review": "good game", "author": {"steamid": "s1"}, "votes_up": 20}
        ], "cursor": "next"})
        result = mock.MagicMock()
        result.returncode = 0
        result.stdout = body
        empty = mock.MagicMock()
        empty.returncode = 0
        empty.stdout = json.dumps({"reviews": [], "cursor": "next2"})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(backfill_gap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(backfill_gap, "time") as t:
            t.sleep = lambda *_: None
            import subprocess as sp
            with mock.patch.object(sp, "run", side_effect=[result, empty]):
                n = backfill_gap.backfill_steam_reviews()
        self.assertEqual(n, 1)

    def test_curl_failure_breaks(self):
        result = mock.MagicMock()
        result.returncode = 1
        result.stdout = ""
        import subprocess as sp
        with mock.patch.object(sp, "run", return_value=result):
            n = backfill_gap.backfill_steam_reviews()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
