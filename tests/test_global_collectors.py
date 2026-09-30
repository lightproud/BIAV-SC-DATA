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


# ─── 纯工具函数 ────────────────────────────────────────────

class TestStripHelpers(unittest.TestCase):
    def test_strip_html_tags_trims(self):
        self.assertEqual(gc._strip_html_tags("  <b>hi</b>  "), "hi")

    def test_strip_html_delegate(self):
        self.assertEqual(gc._strip_html("<i>x</i>"), "x")

    def test_make_item_is_news_common(self):
        item = gc._make_item(
            title="t", summary="s", source="reddit", platform_region="global",
            time_str=RECENT, url="https://x", engagement=5,
        )
        self.assertEqual(item["source"], "reddit")
        self.assertEqual(item["engagement"], 5)

    def test_make_item_no_layering_fields_by_default(self):
        # 甲方案：不传 region/archive_subtype 时不落字段（不带字段的源零破坏）
        item = gc._make_item(
            title="t", summary="s", source="reddit", platform_region="global",
            time_str=RECENT, url="https://x",
        )
        self.assertNotIn("region", item)
        self.assertNotIn("archive_subtype", item)

    def test_make_item_layering_fields_written(self):
        # 甲方案：显式标注则写入，供 archive_platforms 分桶 <平台>/<区服>/<类型>/
        item = gc._make_item(
            title="t", summary="s", source="steam", platform_region="jp",
            time_str=RECENT, url="https://x", region="jp", archive_subtype="review",
        )
        self.assertEqual(item["region"], "jp")
        self.assertEqual(item["archive_subtype"], "review")


class TestParseTwitterTime(unittest.TestCase):
    def test_valid(self):
        iso = gc._parse_twitter_time("Fri May 22 10:29:33 +0000 2026")
        self.assertTrue(iso.startswith("2026-05-22T10:29:33"))

    def test_invalid_returns_none(self):
        self.assertIsNone(gc._parse_twitter_time("not a date"))

    def test_none_returns_none(self):
        self.assertIsNone(gc._parse_twitter_time(None))


class TestTwitterWalkTweets(unittest.TestCase):
    def test_collects_nested_tweets(self):
        obj = {
            "a": {"full_text": "hi", "id_str": "1"},
            "b": [{"full_text": "yo", "id_str": "2"}, {"no": "match"}],
            "c": {"full_text": "missing id"},
        }
        acc = []
        gc._twitter_walk_tweets(obj, acc)
        ids = sorted(t["id_str"] for t in acc)
        self.assertEqual(ids, ["1", "2"])

    def test_empty(self):
        acc = []
        gc._twitter_walk_tweets({}, acc)
        gc._twitter_walk_tweets([], acc)
        self.assertEqual(acc, [])


class TestParseWeiboTime(unittest.TestCase):
    def test_yesterday_hh_mm(self):
        iso, approx = gc._parse_weibo_time("昨天 14:30")
        self.assertFalse(approx)
        self.assertIn("14:30", iso)

    def test_full_date_format(self):
        iso, approx = gc._parse_weibo_time("Wed Jan 01 00:00:00 +0800 2025")
        self.assertFalse(approx)
        self.assertTrue(iso.startswith("2025-01-01"))

    def test_empty_is_approximate(self):
        iso, approx = gc._parse_weibo_time("")
        self.assertTrue(approx)

    def test_relative_delegated(self):
        iso, approx = gc._parse_weibo_time("3小时前")
        self.assertFalse(approx)


class TestParseRedditRss(unittest.TestCase):
    ATOM = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Morimens new banner</title>
        <link href="https://reddit.com/r/Morimens/1"/>
        <updated>{recent}</updated>
        <author><name>alice</name></author>
        <content>&lt;p&gt;body text&lt;/p&gt;</content>
      </entry>
    </feed>"""

    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_parses_dedicated_sub(self):
        xml = self.ATOM.format(recent=RECENT)
        items = gc._parse_reddit_rss(xml, "Morimens")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Morimens new banner")
        self.assertEqual(items[0]["author"], "u/alice")
        self.assertEqual(items[0]["summary"], "body text")

    def test_old_entry_filtered(self):
        xml = self.ATOM.format(recent=OLD)
        self.assertEqual(gc._parse_reddit_rss(xml, "Morimens"), [])

    def test_missing_updated_skipped(self):
        xml = """<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>X</title></entry></feed>"""
        self.assertEqual(gc._parse_reddit_rss(xml, "Morimens"), [])

    def test_generic_sub_requires_keyword(self):
        # 标题不含关键词 → 综合版块过滤掉
        xml = self.ATOM.format(recent=RECENT).replace(
            "Morimens new banner", "Random gacha talk"
        )
        self.assertEqual(gc._parse_reddit_rss(xml, "gachagaming"), [])

    def test_generic_sub_keeps_keyword_match(self):
        items = gc._parse_reddit_rss(self.ATOM.format(recent=RECENT), "gachagaming")
        self.assertEqual(len(items), 1)


# ─── fetch_* （mock 网络层） ────────────────────────────────

class TestFetchReddit(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _post_json(self):
        return {"data": {"children": [
            {"data": {
                "title": "Morimens update",
                "created_utc": datetime(2026, 6, 19, tzinfo=UTC).timestamp(),
                "selftext": "details",
                "permalink": "/r/Morimens/abc",
                "score": 150,
                "num_comments": 20,
                "author": "bob",
                "link_flair_richtext": [{"text": "News"}, {"no": "text"}],
            }},
        ]}}

    def test_json_path(self):
        with mock.patch.object(gc, "_get", return_value=FakeResp(json_data=self._post_json())):
            items = gc.fetch_reddit(["Morimens"])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["url"], "https://reddit.com/r/Morimens/abc")
        self.assertEqual(it["engagement"], 170)
        self.assertTrue(it["is_hot"])  # score 150 > 100
        self.assertEqual(it["tags"], ["News"])

    def test_old_post_filtered(self):
        data = self._post_json()
        data["data"]["children"][0]["data"]["created_utc"] = \
            datetime(2019, 1, 1, tzinfo=UTC).timestamp()
        with mock.patch.object(gc, "_get", return_value=FakeResp(json_data=data)):
            self.assertEqual(gc.fetch_reddit(["Morimens"]), [])

    def test_json_failure_falls_back_to_rss(self):
        atom = TestParseRedditRss.ATOM.format(recent=RECENT)

        def fake_get(url, *a, **k):
            if url.endswith(".rss"):
                return FakeResp(text=atom)
            raise RuntimeError("json api down")

        with mock.patch.object(gc, "_get", side_effect=fake_get):
            items = gc.fetch_reddit(["Morimens"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "reddit")

    def test_both_paths_fail_returns_empty(self):
        with mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            self.assertEqual(gc.fetch_reddit(["Morimens"]), [])


class TestFetchBilibili(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _resp(self):
        return FakeResp(json_data={"data": {"result": [{
            "title": "忘却前夜实况",
            "description": "desc",
            "pubdate": datetime(2026, 6, 19, tzinfo=UTC).timestamp(),
            "play": 20000,
            "danmaku": 100,
            "arcurl": "https://bili/v1",
            "author": "up主",
            "typename": "游戏",
            "pic": "https://img",
        }]}})

    def test_collects_videos(self):
        with mock.patch.object(gc.news_common, "bilibili_spi_cookies", return_value={}), \
                mock.patch.object(gc.news_common, "get_wbi_mixin_key", return_value=None), \
                mock.patch.object(gc, "_get", return_value=self._resp()):
            items = gc.fetch_bilibili()
        # KEYWORDS["zh"] 有 2 个关键词，每个返回 1 条
        self.assertEqual(len(items), 2)
        it = items[0]
        self.assertEqual(it["source"], "bilibili")
        self.assertEqual(it["engagement"], 20100)
        self.assertTrue(it["is_hot"])
        self.assertEqual(it["content_type"], "video")

    def test_zero_pubdate_skipped(self):
        resp = FakeResp(json_data={"data": {"result": [{"title": "x", "pubdate": 0}]}})
        with mock.patch.object(gc.news_common, "bilibili_spi_cookies", return_value={}), \
                mock.patch.object(gc.news_common, "get_wbi_mixin_key", return_value=None), \
                mock.patch.object(gc, "_get", return_value=resp):
            self.assertEqual(gc.fetch_bilibili(), [])


class TestFetchTwitter(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(gc, "CUTOFF", PAST_CUTOFF)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _html(self, screen="MorimensOfcl"):
        import json
        payload = {"props": {"pageProps": {"timeline": {"entries": [
            {"full_text": "Official news drop", "id_str": "999",
             "created_at": "Fri Jun 19 10:00:00 +0000 2026",
             "favorite_count": 600, "retweet_count": 10, "reply_count": 5,
             "user": {"screen_name": screen, "lang": "en"},
             "entities": {"media": [{"media_url_https": "https://img.jpg"}]}},
        ]}}}}
        inner = json.dumps(payload)
        return f'<script id="__NEXT_DATA__" type="application/json">{inner}</script>'

    def test_single_handle(self):
        with mock.patch.dict(gc.os.environ, {"TWITTER_HANDLES": "MorimensOfcl"}), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=self._html())):
            items = gc.fetch_twitter()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source"], "twitter")
        self.assertEqual(it["url"], "https://x.com/MorimensOfcl/status/999")
        self.assertEqual(it["engagement"], 600 + 10 * 3 + 5 * 2)
        self.assertTrue(it["is_hot"])
        self.assertEqual(it["content_type"], "image")
        self.assertEqual(it["media_url"], "https://img.jpg")
        self.assertEqual(it["region"], "global")          # 甲方案：@MorimensOfcl → global 区服
        self.assertEqual(it["platform_region"], "global")

    def test_jp_handle_region(self):
        # 日服官方账号 @bokyakuzenya（AltPlus）→ 拆 jp 区服，修正旧硬编码 global bug
        with mock.patch.dict(gc.os.environ, {"TWITTER_HANDLES": "bokyakuzenya"}), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=self._html("bokyakuzenya"))):
            items = gc.fetch_twitter()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["region"], "jp")
        self.assertEqual(it["platform_region"], "jp")
        self.assertEqual(it["url"], "https://x.com/bokyakuzenya/status/999")

    def test_unknown_handle_no_region(self):
        # 未登记的自定义 handle：不落 region 字段 → archive 回落扁平 twitter/
        with mock.patch.dict(gc.os.environ, {"TWITTER_HANDLES": "somefan"}), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=self._html("somefan"))):
            items = gc.fetch_twitter()
        self.assertEqual(len(items), 1)
        self.assertNotIn("region", items[0])
        self.assertEqual(items[0]["platform_region"], "global")  # 回落默认

    def test_no_next_data_skipped(self):
        with mock.patch.dict(gc.os.environ, {"TWITTER_HANDLES": "MorimensOfcl"}), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text="<html>no payload</html>")):
            self.assertEqual(gc.fetch_twitter(), [])


class TestFetchYoutube(unittest.TestCase):
    def test_no_api_key_returns_empty(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True):
            self.assertEqual(gc.fetch_youtube(), [])

    def test_collects_videos(self):
        search = {"items": [{"id": {"videoId": "v1"}, "snippet": {
            "title": "Morimens trailer", "description": "d",
            "publishedAt": RECENT, "channelTitle": "ch",
            "thumbnails": {"high": {"url": "https://t.jpg"}}}}]}
        stats = {"items": [{"id": "v1", "statistics": {"viewCount": "6000", "likeCount": "200"}}]}

        def fake_get(url, *a, **k):
            if "search" in url:
                return FakeResp(json_data=search)
            return FakeResp(json_data=stats)

        with mock.patch.dict(gc.os.environ, {"YOUTUBE_API_KEY": "k"}), \
                mock.patch.object(gc, "_get", side_effect=fake_get):
            items = gc.fetch_youtube()
        # 甲方案双源：2 关键词（global 社区流）+ 1 日本官方频道（jp）= 3
        self.assertEqual(len(items), 3)
        it = items[0]
        self.assertEqual(it["engagement"], 6200)
        self.assertTrue(it["is_hot"])
        self.assertEqual(it["content_type"], "video")
        self.assertEqual(it["region"], "global")                  # 关键词社区流 → global 区服
        self.assertEqual(it["archive_subtype"], "video")          # 归档 youtube/<区服>/video
        self.assertTrue(any(i["region"] == "jp" for i in items))  # 日本官方频道 → jp 区服


class TestFetchWeibo(unittest.TestCase):
    def _resp(self):
        return FakeResp(json_data={"data": {"cards": [
            {"card_type": 9, "mblog": {
                "created_at": "3小时前",
                "text": "忘却前夜 <a>新闻</a> 内容",
                "id": "123",
                "reposts_count": 10, "comments_count": 5, "attitudes_count": 600,
                "user": {"screen_name": "weibo_user"},
            }},
            {"card_type": 1},  # 非帖子类型，跳过
        ]}})

    def test_collects_posts(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=self._resp()):
            items = gc.fetch_weibo()
        self.assertEqual(len(items), 2)  # KEYWORDS["zh"] 2 个关键词
        it = items[0]
        self.assertEqual(it["source"], "weibo")
        self.assertEqual(it["engagement"], 615)
        self.assertTrue(it["is_hot"])
        self.assertEqual(it["url"], "https://m.weibo.cn/detail/123")


class TestFetchArcaLive(unittest.TestCase):
    HTML = (
        '<a class="vrow column" href="/b/forgettingeve/111?p=1">'
        '<span class="title">망각전야 패치</span> <span class="info">'
        '<time datetime="2026-06-19T00:00:00+00:00"></time>'
        '<span class="vcol col-view"> 1,234</span>'
        '<span class="vcol col-rate"> 15</span>'
        '<span class="comment-count"> [7]</span>'
        '<span data-filter="author1"></span>'
    )

    def test_parses_rows(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text=self.HTML)):
            items = gc.fetch_arca_live()
        # best + latest 两个 mode，但 URL 去重 → 同一条只进 1 次
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source"], "arca_live")
        self.assertEqual(it["url"], "https://arca.live/b/forgettingeve/111")
        # views 1234 + rate 15*5 + comments 7*2 = 1323
        self.assertEqual(it["engagement"], 1234 + 75 + 14)
        self.assertEqual(it["author"], "author1")

    def test_empty_html(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=FakeResp(text="<html></html>")):
            self.assertEqual(gc.fetch_arca_live(), [])


class TestFetchDiscord(unittest.TestCase):
    def test_no_token_returns_empty(self):
        with mock.patch.dict(gc.os.environ, {}, clear=True):
            self.assertEqual(gc.fetch_discord(), [])

    def test_collects_messages(self):
        msgs = [
            {"content": "hot msg", "reactions": [{"count": 8}, {"count": 5}],
             "timestamp": RECENT, "id": "m1", "guild_id": "g1",
             "author": {"username": "u"}},
            {"content": "ignored", "reactions": [{"count": 1}], "timestamp": RECENT},
        ]
        with mock.patch.dict(gc.os.environ,
                             {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_IDS": "c1"}, clear=True), \
                mock.patch.object(gc, "_get", return_value=FakeResp(json_data=msgs)):
            items = gc.fetch_discord()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["engagement"], 13)
        self.assertTrue(it["is_hot"])  # 13 > 10
        self.assertEqual(it["author"], "u")


class TestFetchAppstoreReviews(unittest.TestCase):
    def test_collects_reviews(self):
        entry = {
            "im:rating": {"label": "5"},
            "title": {"label": "Great"},
            "content": {"label": "love it"},
            "updated": {"label": RECENT},
            "author": {"name": {"label": "reviewer"}},
        }
        resp = FakeResp(json_data={"feed": {"entry": [entry]}})
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=resp):
            items = gc.fetch_appstore_reviews()
        # 甲方案双 appid：global app 24 区 + jp 独立 app（仅日本店）1 条 = 25
        self.assertEqual(len(items), 25)
        it = items[0]
        self.assertEqual(it["source"], "appstore")
        self.assertEqual(it["engagement"], 5)
        self.assertEqual(it["region"], "global")                  # global app 评论标 global 区服
        self.assertTrue(any(i["region"] == "jp" for i in items))  # jp 独立 app（AltPlus）评论标 jp

    def test_empty_id_returns_empty(self):
        with mock.patch.dict(gc.os.environ, {"APPSTORE_APP_ID": ""}, clear=True):
            self.assertEqual(gc.fetch_appstore_reviews(), [])


class TestFetchPixiv(unittest.TestCase):
    def _resp(self):
        return FakeResp(json_data={"body": {"illustManga": {"data": [
            {"id": "1001", "title": "fan art", "description": "d",
             "bookmarkCount": 600, "likeCount": 100,
             "tags": ["忘却前夜", {"tag": "イラスト"}],
             "createDate": RECENT, "userName": "artist", "url": "https://px.jpg"},
            "not a dict",  # 应跳过
        ]}}})

    def test_collects_artworks(self):
        with mock.patch.object(gc, "_get", return_value=self._resp()):
            items = gc.fetch_pixiv()
        # 3 个关键词
        self.assertEqual(len(items), 3)
        it = items[0]
        self.assertEqual(it["source"], "pixiv")
        self.assertEqual(it["engagement"], 700)
        self.assertTrue(it["is_hot"])
        self.assertEqual(it["tags"], ["忘却前夜", "イラスト"])
        self.assertEqual(it["content_type"], "image")

    def test_malformed_body_returns_empty(self):
        with mock.patch.object(gc, "_get", return_value=FakeResp(json_data={"body": "garbage"})):
            self.assertEqual(gc.fetch_pixiv(), [])


class TestFetchBahamut(unittest.TestCase):
    # 2026-07-10 重写：ajax JSON / search.php 双路径已死（接口退役），
    # 采集器改为解析专板 B.php 列表页 HTML（默认 bsn=78829）。
    BOARD_FIXTURE = (
    # 置顶行：<a class="b-list__main__title">…</a> 直持 href
    '<tr class="b-list__row b-list__row--sticky b-list-item">'
    '<span class="b-list__summary__gp b-gp">60</span>'
    '<a href="C.php?bsn=78829&amp;snA=581&amp;tnum=1" class="b-list__main__title">'
    '【情報】置頂公告</a>'
    '<p class="b-list__count__number"><span title="互動：10">10</span></p>'
    '<p class="b-list__count__user"><a href="https://home.gamer.com.tw/u1">sticky_user</a></p>'
    '<p class="b-list__time__edittime"><a href="C.php?bsn=78829&amp;snA=581&amp;last=1">2026-06-19</a></p>'
    '</tr>'
    # 普通行：外层大锚点持 href，标题在 <p class="b-list__main__title">…</p>
    '<tr class="b-list__row b-list-item">'
    '<a href="C.php?bsn=78829&amp;snA=797&amp;tnum=1">'
    '<p class="b-list__main__title">【問題】新手求助</p>'
    '<p class="b-list__brief">正文摘要</p></a>'
    '<p class="b-list__count__number"><span title="互動：3">3</span></p>'
    '<p class="b-list__count__user"><a href="https://home.gamer.com.tw/u2">normal_user</a></p>'
    '<p class="b-list__time__edittime"><a href="C.php?bsn=78829&amp;snA=797&amp;last=1">2026-06-20</a></p>'
    '</tr>'
)

    def test_parses_board_rows_both_shapes(self):
        resp = FakeResp(text=self.BOARD_FIXTURE, status_code=200)
        with mock.patch.dict(gc.os.environ, {}, clear=True), \
                mock.patch.object(gc, "_get", return_value=resp):
            items = gc.fetch_bahamut()
        self.assertEqual(len(items), 2)
        sticky = next(i for i in items if i["author"] == "sticky_user")
        self.assertEqual(sticky["title"], "【情報】置頂公告")
        self.assertEqual(sticky["engagement"], 70)   # gp60 + 互動10
        self.assertTrue(sticky["is_hot"])            # gp 60 > 50
        self.assertIn("snA=581", sticky["url"])
        normal = next(i for i in items if i["author"] == "normal_user")
        self.assertEqual(normal["title"], "【問題】新手求助")
        self.assertFalse(normal["is_hot"])
        self.assertTrue(normal["time"].startswith("2026-06-20"))

    def test_bsn_env_override(self):
        captured = {}

        def fake_get(url, params=None, **k):
            captured.update(params or {})
            return FakeResp(text="", status_code=200)

        with mock.patch.dict(gc.os.environ, {"BAHAMUT_BSN": "999"}, clear=True), \
                mock.patch.object(gc, "_get", side_effect=fake_get):
            self.assertEqual(gc.fetch_bahamut(), [])
        self.assertEqual(captured.get("bsn"), "999")


class TestFetchWeixin(unittest.TestCase):
    def test_parses_results(self):
        html = (
            "<script>timeConvert('1718755200')</script>"
            '<h3><a href="https://sogou/link1">忘却前夜<em>新闻</em></a></h3>'
            '<p class="txt-info">摘要内容</p>'
            '<div class="s-p">微信公众号: 测试号</div>'
        )
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_weixin()
        self.assertTrue(items)
        it = items[0]
        self.assertEqual(it["source"], "weixin")
        self.assertTrue(it["title"].startswith("[微信]"))

    def test_failure_returns_empty(self):
        with mock.patch.object(gc, "_get_cf", side_effect=RuntimeError("blocked")):
            self.assertEqual(gc.fetch_weixin(), [])


class TestFetchNoteCom(unittest.TestCase):
    # 2026-07-10 重写：/api/v3/searches 已一律 403，采集器改走 hashtag RSS。
    RSS_FIXTURE = (
        '<rss><channel>'
        '<item><title><![CDATA[攻略記事]]></title>'
        '<description><![CDATA[<p>本文サマリ</p>]]></description>'
        '<link>https://note.com/w/n/n1</link>'
        '<note:creatorName> writer </note:creatorName>'
        '<pubDate>Thu, 09 Jul 2026 12:00:00 +0900</pubDate></item>'
        '<item><title>重複</title><link>https://note.com/w/n/n1</link></item>'
        '</channel></rss>'
    )

    def test_collects_notes_from_rss(self):
        resp = FakeResp(text=self.RSS_FIXTURE, status_code=200)
        with mock.patch.object(gc, "_get", return_value=resp):
            items = gc.fetch_note_com()
        # 两个 hashtag 返回同一 fixture，URL 去重后仅 1 条
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["source"], "note_com")
        self.assertEqual(it["title"], "攻略記事")
        self.assertEqual(it["author"], "writer")
        self.assertEqual(it["summary"], "本文サマリ")
        self.assertTrue(it["time"].startswith("2026-07-09"))
        self.assertEqual(it["engagement"], 0)  # RSS 无互动指标（已知限制）

    def test_request_failure_returns_empty(self):
        with mock.patch.object(gc, "_get", side_effect=RuntimeError("blocked")):
            self.assertEqual(gc.fetch_note_com(), [])


class TestFetchRuliweb(unittest.TestCase):
    def test_parses_search_results(self):
        html = (
            '<div id="board_search">'
            '<li class="search_result_item">'
            '<a class="title text_over" href="/best/board/300143/read/1">망각전야 공략</a>'
            '<span class="time">2026.06.19</span>'
            '<span class="desc">설명 텍스트</span>'
            '</li></div>'
        )
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text=html)):
            items = gc.fetch_ruliweb()
        self.assertTrue(items)
        it = items[0]
        self.assertEqual(it["source"], "ruliweb")
        self.assertEqual(it["url"], "https://bbs.ruliweb.com/best/board/300143/read/1")
        self.assertEqual(it["summary"], "설명 텍스트")

    def test_no_board_section(self):
        with mock.patch.object(gc, "_get_cf", return_value=FakeResp(text="<html></html>")):
            self.assertEqual(gc.fetch_ruliweb(), [])


class TestFetchStopgame(unittest.TestCase):
    def test_parses_rating_and_reviews(self):
        html = (
            '<div class="game-rating">8.5</div>'
            '<span>120 оценок</span>'
            '<time datetime="2026-06-19T00:00:00+00:00"></time>'
            '<div class="review-text">Отличная игра очень нравится</div>'
        )
        with mock.patch.object(gc, "_get", return_value=FakeResp(text=html)):
            items = gc.fetch_stopgame()
        self.assertTrue(items)
        rating_item = items[0]
        self.assertEqual(rating_item["source"], "stopgame")
        self.assertIn("8.5", rating_item["title"])
        self.assertEqual(rating_item["engagement"], 120)
        self.assertTrue(rating_item["is_hot"])  # 120 > 50

    def test_failure_returns_empty(self):
        with mock.patch.object(gc, "_get", side_effect=RuntimeError("down")):
            self.assertEqual(gc.fetch_stopgame(), [])


class TestFetchTaptap(unittest.TestCase):
    def test_import_error_returns_empty(self):
        # taptap_collector 不存在 → 两次 ImportError → 返回空
        with mock.patch.dict(sys.modules, {"taptap_collector": None}):
            self.assertEqual(gc.fetch_taptap(), [])


class TestFetchGooglePlay(unittest.TestCase):
    def test_import_error_returns_empty(self):
        with mock.patch.dict(sys.modules, {"google_play_scraper": None}):
            self.assertEqual(gc.fetch_google_play(), [])


class TestPostHelper(unittest.TestCase):
    def test_post_success(self):
        resp = FakeResp(json_data={"ok": 1})
        with mock.patch.object(gc.requests, "post", return_value=resp) as p:
            out = gc._post("https://x", json_data={"a": 1})
        self.assertIs(out, resp)
        p.assert_called_once()

    def test_post_retries_then_raises(self):
        with mock.patch.object(gc.requests, "post",
                               side_effect=gc.requests.RequestException("boom")), \
                mock.patch.object(gc.time, "sleep"):
            with self.assertRaises(gc.requests.RequestException):
                gc._post("https://x")


if __name__ == "__main__":
    unittest.main()
