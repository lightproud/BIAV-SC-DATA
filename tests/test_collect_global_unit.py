"""collect_global coverage for the branches the existing failure-aggregation
test leaves: convert_item media/metadata, _is_recent, build_summary,
load_existing_news, the Playwright fallback + dormant-skip + auth-gated 0-item
paths inside run_zero_cost_collectors, and main()'s success / empty-run exits.

Hermetic: every collector mocked, all output writes stubbed, no network.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import collect_global as cg
import global_collectors
import news_common


# ── convert_item ─────────────────────────────────────────────────────────────

class TestConvertItem(unittest.TestCase):
    def test_source_mapped(self):
        out = cg.convert_item({"source": "bilibili", "title": "t"})
        self.assertEqual(out["source"], "bilibili")

    def test_steam_sources_are_not_remapped(self):
        """steam 三源各自直通（2026-08-22）。

        旧表把 steam → steam_review：AC 栈里 GC 只见得到评价，那时无碍；三源迁入 GC 后
        官方新闻会被改标成评价，archive_platforms 据 source 分桶，落档就进错桶。
        """
        for src in ("steam", "steam_review", "steam_discussion"):
            self.assertEqual(cg.convert_item({"source": src, "title": "t"})["source"], src)

    def test_unknown_source_passthrough(self):
        out = cg.convert_item({"source": "myst", "title": "t"})
        self.assertEqual(out["source"], "myst")

    def test_media_fields_preserved(self):
        out = cg.convert_item({"source": "pixiv", "media_url": "https://x.jpg"})
        self.assertEqual(out["media_url"], "https://x.jpg")
        self.assertEqual(out["content_type"], "image")

    def test_metadata_preserved(self):
        out = cg.convert_item({"source": "x", "metadata": {"plays": 5}})
        self.assertEqual(out["metadata"], {"plays": 5})

    def test_bad_metadata_ignored(self):
        out = cg.convert_item({"source": "x", "metadata": "notadict"})
        self.assertNotIn("metadata", out)

    def test_region_subtype_passthrough(self):
        # 甲方案：采集器标的 region/archive_subtype 必须透传给 archive 端分桶
        out = cg.convert_item({"source": "steam", "region": "jp", "archive_subtype": "review"})
        self.assertEqual(out["region"], "jp")
        self.assertEqual(out["archive_subtype"], "review")

    def test_region_subtype_absent_not_added(self):
        # 缺省不落字段 → archive_platforms 回落扁平，不带字段的源零破坏
        out = cg.convert_item({"source": "steam"})
        self.assertNotIn("region", out)
        self.assertNotIn("archive_subtype", out)


# ── _is_recent / build_summary / load_existing_news ──────────────────────────

class TestRecency(unittest.TestCase):
    def test_empty_time_false(self):
        self.assertFalse(cg._is_recent(""))

    def test_recent_true(self):
        now = datetime.now(UTC).isoformat()
        self.assertTrue(cg._is_recent(now))

    def test_old_false(self):
        old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        self.assertFalse(cg._is_recent(old))

    def test_naive_datetime_assumed_utc(self):
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        self.assertTrue(cg._is_recent(now))

    def test_bad_time_false(self):
        self.assertFalse(cg._is_recent("not-a-date"))

    def test_sparse_source_wider_window(self):
        # 20 days old: too old for default 24h, but within sparse 30d
        mid = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        sparse = next(iter(cg.SPARSE_SOURCES)) if cg.SPARSE_SOURCES else None
        if sparse:
            self.assertTrue(cg._is_recent(mid, sparse))


class TestBuildSummary(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(cg.build_summary([]), "")

    def test_joins_titles(self):
        items = [{"title": "Alpha"}, {"title": "Beta"}]
        out = cg.build_summary(items)
        self.assertIn("Alpha", out)
        self.assertTrue(out.endswith("。"))


# load_existing_news 已随 aggregator 全家退役（2026-08-22「采集 → 直接入湖」）：
# 它读的是 AC 栈先写下的 news.json，作为唯一入口后没有"上一段"的产物可并，
# existing 恒为空列表。原三例（缺文件 / 正常读 / 坏档回落空）随函数一并退役。


# ── run_zero_cost_collectors special branches ────────────────────────────────

def _item(title, url):
    return {"title": title, "url": url, "engagement": 1}


class TestRunZeroCostBranches(unittest.TestCase):
    """Drive the dormant-skip, playwright-fallback, and auth-gated 0-item
    paths that the existing test doesn't reach."""

    def _patch_all_empty(self, overrides):
        attr_by_name = {
            # 2026-08-22：AC 栈退役后 reddit / bilibili / taptap 回落 GC、steam 三源迁入 GC。
            # 本表漏登记 = 该采集器不被 mock = 用例真的出网（实测把 hermetic 用例拖成 55 秒）。
            # 漂移守卫见 test_fetcher_stub_map_covers_every_registered_collector。
            "Reddit": "fetch_reddit", "Bilibili": "fetch_bilibili",
            "TapTap": "fetch_taptap",
            "Steam News": "fetch_steam_news", "Steam Reviews": "fetch_steam_reviews",
            "Steam Discussions": "fetch_steam_discussions",
            "Weibo": "fetch_weibo", "App Store": "fetch_appstore_reviews",
            "Pixiv": "fetch_pixiv", "Note.com": "fetch_note_com",
            "Ruliweb": "fetch_ruliweb", "StopGame": "fetch_stopgame",
            "搜狗微信": "fetch_weixin", "YouTube": "fetch_youtube",
            "Bahamut": "fetch_bahamut", "Arca.live": "fetch_arca_live",
            "Google Play": "fetch_google_play",
        }
        patches = []
        for name, attr in attr_by_name.items():
            fn = overrides.get(name, list)
            patches.append(mock.patch.object(global_collectors, attr, fn))
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_dormant_source_skipped(self):
        # A tracker that marks every platform dormant → all skipped, no items.
        tracker = mock.MagicMock()
        tracker.should_skip_platform.return_value = True
        fake_dq = mock.MagicMock()
        fake_dq.SilentPlatformTracker.return_value = tracker
        with mock.patch.object(global_collectors, "_refresh_cutoff", return_value=None), \
                mock.patch.dict(sys.modules, {"data_quality": fake_dq,
                                              "playwright_collectors": None}):
            self._patch_all_empty({"Weibo": lambda: [_item("t", "https://t/1")]})
            items, core_failures = cg.run_zero_cost_collectors()
        self.assertEqual(items, [])
        self.assertEqual(core_failures, [])

    def test_playwright_fallback_on_empty(self):
        # Ruliweb returns [] via HTTP → playwright fallback yields items.
        # （原以 Arca.live 举例；该源 2026-08-16 摘除后已不在编排里）
        pw_mod = mock.MagicMock()
        pw_mod.fetch_ruliweb_playwright = lambda: [_item("pw", "https://pw/1")]
        with mock.patch.object(global_collectors, "_refresh_cutoff", return_value=None), \
                mock.patch.dict(sys.modules, {"data_quality": mock.MagicMock(
                    SilentPlatformTracker=mock.MagicMock(side_effect=Exception("off"))),
                    "playwright_collectors": pw_mod}):
            self._patch_all_empty({"Ruliweb": list})
            items, _ = cg.run_zero_cost_collectors()
        self.assertTrue(any(i["title"] == "pw" for i in items))

    def test_playwright_fallback_on_exception(self):
        # Weibo raises → playwright fallback recovers.
        pw_mod = mock.MagicMock()
        pw_mod.fetch_weibo_playwright = lambda: [_item("recovered", "https://r/1")]

        def boom():
            raise RuntimeError("http down")

        with mock.patch.object(global_collectors, "_refresh_cutoff", return_value=None), \
                mock.patch.dict(sys.modules, {"data_quality": mock.MagicMock(
                    SilentPlatformTracker=mock.MagicMock(side_effect=Exception("off"))),
                    "playwright_collectors": pw_mod}):
            self._patch_all_empty({"Weibo": boom})
            items, _ = cg.run_zero_cost_collectors()
        self.assertTrue(any(i["title"] == "recovered" for i in items))

    def test_auth_gated_zero_graceful(self):
        # Google Play is auth-gated; with no key env + 0 items → graceful, no fail.
        with mock.patch.object(global_collectors, "_refresh_cutoff", return_value=None), \
                mock.patch.dict(sys.modules, {"data_quality": mock.MagicMock(
                    SilentPlatformTracker=mock.MagicMock(side_effect=Exception("off"))),
                    "playwright_collectors": None}), \
                mock.patch.dict(cg.os.environ, {}, clear=True):
            self._patch_all_empty({"Weibo": lambda: [_item("t", "https://t/1")]})
            items, core_failures = cg.run_zero_cost_collectors()
        self.assertEqual(core_failures, [])
        self.assertEqual(len(items), 1)


# ── main() success + empty exit ──────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def test_empty_run_exits_nonzero(self):
        with mock.patch.object(cg, "run_zero_cost_collectors", return_value=([], [])):
            with self.assertRaises(SystemExit) as cm:
                cg.main()
        self.assertEqual(cm.exception.code, 1)

    def test_success_writes_and_returns(self):
        items = [{"title": "T", "url": "https://x/1", "engagement": 9,
                  "time": datetime.now(UTC).isoformat(), "source": "weibo"}]
        with mock.patch.object(cg, "run_zero_cost_collectors", return_value=(items, [])), \
                mock.patch.object(cg, "_mark_collected"), \
                mock.patch.object(news_common, "write_validation_drops",
                                  return_value={"total_dropped": 0, "by_source": {}}), \
                mock.patch.object(news_common, "dump_json_atomic") as dump:
            cg.main()  # no SystemExit on clean success
        # both news.json and news-raw.json written
        self.assertEqual(dump.call_count, 2)

    def test_core_failure_writes_sentinel_without_exit(self):
        """核心源失败：哨兵 + 0 退出（2026-08-22 起）。

        非零退出会让 workflow 跳过后续 archive/repair/health 步骤，本轮已采到的
        好数据随 runner 一起销毁——H9 当年正是为此把硬退出换成哨兵制。
        """
        items = [{"title": "T", "url": "https://x/1", "engagement": 9,
                  "time": datetime.now(UTC).isoformat(), "source": "weibo"}]
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "collect-failure.flag"
            with mock.patch.object(cg, "run_zero_cost_collectors",
                                   return_value=(items, [("youtube", "down")])), \
                    mock.patch.object(cg, "FAILURE_FLAG", flag), \
                    mock.patch.object(cg, "_mark_collected") as marked, \
                    mock.patch.object(news_common, "write_validation_drops",
                                      return_value={"total_dropped": 0, "by_source": {}}), \
                    mock.patch.object(news_common, "dump_json_atomic", return_value=None):
                cg.main()  # 不抛 SystemExit
            self.assertTrue(flag.exists())
            marked.assert_not_called()



class TestFetcherStubMapDoesNotDrift(unittest.TestCase):
    """测试桩映射漏一个源，用例就真的出网——把它升格为守卫，而不是靠人记得同步。"""

    def test_fetcher_stub_map_covers_every_registered_collector(self):
        import re as _re
        src = Path(cg.__file__).read_text(encoding="utf-8")
        block = src.split("zero_cost_fetchers = [", 1)[1].split("all_fetchers =", 1)[0]
        registered = set(_re.findall(r"c\.(fetch_[a-z_0-9]+)", block))
        stub_src = Path(__file__).read_text(encoding="utf-8")
        stubbed = set(_re.findall(r'"(fetch_[a-z_0-9]+)"', stub_src))
        missing = sorted(registered - stubbed)
        self.assertEqual(missing, [], f"这些已注册的采集器没有测试桩，用例会真的出网：{missing}")


if __name__ == "__main__":
    unittest.main()
