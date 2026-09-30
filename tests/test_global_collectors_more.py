import importlib
import sys
import unittest
from datetime import datetime, UTC
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import global_collectors as gc


# 固定的过去 CUTOFF，使时间过滤逻辑确定可测。
PAST_CUTOFF = datetime(2020, 1, 1, tzinfo=UTC)
RECENT = "2026-06-19T00:00:00+00:00"  # 远晚于 PAST_CUTOFF
OLD = "2019-01-01T00:00:00+00:00"     # 早于 PAST_CUTOFF


class FakeResp:
    """模拟 requests.Response：支持 .json()/.text/.status_code。"""

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


# ─── module-level helpers ──────────────────────────────────

class TestModuleHelpers(unittest.TestCase):
    def test_refresh_cutoff_updates_global(self):
        # _refresh_cutoff 重算全局 CUTOFF（line 58）
        original = gc.CUTOFF
        try:
            gc.CUTOFF = datetime(1999, 1, 1, tzinfo=UTC)
            gc._refresh_cutoff()
            self.assertGreater(gc.CUTOFF, datetime(2020, 1, 1, tzinfo=UTC))
        finally:
            gc.CUTOFF = original

    def test_get_delegates_to_news_common(self):
        # _get 委托 news_common.get_with_retry（line 84）
        sentinel = object()
        with mock.patch.object(gc.news_common, "get_with_retry", return_value=sentinel) as m:
            out = gc._get("https://x", params={"a": 1}, headers={"H": "1"})
        self.assertIs(out, sentinel)
        m.assert_called_once()


class TestGetCf(unittest.TestCase):
    def test_cloudscraper_success_path(self):
        # cloudscraper 存在 → 走 scraper.get 成功路径（lines 92-98）
        resp = FakeResp(text="ok")
        fake_scraper = mock.Mock()
        fake_scraper.get.return_value = resp
        fake_module = mock.Mock()
        fake_module.create_scraper.return_value = fake_scraper
        with mock.patch.dict(sys.modules, {"cloudscraper": fake_module}):
            out = gc._get_cf("https://cf", params={"p": 1}, headers={"X": "y"})
        self.assertIs(out, resp)
        fake_scraper.get.assert_called_once()

    def test_import_error_falls_back_to_get(self):
        # cloudscraper 缺失 → ImportError 回退 _get（lines 99-101）
        sentinel = FakeResp(text="fallback")
        with mock.patch.dict(sys.modules, {"cloudscraper": None}), \
                mock.patch.object(gc, "_get", return_value=sentinel) as m:
            out = gc._get_cf("https://cf")
        self.assertIs(out, sentinel)
        m.assert_called_once()


class TestHoursLookbackImportFallback(unittest.TestCase):
    def test_reimport_with_missing_collection_state(self):
        # 重新导入模块，模拟 collection_state ImportError → HOURS_LOOKBACK 走 except 分支（lines 50-51）
        with mock.patch.dict(sys.modules, {"collection_state": None}), \
                mock.patch.dict(gc.os.environ, {}, clear=True):
            reloaded = importlib.reload(gc)
            self.assertEqual(reloaded.HOURS_LOOKBACK, 24)
        # 还原模块到正常状态，避免污染其它测试
        importlib.reload(gc)


# ─── reddit RSS bad timestamp ──────────────────────────────

class TestParseRedditRssBadTime(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_bad_updated_value_skipped(self):
        # updated 非 ISO → ValueError 跳过（lines 159-160）
        xml = """<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Morimens x</title>
          <updated>not-a-date</updated></entry></feed>"""
        self.assertEqual(gc._parse_reddit_rss(xml, "Morimens"), [])


# ─── reddit JSON generic-sub keyword filter ────────────────

class TestFetchRedditGenericSub(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_generic_sub_non_matching_title_skipped(self):
        # gachagaming 综合版块，标题无关键词 → 跳过（lines 211-213）
        data = {"data": {"children": [
            {"data": {
                "title": "Random gacha thread",
                "created_utc": datetime(2026, 6, 19, tzinfo=UTC).timestamp(),
                "selftext": "",
                "permalink": "/r/gachagaming/x",
                "score": 1, "num_comments": 0, "author": "z",
                "link_flair_richtext": [],
            }},
        ]}}
        with mock.patch.object(gc, "_get", return_value=FakeResp(json_data=data)):
            self.assertEqual(gc.fetch_reddit(["gachagaming"]), [])


# ─── bilibili extra branches ───────────────────────────────

class TestFetchBilibiliExtra(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_spi_cookie_and_mixin_sign(self):
        # spi 返回 cookie → 设 Cookie 头（line 260）；mixin_key 存在 → sign_wbi_params（line 267）
        resp = FakeResp(json_data={"data": {"result": [{
            "title": "忘却前夜", "description": "d",
            "pubdate": datetime(2026, 6, 19, tzinfo=UTC).timestamp(),
            "play": 5, "danmaku": 1, "arcurl": "https://b", "author": "u",
            "typename": "游戏", "pic": "https://p",
        }]}})
        with mock.patch.object(gc.news_common, "bilibili_spi_cookies", return_value={"buvid3": "X"}), \
                mock.patch.object(gc.news_common, "get_wbi_mixin_key", return_value="mk"), \
                mock.patch.object(gc.news_common, "sign_wbi_params",
                                  side_effect=lambda p, k: p) as sign, \
                mock.patch.object(gc, "_get", return_value=resp):
            items = gc.fetch_bilibili()
        self.assertEqual(len(items), 2)
        self.assertTrue(sign.called)

    def test_old_pubdate_skipped(self):
        # created < CUTOFF → 跳过（line 280）
        resp = FakeResp(json_data={"data": {"result": [{
            "title": "x",
            "pubdate": datetime(2019, 1, 1, tzinfo=UTC).timestamp(),
        }]}})
        with mock.patch.object(gc.news_common, "bilibili_spi_cookies", return_value={}), \
                mock.patch.object(gc.news_common, "get_wbi_mixin_key", return_value=None), \
                mock.patch.object(gc, "_get", return_value=resp):
            self.assertEqual(gc.fetch_bilibili(), [])

    def test_request_exception_handled(self):
        # _get 抛异常 → warning 分支（lines 300-301）
        with mock.patch.object(gc.news_common, "bilibili_spi_cookies", return_value={}), \
                mock.patch.object(gc.news_common, "get_wbi_mixin_key", return_value=None), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            self.assertEqual(gc.fetch_bilibili(), [])


# ─── twitter extra branches ────────────────────────────────

class TestFetchTwitterExtra(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _html_with(self, tweet):
        import json
        payload = {"props": {"pageProps": {"timeline": {"entries": [tweet]}}}}
        inner = json.dumps(payload)
        return f'<script id="__NEXT_DATA__" type="application/json">{inner}</script>'

    def test_multi_handle_sleep_and_bad_time(self):
        # 两个 handle → idx>0 走 sleep（line 356）；无 created_at → iso None 跳过（line 379）
        tweet = {"full_text": "x", "id_str": "1", "created_at": "garbage",
                 "user": {"screen_name": "h"}}
        html = self._html_with(tweet)
        with mock.patch.dict(gc.os.environ, {"TWITTER_HANDLES": "a,b"}), \
                mock.patch.object(gc.time, "sleep") as slp, \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_twitter()
        self.assertEqual(items, [])
        self.assertTrue(slp.called)

    def test_old_tweet_skipped(self):
        # created < CUTOFF → 跳过（line 382）
        tweet = {"full_text": "x", "id_str": "1",
                 "created_at": "Tue Jan 01 00:00:00 +0000 2019",
                 "user": {"screen_name": "h"}}
        html = self._html_with(tweet)
        with mock.patch.dict(gc.os.environ, {"TWITTER_HANDLES": "a"}), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            self.assertEqual(gc.fetch_twitter(), [])

    def test_request_exception_handled(self):
        # _get 抛异常 → warning 分支（lines 414-415）
        with mock.patch.dict(gc.os.environ, {"TWITTER_HANDLES": "a"}), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("boom")):
            self.assertEqual(gc.fetch_twitter(), [])


# ─── youtube missing videoId ───────────────────────────────

class TestFetchYoutubeExtra(unittest.TestCase):
    def test_item_without_video_id_skipped(self):
        # 第二个 item 无 videoId → 跳过（line 465）
        search = {"items": [
            {"id": {"videoId": "v1"}, "snippet": {
                "title": "Morimens", "description": "d", "publishedAt": RECENT,
                "channelTitle": "ch", "thumbnails": {"high": {"url": "t"}}}},
            {"id": {}, "snippet": {"title": "no id"}},
        ]}
        stats = {"items": [{"id": "v1", "statistics": {"viewCount": "1", "likeCount": "0"}}]}

        def fake_get(url, *a, **k):
            return FakeResp(json_data=search if "search" in url else stats)

        with mock.patch.dict(gc.os.environ, {"YOUTUBE_API_KEY": "k"}), \
                mock.patch.object(gc, "_get", side_effect=fake_get):
            items = gc.fetch_youtube()
        # 甲方案双源：2 关键词（global）+ 1 日本频道（jp），各 1 条有效（无 videoId 的跳过）= 3
        self.assertEqual(len(items), 3)


# ─── weibo extra branches ──────────────────────────────────

class TestFetchWeiboExtra(unittest.TestCase):
    def test_cookie_header_and_approximate(self):
        # WEIBO_COOKIE 设置 → 加 Cookie 头（line 566）；created 为空 → approximate（line 595）
        resp = FakeResp(json_data={"data": {"cards": [
            {"card_type": 9, "mblog": {
                "created_at": "",
                "text": "忘却前夜 内容", "id": "1",
                "reposts_count": 0, "comments_count": 0, "attitudes_count": 0,
                "user": {"screen_name": "u"},
            }},
        ]}})
        with mock.patch.dict(gc.os.environ, {"WEIBO_COOKIE": "sess=1"}, clear=True), \
                mock.patch.object(gc, "_get", return_value=resp):
            items = gc.fetch_weibo()
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].get("time_is_approximate"))

    def test_request_exception_handled(self):
        # _get 抛异常 → warning 分支（lines 599-600）
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            self.assertEqual(gc.fetch_weibo(), [])

    @staticmethod
    def _card(mid):
        return {"card_type": 9, "mblog": {
            "created_at": "", "text": f"忘却前夜 {mid}", "id": str(mid),
            "reposts_count": 0, "comments_count": 0, "attitudes_count": 0,
            "user": {"screen_name": "u"},
        }}

    def test_pagination_walks_pages(self):
        """翻页：只取首页时一轮就那十来条，高峰日会整段刷过去（实测 8-13 归档为 0）。"""
        pages = {1: [self._card(1)], 2: [self._card(2)], 3: []}

        def fake_get(_url, params=None, **_kw):
            return FakeResp(json_data={"data": {"cards": pages.get(params["page"], [])}})

        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=fake_get):
            items = gc.fetch_weibo()
        # 2 个中文关键词 × 2 页各 1 条
        self.assertEqual(len(items), 4)

    def test_pagination_stops_on_repeated_page(self):
        """微博越界翻页会重复回吐上一页——整页无新 id 必须停，否则同批内容反复计入。"""
        calls = {"n": 0}

        def fake_get(_url, params=None, **_kw):
            calls["n"] += 1
            return FakeResp(json_data={"data": {"cards": [self._card(1)]}})

        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=fake_get):
            items = gc.fetch_weibo()
        # 每个关键词翻到第 2 页即判定重复停下：2 关键词 × 1 条，共 2×2 次请求
        self.assertEqual(len(items), 2)
        self.assertEqual(calls["n"], 4)


# ─── arca extra branches ───────────────────────────────────

class TestFetchArcaExtra(unittest.TestCase):
    def test_row_without_title_skipped(self):
        # 行有 href 段但 title 段缺失 → 跳过（line 640）
        html = '<a class="vrow column" href="/b/forgettingeve/1?p=1">no title span info missing'
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            self.assertEqual(gc.fetch_arca_live(), [])

    def test_empty_title_skipped(self):
        # title 段存在但 strip 后为空 → 跳过（line 643）
        html = ('<a class="vrow column" href="/b/forgettingeve/2?p=1">'
                '<span class="title">   </span> <span class="info"></span>')
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            self.assertEqual(gc.fetch_arca_live(), [])


# ─── discord extra branches ────────────────────────────────

class TestFetchDiscordExtra(unittest.TestCase):
    def test_empty_channel_id_skipped_and_exception(self):
        # channel_ids "c1,," → 空段 continue（line 695）；_get 抛异常 → warning（lines 723-724）
        with mock.patch.dict(gc.os.environ,
                             {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_IDS": "c1,,"},
                             clear=True), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("api down")):
            self.assertEqual(gc.fetch_discord(), [])


# ─── appstore exception ────────────────────────────────────

class TestFetchAppstoreExtra(unittest.TestCase):
    def test_request_exception_handled(self):
        # _get 抛异常 → debug 分支（lines 780-781），24 区全失败 → []
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            self.assertEqual(gc.fetch_appstore_reviews(), [])


# ─── pixiv extra branches ──────────────────────────────────

class TestFetchPixivExtra(unittest.TestCase):
    def test_malformed_inner_structures(self):
        # illustManga 非 dict（line 800）、data 非 list（line 803）兼容
        resp = FakeResp(json_data={"body": {"illustManga": "garbage"}})
        with mock.patch.object(gc, "_get", return_value=resp):
            self.assertEqual(gc.fetch_pixiv(), [])

    def test_illust_data_not_list(self):
        # illustManga 是 dict 但 data 非 list（line 803）
        resp = FakeResp(json_data={"body": {"illustManga": {"data": "garbage"}}})
        with mock.patch.object(gc, "_get", return_value=resp):
            self.assertEqual(gc.fetch_pixiv(), [])

    def test_raw_tags_not_list(self):
        # illust.tags 非 list → tag_list = []（line 821）
        resp = FakeResp(json_data={"body": {"illustManga": {"data": [
            {"id": "1", "title": "art", "description": "", "bookmarkCount": 1,
             "likeCount": 0, "tags": "notalist", "createDate": RECENT,
             "userName": "a", "url": "https://u"},
        ]}}})
        with mock.patch.object(gc, "_get", return_value=resp):
            items = gc.fetch_pixiv()
        self.assertEqual(items[0]["tags"], [])

    def test_request_exception_handled(self):
        # _get 抛异常 → warning 分支（lines 840-841）
        with mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            self.assertEqual(gc.fetch_pixiv(), [])


# ─── bahamut extra branches ────────────────────────────────

class TestFetchBahamutExtra(unittest.TestCase):
    # 2026-07-10 重写：采集器改解析专板 B.php 列表页（旧 ajax/search 双路径退役）。
    def test_non_row_html_returns_empty(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get",
                                  return_value=FakeResp(text="not a board page", status_code=200)):
            self.assertEqual(gc.fetch_bahamut(), [])

    def test_request_exception_handled(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            self.assertEqual(gc.fetch_bahamut(), [])

    def test_row_without_title_skipped(self):
        html = ('<tr class="b-list__row b-list-item">'
                '<a href="C.php?bsn=78829&amp;snA=2&amp;tnum=1"><img></a></tr>')
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=html, status_code=200)):
            self.assertEqual(gc.fetch_bahamut(), [])


# ─── weixin extra branches ─────────────────────────────────

class TestFetchWeixinExtra(unittest.TestCase):
    def test_empty_title_skipped(self):
        # 结果 title strip 后为空 → 跳过（line 1066）
        html = ('<script>timeConvert(\'1718755200\')</script>'
                '<h3><a href="https://s/1"><img></a></h3>'
                '<p class="txt-info">x</p>'
                '<div class="s-p">微信公众号: 号</div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            self.assertEqual(gc.fetch_weixin(), [])

    def test_timestamp_from_timeconvert(self):
        # timeConvert 时间戳 → time_approx False（lines 1082-1084）
        html = ("<script>timeConvert('1718755200')</script>"
                '<h3><a href="https://s/1">忘却前夜<em>新闻</em></a></h3>'
                '<div class="s-p">微信公众号: 测试号</div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_weixin()
        self.assertTrue(items)
        self.assertFalse(items[0].get("time_is_approximate"))

    def test_meta_date_fallback_and_approximate(self):
        # 无 timeConvert → meta 文本日期回退（lines 1089-1095）
        # 这里给一个不会被 timeConvert 命中的页面，meta 含日期
        html = ('<h3><a href="https://s/2">忘却前夜攻略</a></h3>'
                '<div class="s-p">微信公众号: 号 2025年3月15日</div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_weixin()
        self.assertTrue(items)
        self.assertIn("2025-03-15", items[0]["time"])
        self.assertFalse(items[0].get("time_is_approximate"))

    def test_no_time_fallback_now_and_approximate(self):
        # 无任何时间信息 → fallback now + approximate（lines 1100-1101, 1116）
        html = ('<h3><a href="https://s/3">忘却前夜资讯</a></h3>'
                '<p class="txt-info">正文</p>'
                '<div class="s-p">微信公众号: 号</div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_weixin()
        self.assertTrue(items)
        self.assertTrue(items[0].get("time_is_approximate"))

    def test_bad_meta_date_value_error(self):
        # meta 含非法日期（月份 13）→ datetime ValueError 被吞（lines 1096-1097）
        html = ('<h3><a href="https://s/4">忘却前夜消息</a></h3>'
                '<p class="txt-info">正文</p>'
                '<div class="s-p">微信公众号: 号 2025-13-40</div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_weixin()
        self.assertTrue(items)
        self.assertTrue(items[0].get("time_is_approximate"))


# ─── note_com exception ────────────────────────────────────

class TestFetchNoteComExtra(unittest.TestCase):
    def test_request_exception_handled(self):
        # RSS 路径 _get 抛异常 → warning 后返回空（2026-07-10 改走 hashtag RSS）
        with mock.patch.object(gc, "_get", side_effect=RuntimeError("blocked")):
            self.assertEqual(gc.fetch_note_com(), [])

    def test_missing_hashtag_404_is_quiet(self):
        # 标签不存在 → 404 归 info（正常态），返回空不告警
        with mock.patch.object(gc, "_get",
                               side_effect=RuntimeError("404 Client Error: Not Found")):
            self.assertEqual(gc.fetch_note_com(), [])


# ─── ruliweb extra branches ────────────────────────────────

class TestFetchRuliwebExtra(unittest.TestCase):
    def test_item_without_title_skipped(self):
        # board_search 存在，item 无 title anchor → 跳过（line 1218）
        html = ('<div id="board_search">'
                '<li class="search_result_item">no title here</li>'
                '</div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            self.assertEqual(gc.fetch_ruliweb(), [])

    def test_empty_title_skipped(self):
        # title anchor 存在但内容空白 → 跳过（line 1221）
        html = ('<div id="board_search">'
                '<li class="search_result_item">'
                '<a class="title text_over" href="/x/1">   </a>'
                '</li></div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            self.assertEqual(gc.fetch_ruliweb(), [])

    def test_bad_time_value_error_then_now_fallback(self):
        # time 匹配格式但日期非法（月 99）→ strptime ValueError（lines 1242-1243）→ now fallback（1246-1247）
        html = ('<div id="board_search">'
                '<li class="search_result_item">'
                '<a class="title text_over" href="/best/board/1/read/9">망각전야 글</a>'
                '<span class="time">2025.99.99</span>'
                '<span class="desc">설명</span>'
                '</li></div>')
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_ruliweb()
        self.assertTrue(items)
        self.assertTrue(items[0].get("time_is_approximate"))


# ─── stopgame extra branches ───────────────────────────────

class TestFetchStopgameExtra(unittest.TestCase):
    def test_meta_desc_and_ru_date_and_reviews(self):
        # 无 game-desc → meta description 回退（line 1304）；
        # 无 <time> → date_ru 解析（lines 1315-1323）；
        # review 含 ru 日期（lines 1376-1383）
        html = (
            '<div class="game-rating">8.0</div>'
            '<span>120 оценок</span>'
            '<meta name="description" content="Описание игры Morimens здесь">'
            '<span>15.03.2025</span>'
            '<div class="review">отзыв 10.02.2024</div>'
            '<div class="review-text">Отличная игра очень нравится мне всем</div>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        self.assertTrue(items)
        rating_item = items[0]
        self.assertIn("Описание", rating_item["summary"])
        self.assertIn("2025-03-15", rating_item["time"])
        # 至少有一条 review
        self.assertTrue(any(i["title"].startswith("[StopGame] Отличная") for i in items))

    def test_iso_date_and_now_fallback_and_approximate(self):
        # 无 ru 日期但有 ISO 日期（lines 1324-1330）
        html = (
            '<div class="game-rating">7.0</div>'
            '<span>5 отзыв</span>'
            '<p>release 2025-06-01 build</p>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        self.assertTrue(items)
        self.assertIn("2025-06-01", items[0]["time"])

    def test_no_date_now_fallback_and_review_approx(self):
        # 无任何日期 → page time now + approximate（lines 1333-1334, 1352）；
        # review 无日期 → 用 page time approx（line 1401）
        html = (
            '<div class="game-rating">6.5</div>'
            '<span>3 оцен</span>'
            '<div class="review-text">Неплохая игра советую всем попробовать сейчас</div>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        self.assertTrue(items)
        self.assertTrue(items[0].get("time_is_approximate"))
        review_items = [i for i in items if i["title"].startswith("[StopGame] Неплохая")]
        self.assertTrue(review_items)
        self.assertTrue(review_items[0].get("time_is_approximate"))

    def test_bad_ru_date_value_error(self):
        # ru 日期数值非法（月 99）→ datetime ValueError 被吞（lines 1322-1323）
        html = (
            '<div class="game-rating">5.0</div>'
            '<span>2 оцен</span>'
            '<p>99.99.2025</p>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        self.assertTrue(items)
        # 非法日期 → 回退 now，approximate True
        self.assertTrue(items[0].get("time_is_approximate"))

    def test_bad_iso_page_date_value_error(self):
        # ISO 日期数值非法（月 99）→ datetime ValueError 被吞（lines 1329-1330）
        html = (
            '<div class="game-rating">5.5</div>'
            '<span>4 оцен</span>'
            '<p>release 2025-99-99 build</p>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        self.assertTrue(items)
        self.assertTrue(items[0].get("time_is_approximate"))

    def test_review_bad_ru_date_value_error(self):
        # review 关联 ru 日期非法 → datetime ValueError 被吞（lines 1382-1383）
        html = (
            '<div class="game-rating">6.0</div>'
            '<span>7 оцен</span>'
            '<time datetime="2026-01-01T00:00:00+00:00"></time>'
            '<div class="review">99.99.2025</div>'
            '<div class="review-text">Хорошая игра рекомендую всем друзьям своим</div>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        self.assertTrue(items)
        review_items = [i for i in items if i["title"].startswith("[StopGame] Хорошая")]
        self.assertTrue(review_items)
        # 非法 ru 日期 → 回退 page time（非 approximate，因为 page 有 <time>）
        self.assertFalse(review_items[0].get("time_is_approximate"))


if __name__ == "__main__":
    unittest.main()
