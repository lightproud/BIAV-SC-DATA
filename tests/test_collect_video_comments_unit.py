"""collect_video_comments 纯逻辑单测 — _get / discover / 分页增量 / main 编排。

urllib 全打桩；youtube archive glob 走 tmp；YOUTUBE_API_KEY env 受控。
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import collect_video_comments as cvc  # noqa: E402


class TestGet(unittest.TestCase):
    def test_builds_url_and_parses(self):
        payload = mock.MagicMock()
        cm = mock.MagicMock()
        cm.__enter__.return_value = payload
        cm.__exit__.return_value = False
        with mock.patch.object(cvc.urllib.request, "urlopen", return_value=cm), \
                mock.patch.object(cvc.json, "load", return_value={"items": []}):
            out = cvc._get("search", {"q": "Morimens", "key": "k"})
        self.assertEqual(out, {"items": []})


class TestDiscoverVideos(unittest.TestCase):
    def test_search_plus_archive(self):
        search_data = {"items": [
            {"id": {"videoId": "v1"}, "snippet": {"title": "T1", "channelTitle": "C1"}}]}
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ydir = Path(d) / "platforms" / "youtube"
            ydir.mkdir(parents=True)
            (ydir / "2026-06-01.json").write_text(
                json.dumps([{"url": "https://youtube.com/watch?v=v2xxxxxxxxx",
                             "title": "AT", "author": "AA"}]), encoding="utf-8")
            with mock.patch.object(cvc, "_get", return_value=search_data), \
                    mock.patch.object(cvc.glob, "glob",
                                      return_value=[str(ydir / "2026-06-01.json")]):
                vids = cvc.discover_videos("key")
        self.assertIn("v1", vids)
        self.assertIn("v2xxxxxxxxx", vids)
        self.assertEqual(vids["v1"], ("T1", "C1"))

    def test_search_exception_continues(self):
        with mock.patch.object(cvc, "_get", side_effect=RuntimeError("boom")), \
                mock.patch.object(cvc.glob, "glob", return_value=[]):
            vids = cvc.discover_videos("key")
        self.assertEqual(vids, {})

    def test_archive_bad_json_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with mock.patch.object(cvc, "_get", return_value={"items": []}), \
                    mock.patch.object(cvc.glob, "glob", return_value=[str(bad)]):
                vids = cvc.discover_videos("key")
        self.assertEqual(vids, {})


class TestFetchVideoComments(unittest.TestCase):
    def _thread(self, cid, text="hi", likes=3):
        return {"id": cid, "snippet": {"topLevelComment": {"snippet": {
            "authorDisplayName": "a", "textDisplay": text, "likeCount": likes,
            "publishedAt": "2026-06-01T00:00:00Z"}}}}

    def test_collects_new_then_stops_no_token(self):
        data = {"items": [self._thread("c1"), self._thread("c2")], "nextPageToken": None}
        with mock.patch.object(cvc, "_get", return_value=data):
            rows, exhausted = cvc.fetch_video_comments("k", "v1", set(), max_pages=8)
        self.assertEqual(len(rows), 2)
        self.assertTrue(exhausted)

    def test_full_page_known_stops(self):
        known = {"c1"}
        data = {"items": [self._thread("c1")], "nextPageToken": "next"}
        with mock.patch.object(cvc, "_get", return_value=data):
            rows, exhausted = cvc.fetch_video_comments("k", "v1", known, max_pages=8)
        self.assertEqual(rows, [])
        self.assertTrue(exhausted)

    def test_http_error_marks_exhausted(self):
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(cvc, "_get", side_effect=err):
            rows, exhausted = cvc.fetch_video_comments("k", "v1", set(), max_pages=8)
        self.assertEqual(rows, [])
        self.assertTrue(exhausted)

    def test_hits_max_pages_not_exhausted(self):
        # every page yields a NEW comment + a nextPageToken → never natural-stop
        counter = {"n": 0}

        def make(*a, **k):
            counter["n"] += 1
            return {"items": [self._thread(f"c{counter['n']}")], "nextPageToken": "tok"}
        with mock.patch.object(cvc, "_get", side_effect=make):
            rows, exhausted = cvc.fetch_video_comments("k", "v1", set(), max_pages=2)
        self.assertEqual(len(rows), 2)
        self.assertFalse(exhausted)


class TestMain(unittest.TestCase):
    def test_no_key_creates_dir_and_returns(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = str(Path(d) / "yc")
            with mock.patch.object(cvc, "DEST", dest), \
                    mock.patch.object(sys, "argv", ["prog", "--date", "2026-06-01"]), \
                    mock.patch.dict(cvc.os.environ, {}, clear=True):
                cvc.main()
            self.assertTrue(Path(dest).exists())

    def test_full_run_with_key(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = str(Path(d) / "yc")
            row = {"id": "c1", "video_id": "v1", "author": "a", "text": "t",
                   "likes": 5, "published": "p", "fetched_at": "f"}
            with mock.patch.object(cvc, "DEST", dest), \
                    mock.patch.object(sys, "argv", ["prog", "--date", "2026-06-01"]), \
                    mock.patch.dict(cvc.os.environ, {"YOUTUBE_API_KEY": "k"}, clear=True), \
                    mock.patch.object(cvc, "discover_videos", return_value={"v1": ("T", "C")}), \
                    mock.patch.object(cvc, "fetch_video_comments", return_value=([row], True)):
                cvc.main()
            snap = json.loads(Path(dest, "2026-06-01.json").read_text())
            self.assertEqual(len(snap), 1)
            self.assertEqual(snap[0]["video_title"], "T")
            self.assertTrue(Path(dest, "comments.jsonl").exists())
            self.assertTrue(Path(dest, "state.json").exists())

    def test_loads_existing_store_and_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "yc"
            dest.mkdir()
            (dest / "comments.jsonl").write_text(
                json.dumps({"id": "old1"}) + "\nnot-json\n", encoding="utf-8")
            (dest / "state.json").write_text(json.dumps({"v0": {"exhausted": True}}), encoding="utf-8")
            with mock.patch.object(cvc, "DEST", str(dest)), \
                    mock.patch.object(sys, "argv", ["prog", "--date", "2026-06-02"]), \
                    mock.patch.dict(cvc.os.environ, {"YOUTUBE_API_KEY": "k"}, clear=True), \
                    mock.patch.object(cvc, "discover_videos", return_value={}), \
                    mock.patch.object(cvc, "fetch_video_comments", return_value=([], True)):
                cvc.main()
            # state.json preserved/rewritten
            state = json.loads((dest / "state.json").read_text())
            self.assertIn("v0", state)


class TestSnapshotDateByPublish(unittest.TestCase):
    """日档按**发布日**分档（守密人 2026-08-08 裁定），--date 只作回落标签。"""

    def test_offset_applied_exactly_once(self):
        # 两侧夹逼，把「北京偏移」钉死为恰好一次：
        #   16:30Z 北京已跨日 → 少算偏移会得 08-05；
        #   10:00Z 北京仍当日 → 多算一遍偏移会得 08-06。
        self.assertEqual(cvc.snapshot_date({"published": "2026-08-05T16:30:00Z"}, "x"),
                         "2026-08-06")
        self.assertEqual(cvc.snapshot_date({"published": "2026-08-05T10:00:00Z"}, "x"),
                         "2026-08-05")

    def test_unparsable_or_missing_falls_back_to_label(self):
        for row in ({"published": "p"}, {"published": ""}, {}):
            self.assertEqual(cvc.snapshot_date(row, "2026-06-01"), "2026-06-01")

    def _run_main(self, dest, rows, date_label):
        with mock.patch.object(cvc, "DEST", dest), \
                mock.patch.object(sys, "argv", ["prog", "--date", date_label]), \
                mock.patch.dict(cvc.os.environ, {"YOUTUBE_API_KEY": "k"}, clear=True), \
                mock.patch.object(cvc, "discover_videos", return_value={"v1": ("T", "C")}), \
                mock.patch.object(cvc, "fetch_video_comments", return_value=(rows, True)):
            cvc.main()

    def test_one_run_scatters_into_own_publish_day_files(self):
        # 断更补采的核心诉求：一轮采回的跨日积压不再全挤进 --date 那一个档。
        import tempfile
        rows = [
            {"id": "c1", "likes": 1, "published": "2026-07-28T03:00:00Z"},
            {"id": "c2", "likes": 2, "published": "2026-08-01T03:00:00Z"},
            {"id": "c3", "likes": 3, "published": "2026-08-01T04:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as d:
            dest = str(Path(d) / "yc")
            self._run_main(dest, rows, "2026-08-07")
            self.assertEqual(len(json.loads(Path(dest, "2026-07-28.json").read_text())), 1)
            self.assertEqual(len(json.loads(Path(dest, "2026-08-01.json").read_text())), 2)
            # 标签日自身不该凭空多出一个档
            self.assertFalse(Path(dest, "2026-08-07.json").exists())

    def test_empty_run_writes_no_file(self):
        # 空组不落笔：本轮无新评论时不得造 `[]` 空档冒充「那天没评论」。
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = str(Path(d) / "yc")
            self._run_main(dest, [], "2026-08-07")
            self.assertFalse(Path(dest, "2026-08-07.json").exists())

    def test_merges_into_existing_day_without_clobbering(self):
        # #875 幂等防线在新结构下仍在：并轨既有日档，不覆盖。
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "yc"
            dest.mkdir()
            (dest / "2026-08-01.json").write_text(
                json.dumps([{"id": "old", "likes": 99}]), encoding="utf-8")
            self._run_main(str(dest), [{"id": "c1", "likes": 1,
                                        "published": "2026-08-01T03:00:00Z"}], "2026-08-07")
            snap = json.loads((dest / "2026-08-01.json").read_text())
            self.assertEqual([r["id"] for r in snap], ["old", "c1"])   # 按 likes 降序


if __name__ == "__main__":
    unittest.main()
