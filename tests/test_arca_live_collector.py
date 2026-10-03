"""arca.live 采集器离线测试：全部网络 mock，HTML 为自造最小样本（无真实帖子与用户名）。"""
import unittest
from datetime import datetime, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401

import arca_live_collector as ac
import archive_layout
import archive_platforms
import collect_global

CUTOFF = datetime(2026, 10, 1, tzinfo=UTC)


def _row(post_id, title, author, time_iso, views=10, rate=1, comments=2, notice=False, badge=""):
    cls = "vrow column notice notice-board" if notice else "vrow column"
    if notice:
        return (f'<a class="{cls}" href="/b/forgettingeve/{post_id}?p=1"><div class="vrow-inner">'
                f'<div class="vrow-top"><span class="vcol col-id"><b>공지</b></span>'
                f'<span class="vcol col-title"><b>{title}</b></span></div>'
                f'<div class="vrow-bottom"><span class="vcol col-author"><span class="user-info ">'
                f'<span data-filter="{author}">{author}</span></span></span>'
                f'<span class="vcol col-time"><time datetime="{time_iso}">x</time></span>'
                f'<span class="vcol col-view">{views}</span><span class="vcol col-rate">0</span>'
                f'</div></div></a>')
    b = f'<span class="badge text-bg-success">{badge}</span>' if badge else ""
    return (f'<a class="{cls}" href="/b/forgettingeve/{post_id}?p=1" ><div class="vrow-inner">'
            f'<div class="vrow-top"><span class="vcol col-id"><span>1</span></span>'
            f'<span class="vcol col-title"><span class="badges">{b}</span>'
            f'<span class="title"><span class="media-icon bi-images"></span> {title} </span>'
            f'<span class="info"><span class="comment-count">[{comments}]</span></span></span></div>'
            f'<div class="vrow-bottom"><span class="vcol col-author"><span class="user-info ">'
            f'<span data-filter="{author}">{author}</span></span></span>'
            f'<span class="vcol col-time"><time datetime="{time_iso}">x</time></span>'
            f'<span class="vcol col-view">{views}</span><span class="vcol col-rate">{rate}</span>'
            f'</div></div></a>')


LIST_HTML = ('<html><body>' + '<div class="vrow column head d-none d-md-flex">h</div>'
             + _row(1, "公告测试", "admin_a", "2025-01-01T00:00:00.000Z", notice=True)
             + _row(200, "新帖一", "nickA", "2026-10-02T10:00:00.000Z", views=1500, rate=3, comments=4, badge="情报")
             + _row(201, "新帖二", "nickB#12345", "2026-10-02T11:00:00.000Z")
             + _row(100, "旧帖", "nickC", "2026-09-01T00:00:00.000Z")
             + "x" * 20000 + '</body></html>')

ARTICLE_HTML = ('<html><body><div class="article-head"><div class="member-info">'
                '<span class="user-info "><a href="/u/@nickA%2F99" data-filter="nickA">nickA</a></span>'
                '<div class="avatar"><img src="a.jpg"></div></div></div>'
                '<div class="fr-view article-content"><p>第一行<b>粗体</b></p><p>第二&amp;行</p>'
                '<div>内层</div><script>bad()</script></div><div class="comments">评论不要</div>'
                + "x" * 20000 + '</body></html>')

CHALLENGE_HTML = '<html><head><title>Just a moment...</title></head><body>cf-chl challenge</body></html>'


class FakeResp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.handler(url)


class ParseTests(unittest.TestCase):
    def test_parse_list_rows_and_fields(self):
        rows = ac.parse_list(LIST_HTML)
        self.assertEqual([r["id"] for r in rows], ["1", "200", "201", "100"])
        self.assertTrue(rows[0]["is_notice"])
        r = rows[1]
        self.assertEqual(r["url"], "https://arca.live/b/forgettingeve/200")  # 去掉 ?p=1
        self.assertEqual((r["title"], r["tag"], r["author_raw"]), ("新帖一", "情报", "nickA"))
        self.assertEqual((r["views"], r["rate"], r["comments"]), (1500, 3, 4))
        self.assertEqual(r["time"], "2026-10-02T10:00:00.000Z")

    def test_parse_article_body_and_author_id(self):
        body, author_id = ac.parse_article(ARTICLE_HTML)
        self.assertEqual(body, "第一行粗体\n第二&行\n内层")
        self.assertNotIn("评论不要", body)
        self.assertNotIn("bad()", body)
        self.assertEqual(author_id, "@nickA/99")

    def test_parse_article_empty_and_truncate(self):
        self.assertEqual(ac.parse_article("<html></html>"), ("", ""))
        self.assertEqual(ac.parse_article(None), ("", ""))
        big = '<div class="fr-view article-content">' + "字" * 5000 + "</div>"
        self.assertEqual(len(ac.parse_article(big)[0]), ac.ARCA_BODY_MAX_CHARS)

    def test_split_author(self):
        self.assertEqual(ac.split_author("nickB#12345"), ("nickB", "12345"))
        self.assertEqual(ac.split_author("nickA"), ("nickA", ""))

    def test_challenge_detection(self):
        self.assertTrue(ac.is_challenge_page(CHALLENGE_HTML))
        self.assertTrue(ac.is_challenge_page("", "article"))
        self.assertFalse(ac.is_challenge_page(LIST_HTML))
        self.assertFalse(ac.is_challenge_page(ARTICLE_HTML, "article"))
        # 正常页里的 captcha / turnstile 配置字样不算挑战
        self.assertFalse(ac.is_challenge_page(LIST_HTML + "recaptcha turnstile"))


class BuildTests(unittest.TestCase):
    def test_window_notice_and_author(self):
        items = ac.build_items(ac.parse_list(LIST_HTML), CUTOFF)
        by = {i["title"]: i for i in items}
        self.assertNotIn("旧帖", by)  # 时窗外
        self.assertNotIn("公告测试", by)  # 2025 年公告在时窗外
        self.assertEqual(by["新帖一"]["author"], "nickA")
        self.assertEqual(by["新帖二"]["author"], "nickB")
        self.assertEqual(by["新帖二"]["metadata"]["author_id"], "nickB#12345")
        self.assertEqual(by["新帖一"]["time"], "2026-10-02T10:00:00.000Z")
        self.assertEqual((by["新帖一"]["source"], by["新帖一"]["platform_region"], by["新帖一"]["lang"]),
                         ("arca_live", "kr", "ko"))
        self.assertTrue(by["新帖一"]["is_hot"])
        self.assertEqual(by["新帖一"]["tags"], ["情报"])

    def test_notice_in_window_marked(self):
        items = ac.build_items(ac.parse_list(LIST_HTML), datetime(2020, 1, 1, tzinfo=UTC))
        notice = [i for i in items if i["title"] == "公告测试"][0]
        self.assertTrue(notice["metadata"]["is_notice"])


class FetchTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ac.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_full_flow_fills_body_skips_notice(self):
        def handler(url):
            if url == ac.ARCA_LIST_URL:
                return FakeResp(LIST_HTML)
            return FakeResp(ARTICLE_HTML)
        s = FakeSession(handler)
        items = ac.fetch_arca_live(cutoff=datetime(2020, 1, 1, tzinfo=UTC), session=s)
        urls = [c[0] for c in s.calls]
        self.assertNotIn("https://arca.live/b/forgettingeve/1", urls)  # 公告不补抓
        self.assertIn("User-Agent", s.calls[0][1]["headers"])
        by = {i["title"]: i for i in items}
        self.assertEqual(by["新帖一"]["summary"], "第一行粗体\n第二&行\n内层")
        self.assertEqual(by["新帖一"]["metadata"]["author_id"], "@nickA/99")
        self.assertEqual(by["公告测试"]["summary"], "")

    def test_list_challenge_degrades_to_empty(self):
        for resp in (FakeResp(CHALLENGE_HTML), FakeResp("", 403)):
            s = FakeSession(lambda url, r=resp: r)
            self.assertEqual(ac.fetch_arca_live(cutoff=CUTOFF, session=s), [])

    def test_list_exception_returns_empty(self):
        def boom(url):
            raise OSError("down")
        self.assertEqual(ac.fetch_arca_live(cutoff=CUTOFF, session=FakeSession(boom)), [])

    def test_body_circuit_breaker_and_degrade(self):
        rows = "".join(_row(300 + i, f"帖{i}", "u", "2026-10-02T10:00:00.000Z") for i in range(8))
        page = "<html>" + rows + "x" * 20000 + "</html>"

        def handler(url):
            return FakeResp(page) if url == ac.ARCA_LIST_URL else FakeResp(CHALLENGE_HTML)
        s = FakeSession(handler)
        items = ac.fetch_arca_live(cutoff=CUTOFF, session=s)
        self.assertEqual(len(items), 8)  # 列表结果不丢
        self.assertEqual(len(s.calls), 1 + ac.ARCA_BODY_MAX_CONSEC_FAIL)  # 连续失败熔断
        failed = [i for i in items if i["metadata"].get("body_fetch_failed")]
        self.assertEqual(len(failed), ac.ARCA_BODY_MAX_CONSEC_FAIL)
        self.assertTrue(all(i["summary"] == "" and i["metadata"]["challenge"] for i in failed))

    def test_body_cap_per_run(self):
        rows = "".join(_row(400 + i, f"帖{i}", "u", "2026-10-02T10:00:00.000Z") for i in range(20))
        page = "<html>" + rows + "x" * 20000 + "</html>"
        s = FakeSession(lambda url: FakeResp(page) if url == ac.ARCA_LIST_URL else FakeResp(ARTICLE_HTML))
        ac.fetch_arca_live(cutoff=CUTOFF, session=s)
        self.assertEqual(len(s.calls), 1 + ac.ARCA_BODY_MAX_PER_RUN)


class DedupAndArchiveTests(unittest.TestCase):
    def _item(self, query=""):
        rows = ac.parse_list(LIST_HTML.replace("?p=1", query))
        return ac.build_items(rows, CUTOFF)[0]

    def test_dedup_keys_stable_across_query_params(self):
        a, b = self._item("?p=1"), self._item("?p=2&foo=bar")
        self.assertEqual(collect_global.dedup_key(a), collect_global.dedup_key(b))
        self.assertEqual(archive_platforms.item_key(a), archive_platforms.item_key(b))
        self.assertEqual(collect_global.dedup_key(a), "https://arca.live/b/forgettingeve/200")
        self.assertEqual(archive_platforms.item_key(a), "https://arca.live/b/forgettingeve/200")

    def test_archive_placement_same_dir_as_history(self):
        item = self._item()
        self.assertEqual(archive_layout.resolve_write_layout("arca_live"), ("arca_live", None, None))
        self.assertNotIn("region", item)
        self.assertNotIn("archive_subtype", item)
        rel = archive_layout.build_relpath("arca_live", None, None, "2026-10-03")
        self.assertEqual(rel, Path("arca_live/2026-10-03.json"))

    def test_registered_as_normal_source(self):
        import sources
        self.assertIn("arca_live", sources.KNOWN_SOURCES)
        self.assertIn("arca_live", sources.ARCHIVE_PLATFORMS)
        self.assertNotIn("arca_live", sources.LEGACY_SOURCES)


if __name__ == "__main__":
    unittest.main()
