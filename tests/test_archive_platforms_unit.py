"""archive_platforms 纯逻辑单测 — dedup / 日期分桶 / 合并 / 归档写入 / argparse。

网络无涉；所有 IO 走 tmp 目录，monkeypatch 模块级 ARCHIVE_DIR / OUTPUT 路径，
绝不触碰真实 projects/news/data/platforms。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_platforms as ap  # noqa: E402


class TestItemKey(unittest.TestCase):
    def test_url_preferred(self):
        self.assertEqual(ap.item_key({"url": " https://x/1 ", "title": "T"}), "https://x/1")

    def test_fallback_composite_when_no_url(self):
        k = ap.item_key({"title": "T", "time": "2026-01-01", "author": "a"})
        self.assertEqual(k, "T|2026-01-01|a")

    def test_empty_url_falls_back(self):
        k = ap.item_key({"url": "   ", "title": "T"})
        self.assertEqual(k, "T||")

    def test_weixin_ignores_rotating_sogou_url(self):
        """搜狗中转链每次请求重签，同一篇公告必须仍判为同一条。

        回归实测：2026-08-07 单日档里「运营主体变更通知」攒了 114 份、
        「第4届N厨杯」144 份，114 个 URL 无一相同（连 url 参数本身都轮换）。
        """
        base = {"source": "weixin", "title": "[微信] 关于《忘却前夜》运营主体变更的通知",
                "time": "2026-08-07T03:11:00+00:00", "author": ""}
        a = ap.item_key({**base, "url": "/link?url=dn9a_AAA&type=2&token=0ED020BE"})
        b = ap.item_key({**base, "url": "/link?url=dn9a_BBB&type=2&token=9FC431AA"})
        self.assertEqual(a, b)

    def test_stable_url_source_still_keys_on_url(self):
        """只有中转链源改键；URL 稳定的源不受影响（同标题不同贴仍是两条）。"""
        a = ap.item_key({"source": "reddit", "title": "T", "url": "https://x/1"})
        b = ap.item_key({"source": "reddit", "title": "T", "url": "https://x/2"})
        self.assertNotEqual(a, b)

    def test_approximate_time_excluded_from_key(self):
        """时间是 now() 兜底出来的就不能进键，否则每轮又是一个新身份。"""
        base = {"source": "weixin", "title": "T", "author": "a", "time_is_approximate": True}
        a = ap.item_key({**base, "time": "2026-08-07T01:00:00+00:00"})
        b = ap.item_key({**base, "time": "2026-08-07T04:00:00+00:00"})
        self.assertEqual(a, b)


class TestItemDateUtc8(unittest.TestCase):
    def test_no_time_uses_fallback(self):
        self.assertEqual(ap.item_date_utc8({}, "2026-05-05"), "2026-05-05")

    def test_utc_shifted_to_utc8(self):
        # 2026-04-13T20:00Z + 8h => 2026-04-14
        out = ap.item_date_utc8({"time": "2026-04-13T20:00:00+00:00"}, "fb")
        self.assertEqual(out, "2026-04-14")

    def test_naive_treated_as_utc(self):
        out = ap.item_date_utc8({"time": "2026-04-13T20:00:00"}, "fb")
        self.assertEqual(out, "2026-04-14")

    def test_bad_time_falls_back(self):
        self.assertEqual(ap.item_date_utc8({"time": "garbage"}, "fb"), "fb")


class TestMergeItems(unittest.TestCase):
    def test_dedup_and_sort_by_engagement(self):
        existing = [{"url": "u1", "engagement": 5}]
        new = [{"url": "u1", "engagement": 99},  # dup, dropped
               {"url": "u2", "engagement": 50},
               {"url": "u3", "engagement": 1}]
        merged = ap.merge_items(existing, new)
        self.assertEqual([m["url"] for m in merged], ["u2", "u1", "u3"])

    def test_dedup_within_existing(self):
        existing = [{"url": "u1"}, {"url": "u1"}]
        merged = ap.merge_items(existing, [])
        self.assertEqual(len(merged), 1)

    def test_missing_engagement_defaults_zero(self):
        merged = ap.merge_items([], [{"url": "u1"}])
        self.assertEqual(merged[0].get("engagement", 0), 0)


class TestLoadNews(unittest.TestCase):
    def test_returns_empty_when_neither_file(self, ):
        with mock.patch.object(ap, "RAW_NEWS", Path("/nonexistent/raw.json")), \
                mock.patch.object(ap, "INPUT_NEWS", Path("/nonexistent/news.json")):
            self.assertEqual(ap.load_news(), [])

    def test_reads_raw_when_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "raw.json"
            raw.write_text(json.dumps({"news": [{"url": "u1"}]}), encoding="utf-8")
            with mock.patch.object(ap, "RAW_NEWS", raw), \
                    mock.patch.object(ap, "INPUT_NEWS", Path(d) / "news.json"):
                self.assertEqual(ap.load_news(), [{"url": "u1"}])

    def test_falls_back_to_input_news(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            news = Path(d) / "news.json"
            news.write_text(json.dumps({"news": [{"url": "n1"}]}), encoding="utf-8")
            with mock.patch.object(ap, "RAW_NEWS", Path(d) / "raw.json"), \
                    mock.patch.object(ap, "INPUT_NEWS", news):
                self.assertEqual(ap.load_news(), [{"url": "n1"}])


class TestArchiveIO(unittest.TestCase):
    def _patch_dir(self, d):
        return mock.patch.object(ap, "ARCHIVE_DIR", Path(d))

    def test_load_existing_missing_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            self.assertEqual(ap.load_existing_archive("steam", None, None, "2026-01-01"), {})

    def test_write_then_load_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            n = ap.write_archive("steam", None, None, "2026-04-14", [{"url": "u1", "engagement": 3}])
            self.assertEqual(n, 1)
            loaded = ap.load_existing_archive("steam", None, None, "2026-04-14")
            self.assertEqual(loaded["item_count"], 1)
            self.assertEqual(loaded["source"], "steam")
            self.assertEqual(loaded["date"], "2026-04-14")

    def test_write_empty_returns_zero_no_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            self.assertEqual(ap.write_archive("steam", None, None, "2026-04-14", []), 0)
            self.assertFalse((Path(d) / "steam").exists())

    def test_write_merges_existing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            ap.write_archive("reddit", None, None, "2026-04-14", [{"url": "u1"}])
            n = ap.write_archive("reddit", None, None, "2026-04-14", [{"url": "u2"}])
            self.assertEqual(n, 2)


class TestArchivePathLayering(unittest.TestCase):
    """甲方案（2026-06-21）：region/subtype 字段 → 子目录分层，无字段回落扁平。"""

    def _patch_dir(self, d):
        return mock.patch.object(ap, "ARCHIVE_DIR", Path(d))

    def test_path_flat_fallback_no_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            p = ap.archive_path("steam", None, None, "2026-01-01")
            self.assertEqual(p, Path(d) / "steam" / "2026-01-01.json")

    def test_path_layered_region_and_subtype(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            p = ap.archive_path("steam", "jp", "review", "2026-01-01")
            self.assertEqual(p, Path(d) / "steam" / "jp" / "review" / "2026-01-01.json")

    def test_path_subtype_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            p = ap.archive_path("youtube", None, "comments", "2026-01-01")
            self.assertEqual(p, Path(d) / "youtube" / "comments" / "2026-01-01.json")

    def test_write_layered_roundtrip_with_meta(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            n = ap.write_archive("steam", "jp", "review", "2026-04-14", [{"url": "u1"}])
            self.assertEqual(n, 1)
            self.assertTrue((Path(d) / "steam" / "jp" / "review" / "2026-04-14.json").exists())
            loaded = ap.load_existing_archive("steam", "jp", "review", "2026-04-14")
            self.assertEqual(loaded["region"], "jp")
            self.assertEqual(loaded["content_subtype"], "review")

    def test_flat_and_layered_dont_collide(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, self._patch_dir(d):
            ap.write_archive("steam", None, None, "2026-04-14", [{"url": "flat"}])
            ap.write_archive("steam", "jp", "review", "2026-04-14", [{"url": "jp"}])
            flat = ap.load_existing_archive("steam", None, None, "2026-04-14")
            jp = ap.load_existing_archive("steam", "jp", "review", "2026-04-14")
            self.assertEqual(flat["items"][0]["url"], "flat")
            self.assertEqual(jp["items"][0]["url"], "jp")


class TestArchiveAll(unittest.TestCase):
    def test_buckets_by_source_and_date_skips_discord(self):
        import tempfile
        news = [
            {"source": "steam", "url": "s1", "time": "2026-04-13T20:00:00+00:00"},
            {"source": "discord", "url": "d1", "time": "2026-04-13T20:00:00+00:00"},
            {"source": "reddit", "url": "r1", "time": "2026-04-13T20:00:00+00:00"},
        ]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(ap, "load_news", return_value=news):
            totals = ap.archive_all(target_date=None, fallback_date="2026-01-01")
        self.assertEqual(totals.get("steam"), 1)
        self.assertEqual(totals.get("reddit"), 1)
        self.assertNotIn("discord", totals)

    def test_official_folds_to_steam_with_subtype(self):
        # 甲方案决策项⑦：official 官方公告归档折叠到 steam/<区服>/news（输出层 official-latest 不动）
        import tempfile
        news = [
            {"source": "official", "url": "n1", "time": "2026-04-13T20:00:00+00:00",
             "region": "jp", "archive_subtype": "news"},
            {"source": "steam_discussion", "url": "g1", "time": "2026-04-13T20:00:00+00:00",
             "region": "global", "archive_subtype": "discussion"},
        ]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(ap, "load_news", return_value=news):
            totals = ap.archive_all(target_date=None, fallback_date="2026-01-01")
            # 两个 steam 家族源都折叠到 steam 平台段，不再各自成桶
            self.assertEqual(totals.get("steam"), 2)
            self.assertNotIn("official", totals)
            self.assertNotIn("steam_discussion", totals)
            # 落点按 region/subtype 分层（断言须在 tempdir 清理前，即 with 块内）
            self.assertTrue((Path(d) / "steam" / "jp" / "news" / "2026-04-14.json").exists())
            self.assertTrue((Path(d) / "steam" / "global" / "discussion" / "2026-04-14.json").exists())

    def test_target_date_filters(self):
        import tempfile
        news = [
            {"source": "steam", "url": "s1", "time": "2026-04-13T20:00:00+00:00"},  # bucket 04-14
            {"source": "steam", "url": "s2", "time": "2026-01-01T00:00:00+00:00"},  # bucket 01-01
        ]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(ap, "load_news", return_value=news):
            totals = ap.archive_all(target_date="2026-04-14", fallback_date="x")
        self.assertEqual(totals.get("steam"), 1)

    def test_empty_news_yields_empty_totals(self):
        with mock.patch.object(ap, "load_news", return_value=[]):
            self.assertEqual(dict(ap.archive_all(None, "2026-01-01")), {})

    def test_buckets_by_region_and_subtype(self):
        """甲方案：item 带 region/archive_subtype 字段 → 分桶到 平台/区服/类型/ 子目录。"""
        import tempfile
        news = [
            {"source": "steam", "url": "g1", "time": "2026-04-13T20:00:00+00:00",
             "region": "global", "archive_subtype": "review"},
            {"source": "steam", "url": "j1", "time": "2026-04-13T20:00:00+00:00",
             "region": "jp", "archive_subtype": "review"},
            {"source": "steam", "url": "n1", "time": "2026-04-13T20:00:00+00:00",
             "region": "global", "archive_subtype": "news"},
        ]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ap, "ARCHIVE_DIR", Path(d)), \
                mock.patch.object(ap, "load_news", return_value=news):
            ap.archive_all(target_date=None, fallback_date="2026-01-01")
            self.assertTrue((Path(d) / "steam" / "global" / "review" / "2026-04-14.json").exists())
            self.assertTrue((Path(d) / "steam" / "jp" / "review" / "2026-04-14.json").exists())
            self.assertTrue((Path(d) / "steam" / "global" / "news" / "2026-04-14.json").exists())


class TestShowStatsAndMain(unittest.TestCase):
    def test_show_stats_runs_with_archives(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            adir = Path(d) / "steam"
            adir.mkdir(parents=True)
            (adir / "2026-04-14.json").write_text(
                json.dumps({"item_count": 3}), encoding="utf-8")
            with mock.patch.object(ap, "ARCHIVE_DIR", Path(d)), \
                    mock.patch.object(ap, "_REPO_ROOT", Path(d)):
                ap.show_stats()  # must not raise

    def test_show_stats_handles_no_archives(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ap, "ARCHIVE_DIR", Path(d) / "missing"), \
                    mock.patch.object(ap, "_REPO_ROOT", Path(d)):
                ap.show_stats()

    def test_main_stats_branch(self):
        with mock.patch.object(sys, "argv", ["prog", "--stats"]), \
                mock.patch.object(ap, "show_stats") as ss:
            ap.main()
        ss.assert_called_once()

    def test_main_archive_branch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["prog", "--date", "2026-04-14"]), \
                    mock.patch.object(ap, "ARCHIVE_DIR", Path(d)), \
                    mock.patch.object(ap, "archive_all", return_value={"steam": 2}) as aa:
                ap.main()
            aa.assert_called_once()

    def test_main_archive_empty_totals(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["prog"]), \
                    mock.patch.object(ap, "ARCHIVE_DIR", Path(d)), \
                    mock.patch.object(ap, "archive_all", return_value={}):
                ap.main()


if __name__ == "__main__":
    unittest.main()
