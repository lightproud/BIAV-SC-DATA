"""global_collectors coverage for branches the existing test leaves: _get_cf
(cloudscraper success + ImportError fallback), fetch_google_play success path,
fetch_taptap success path, and assorted fetcher exception / fallback branches.

Hermetic — network helpers (_get / _get_cf / requests) and optional libs are
mocked; CUTOFF pinned to the past so recency filters pass.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone, UTC
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import global_collectors as gc


PAST_CUTOFF = datetime(2020, 1, 1, tzinfo=UTC)
RECENT = "2026-06-19T00:00:00+00:00"


class FakeResp:
    def __init__(self, json_data=None, text="", status_code=200):
        self._json = json_data
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        pass


# ── _get_cf ──────────────────────────────────────────────────────────────────

class TestGetCf(unittest.TestCase):
    def test_cloudscraper_success(self):
        resp = FakeResp(text="ok")
        scraper = mock.Mock()
        scraper.get.return_value = resp
        fake_cs = mock.Mock()
        fake_cs.create_scraper.return_value = scraper
        with mock.patch.dict(sys.modules, {"cloudscraper": fake_cs}):
            out = gc._get_cf("https://x", params={"a": 1})
        self.assertIs(out, resp)
        scraper.get.assert_called_once()

    def test_importerror_falls_back_to_get(self):
        with mock.patch.dict(sys.modules, {"cloudscraper": None}), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text="fb")) as g:
            out = gc._get_cf("https://x")
        self.assertEqual(out.text, "fb")
        g.assert_called_once()


# ── _post retry/debug branch ─────────────────────────────────────────────────

class TestPostRetry(unittest.TestCase):
    def test_retry_then_success_logs_debug(self):
        ok = FakeResp(json_data={"ok": 1})
        with mock.patch.object(gc.requests, "post",
                               side_effect=[gc.requests.RequestException("x"), ok]), \
                mock.patch.object(gc.time, "sleep"):
            out = gc._post("https://x")
        self.assertIs(out, ok)


# ── fetch_youtube exception branch ───────────────────────────────────────────

class TestFetchYoutubeError(unittest.TestCase):
    def test_search_exception_redacted(self):
        with mock.patch.dict(gc.os.environ, {"YOUTUBE_API_KEY": "secretkey"}), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("403 key=secretkey")):
            # exception swallowed per keyword → returns [] without raising
            self.assertEqual(gc.fetch_youtube(), [])


# ── fetch_taptap success path ────────────────────────────────────────────────

class TestFetchTaptapSuccess(unittest.TestCase):
    def test_collects_via_taptap_collector(self):
        fake_tc = mock.MagicMock()
        # gc.fetch_taptap calls asyncio.run(_tc.collect(...)); stub asyncio.run
        # to return the (topics, reviews) tuple deterministically.
        with mock.patch.dict(sys.modules, {"taptap_collector": fake_tc}), \
                mock.patch.object(gc.asyncio, "run",
                                  return_value=([{"title": "post"}], [{"title": "review"}])):
            items = gc.fetch_taptap()
        self.assertEqual(len(items), 2)

    def test_collect_exception_returns_empty(self):
        fake_tc = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"taptap_collector": fake_tc}), \
                mock.patch.object(gc.asyncio, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(gc.fetch_taptap(), [])


# ── fetch_google_play success path ───────────────────────────────────────────

class TestFetchGooglePlaySuccess(unittest.TestCase):
    def test_collects_reviews(self):
        review = {
            "score": 5, "content": "love it",
            "at": datetime(2026, 6, 19, tzinfo=UTC),
            "thumbsUpCount": 7, "userName": "fan",
        }
        fake_mod = mock.MagicMock()

        class _Sort:
            NEWEST = "newest"

        fake_mod.reviews = lambda *a, **k: ([review], None)
        fake_mod.Sort = _Sort
        with mock.patch.dict(sys.modules, {"google_play_scraper": fake_mod}), \
                mock.patch.dict(gc.os.environ, {"GOOGLE_PLAY_PACKAGE": "com.x"}, clear=True):
            items = gc.fetch_google_play()
        # 22 locales, 1 review each
        self.assertTrue(items)
        self.assertEqual(items[0]["source"], "google_play")
        self.assertIn("好评", items[0]["title"])

    def test_locale_exception_skipped(self):
        fake_mod = mock.MagicMock()

        class _Sort:
            NEWEST = "newest"

        def boom(*a, **k):
            raise RuntimeError("rate limited")

        fake_mod.reviews = boom
        fake_mod.Sort = _Sort
        with mock.patch.dict(sys.modules, {"google_play_scraper": fake_mod}), \
                mock.patch.dict(gc.os.environ, {}, clear=True):
            self.assertEqual(gc.fetch_google_play(), [])


# ── fetch_bahamut exception branch ───────────────────────────────────────────

class TestFetchBahamutError(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_all_paths_fail_returns_list(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            out = gc.fetch_bahamut()
        self.assertIsInstance(out, list)


# ── fetch_arca_live exception branch ─────────────────────────────────────────

class TestFetchArcaError(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_get_raises_tolerated(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("blocked")):
            self.assertEqual(gc.fetch_arca_live(), [])


# ── fetch_ruliweb playwright-time parse branch ───────────────────────────────

class TestFetchRuliwebTime(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_time_with_hh_mm_parsed(self):
        html = (
            '<div id="board_search">'
            '<li class="search_result_item">'
            '<a class="title text_over" href="/best/board/300143/read/2">망각전야 글</a>'
            '<span class="time">2026.06.19 12:30</span>'
            '<span class="desc">설명</span>'
            '</li></div>'
        )
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_ruliweb()
        self.assertTrue(items)
        self.assertTrue(items[0]["time"].startswith("2026-06-19"))

    def test_get_cf_raises_tolerated(self):
        with mock.patch.object(gc, "_get_cf", side_effect=RuntimeError("cf down")):
            self.assertEqual(gc.fetch_ruliweb(), [])


# ── 时区口径三卫（2026-09-18 守密人裁定「三处都修」，对账报告 D5 / D6）────────

class TestRuliwebDateOnlyUsesNoon(unittest.TestCase):
    """日期级来源必须取当地正午，不取零点。

    零点站在时区分界线上：KST 00:00 折成北京日期就掉进前一天的桶，整批帖子日期
    系统性早一天（2026-09-17 对账实测 301/448 条）。正午离两侧日界各 12 小时。
    """

    def setUp(self):
        self._p = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _fetch(self, shown):
        html = (
            '<div id="board_search"><li class="search_result_item">'
            '<a class="title text_over" href="/best/board/300143/read/2">망각전야 글</a>'
            f'<span class="time">{shown}</span>'
            '<span class="desc">설명</span>'
            '</li></div>'
        )
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            return gc.fetch_ruliweb()

    def test_date_only_lands_on_its_own_korean_day(self):
        items = self._fetch("2026.06.19")
        self.assertTrue(items)
        when = datetime.fromisoformat(items[0]["time"])
        kst = when.astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(kst.strftime("%Y-%m-%d"), "2026-06-19", "韩国本地日历日必须守住")
        # 归档桶按北京日算，正午代表时刻下两者同日
        beijing = when.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(beijing.strftime("%Y-%m-%d"), "2026-06-19")

    def test_hh_mm_still_uses_real_clock(self):
        items = self._fetch("2026.06.19 12:30")
        kst = datetime.fromisoformat(items[0]["time"]).astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(kst.strftime("%Y-%m-%d %H:%M"), "2026-06-19 12:30")


class TestPacificWallclockIso(unittest.TestCase):
    """苹果 RSS 的 `updated` 偏移标签全年恒为 -07:00，冬季条目因此偏 1 小时。"""

    def test_winter_entry_relabelled_to_pst(self):
        out = gc._pacific_wallclock_iso("2023-12-07T12:19:29-07:00")
        self.assertTrue(out.endswith("-08:00"), f"冬令时应重标为 PST，实得 {out}")
        self.assertTrue(out.startswith("2023-12-07T12:19:29"), "墙钟不得被改动")

    def test_summer_entry_stays_pdt(self):
        out = gc._pacific_wallclock_iso("2024-07-07T12:19:29-07:00")
        self.assertTrue(out.endswith("-07:00"))

    def test_blank_and_garbage_pass_through(self):
        self.assertEqual(gc._pacific_wallclock_iso(""), "")
        self.assertEqual(gc._pacific_wallclock_iso("not-a-time"), "not-a-time")


class TestGooglePlayAtIso(unittest.TestCase):
    """naive datetime 必须被显式定位，不能留给下游按 UTC 默认解释。"""

    def test_aware_value_normalised_to_utc(self):
        out = gc._google_play_at_iso(datetime(2026, 6, 19, 8, 0, tzinfo=timezone(timedelta(hours=9))))
        self.assertEqual(datetime.fromisoformat(out).utcoffset(), timedelta(0))
        self.assertEqual(out[:16], "2026-06-18T23:00")

    def test_naive_value_gets_an_offset(self):
        out = gc._google_play_at_iso(datetime(2026, 6, 19, 8, 0))
        self.assertIsNotNone(datetime.fromisoformat(out).tzinfo, "落档时间戳不得无时区")

    def test_none_passes_through(self):
        self.assertIsNone(gc._google_play_at_iso(None))


# ── fetch_weixin date-fallback branch ────────────────────────────────────────

class TestFetchWeixinDateFallback(unittest.TestCase):
    def test_meta_date_pattern_used(self):
        # No timeConvert script → falls back to date pattern inside s-p meta.
        html = (
            '<h3><a href="https://sogou/x">忘却前夜<em>资讯</em></a></h3>'
            '<p class="txt-info">摘要</p>'
            '<div class="s-p">微信公众号: 号 2025-03-15</div>'
        )
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_weixin()
        self.assertTrue(items)
        self.assertTrue(any(i["time"].startswith("2025-03-15") for i in items))


# ── fetch_stopgame review extraction branch ──────────────────────────────────

class TestFetchStopgameReviews(unittest.TestCase):
    def test_review_with_iso_time(self):
        html = (
            '<div class="game-rating">9.0</div>'
            '<span>200 оценок</span>'
            '<div class="review">'
            '<time datetime="2026-06-19T00:00:00+00:00"></time>'
            '<div class="review-text">Замечательная игра действительно нравится</div>'
            '</div>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        # rating item + at least one review item
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(any("review-text" not in i["title"] for i in items))


if __name__ == "__main__":
    unittest.main()
