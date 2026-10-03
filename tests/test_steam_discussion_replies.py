"""Steam 讨论区：全板块发现 + 回帖采集（离线，自造最小 HTML）。"""
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

import archive_layout
import archive_platforms
import collect_global
import global_collectors as gc
import news_common

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
APP = '3052450'
BASE = f'https://steamcommunity.com/app/{APP}'


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


def ts(hours_ago):
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


INDEX_HTML = f'''
<select class="responsive_tab_select">
<option value="{BASE}/discussions/" selected>x</option>
<option value="{BASE}/screenshots/" >x</option>
<option value="{BASE}/discussions/0/" class="">General Discussions</option>
<option value="{BASE}/eventcomments/"  class="">Events &amp; Announcements</option>
<option value="{BASE}/tradingforum/" class="">Trading</option>
</select>'''


def listing(path, topics):
    """topics: [(topic_id, 标题, 最后回复 hours_ago)]"""
    out = ['<div class="forum_area">']
    for tid, title, hours_ago in topics:
        out.append(
            f'<div class="forum_topic  unread" id="t{tid}">'
            f'<a class="forum_topic_overlay" href="{BASE}/{path}/{tid}/"></a>'
            f'<div class="forum_topic_op">楼主甲</div>'
            f'<div class="forum_topic_reply_count"><img src="x"> 3</div>'
            f'<div class="forum_topic_lastpost" data-timestamp="{ts(hours_ago)}">t</div>'
            f'<div class="forum_topic_name ">{title}</div></div>')
    return '\n'.join(out)


def comment(cid, name, mini, hours_ago, text):
    return (
        f'<div class="commentthread_comment responsive_body_text" id="comment_{cid}">'
        f'<div class="commentthread_comment_content"><div class="commentthread_comment_author">'
        f'<a class="hoverunderline commentthread_author_link" href="https://steamcommunity.com/profiles/7656{mini}" '
        f'data-miniprofile="{mini}"><bdi>{name}<span class="forum_author_action_pulldown"></span></bdi></a>'
        f'<div class="commentthread_comment_timestamp"></div>'
        f'<div class="commentthread_comment_timestamp" data-timestamp="{ts(hours_ago)}">x</div></div>'
        f'<div class="commentthread_comment_text" id="comment_content_{cid}">{text}</div>'
        f'<div class="forum_comment_permlink"><a href="#c{cid}">#1</a></div></div></div>')


def thread_page(tid, op_mini, op_text, comments, total, op_hours_ago=48):
    return (
        f'<div class="forum_op" id="forum_op_{tid}"><div class="forum_op_header">'
        f'<a class="hoverunderline forum_op_author" href="https://steamcommunity.com/id/opname" '
        f'data-miniprofile="{op_mini}">楼主甲<span class="x"></span></a>'
        f'<div class="date commentthread_comment_timestamp" data-timestamp="{ts(op_hours_ago)}">d</div></div>'
        f'<div class="topic" id="forum_op_topic_{tid}">标题</div>'
        f'<div class="content" id="forum_op_content_{tid}">{op_text}</div></div>'
        f'<script>InitializeCommentThread("ForumTopic", "x", {{"total_count":{total},"start":0,"pagesize":2}});</script>'
        + ''.join(comments))


class FakeSteam:
    """按 URL 路由的假 steamcommunity。pages: {url: html 或 Exception}"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(url)
        page = self.pages.get(url)
        if page is None:
            raise RuntimeError(f'404 {url}')
        if isinstance(page, Exception):
            raise page
        return Mock(text=page, raise_for_status=lambda: None)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(gc, 'datetime', FixedDatetime)
    monkeypatch.setattr(gc, 'HOURS_LOOKBACK', 24)
    monkeypatch.setattr(gc.time, 'sleep', lambda _: None)


def install(monkeypatch, pages):
    fake = FakeSteam(pages)
    monkeypatch.setattr(gc.requests, 'get', fake)
    return fake


def basic_pages():
    t1 = f'{BASE}/discussions/0/111/'
    return {
        f'{BASE}/discussions/': INDEX_HTML,
        f'{BASE}/discussions/0/': listing('discussions/0', [(111, '帖甲', 1), (222, '旧置顶', 900)]),
        f'{BASE}/eventcomments/': listing('eventcomments', [(333, '公告', 2)]),
        f'{BASE}/tradingforum/': '<div class="forum_area"></div>',
        # 帖 111：总 5 条回帖，每页 2 → 3 页；第 3 页最新
        t1: thread_page(111, 900, '首帖<br>全文<div class="bb_quote">引用<div>嵌套</div></div>尾', [
            comment(1001, '乙', 901, 100, '很久以前的回帖'),
            comment(1002, '丙', 902, 90, '很久以前的回帖二')], total=5),
        t1 + '?ctp=3': thread_page(111, 900, 'x', [
            comment(1005, '楼主甲', 900, 1, '楼主自己回复')], total=5),
        t1 + '?ctp=2': thread_page(111, 900, 'x', [
            comment(1003, '丁', 903, 30, '窗外回帖'),
            comment(1004, '戊', 904, 5, '窗内回帖<br>第二行')], total=5),
        f'{BASE}/eventcomments/333/': thread_page(333, '', '公告全文', [
            comment(3001, '己', 905, 1, '公告回帖')], total=1),
    }


def test_forum_discovery_and_fallback(monkeypatch):
    install(monkeypatch, {f'{BASE}/discussions/': INDEX_HTML})
    assert gc._steam_discover_forums(APP, {}) == [
        ('discussions/0', 'General Discussions'),
        ('eventcomments', 'Events & Announcements'),
        ('tradingforum', 'Trading'),
    ]
    # 首页没有下拉 / 请求失败 → 回落 General 一个
    install(monkeypatch, {f'{BASE}/discussions/': '<html></html>'})
    assert gc._steam_discover_forums(APP, {}) == gc.STEAM_FORUM_FALLBACK
    install(monkeypatch, {})
    assert gc._steam_discover_forums(APP, {}) == gc.STEAM_FORUM_FALLBACK


def test_thread_parser_fields():
    html = thread_page(111, 900, '甲<br>乙<div>引<div>深</div></div>丙', [
        comment(1001, '乙', 901, 3, '正文')], total=1)
    p = gc._steam_parse_thread_page(html)
    assert p.op['author'] == '楼主甲' and p.op['author_id'] == '900'
    assert p.op['profile'] == '/id/opname'
    assert p.op['text'].startswith('甲\n乙') and '深' in p.op['text'] and p.op['text'].endswith('丙')
    assert p.total == 1 and p.pagesize == 2
    c = p.comments[0]
    assert (c['id'], c['author'], c['author_id']) == ('1001', '乙', '901')
    assert c['time'] == NOW - timedelta(hours=3) and c['text'] == '正文'


def test_all_forums_and_replies(monkeypatch):
    fake = install(monkeypatch, basic_pages())
    items = gc._fetch_steam_discussions_one(APP, 'global', max_pages=1)
    threads = [i for i in items if i['metadata']['kind'] == 'thread']
    replies = [i for i in items if i['metadata']['kind'] == 'reply']
    # 旧置顶被跳过；General 一帖 + Events 一帖；Trading 为空
    assert sorted(t['url'] for t in threads) == [f'{BASE}/discussions/0/111/', f'{BASE}/eventcomments/333/']
    assert {t['metadata']['forum'] for t in threads} == {'General Discussions', 'Events & Announcements'}
    # 回帖只收时窗内的，且 1001/1002/1003 窗外不收
    assert sorted(r['url'] for r in replies) == [
        f'{BASE}/discussions/0/111/#c1004', f'{BASE}/discussions/0/111/#c1005',
        f'{BASE}/eventcomments/333/#c3001']
    r = next(r for r in replies if r['url'].endswith('#c1004'))
    assert r['author'] == '戊' and r['metadata']['author_id'] == '904'
    assert r['metadata']['thread_url'] == f'{BASE}/discussions/0/111/'
    assert r['metadata']['forum'] == 'General Discussions' and r['metadata']['is_op'] is False
    assert r['summary'] == '窗内回帖\n第二行'
    assert r['time'] == (NOW - timedelta(hours=5)).isoformat()
    op_reply = next(r for r in replies if r['url'].endswith('#c1005'))
    assert op_reply['metadata']['is_op'] is True
    # 公告帖楼主无作者 → 无人是楼主
    assert next(r for r in replies if r['url'].endswith('#c3001'))['metadata']['is_op'] is False
    # 从最后一页往前翻，遇窗外即止：不会去抓更早的页；第 1 页因取首帖必抓
    assert f'{BASE}/discussions/0/111/?ctp=2' in fake.calls
    assert not any('ctp=4' in c for c in fake.calls)


def test_op_body_backfill_keeps_identity(monkeypatch):
    monkeypatch.setattr(collect_global, 'datetime', FixedDatetime)
    pages = basic_pages()
    install(monkeypatch, pages)
    with_replies = gc._fetch_steam_discussions_one(APP, 'global', max_pages=1)
    install(monkeypatch, pages)
    list_only = gc._fetch_steam_discussions_one(APP, 'global', max_pages=1, fetch_replies=False)
    t_full = next(i for i in with_replies if i['url'] == f'{BASE}/discussions/0/111/')
    t_list = next(i for i in list_only if i['url'] == f'{BASE}/discussions/0/111/')
    assert '全文' in t_full['summary'] and '嵌套' in t_full['summary']
    assert t_full['created_at'] == (NOW - timedelta(hours=48)).isoformat()
    assert t_full['metadata']['author_id'] == '900'
    # 补全正文 / 创建时间不改去重键，也不改 time
    assert t_full['time'] == t_list['time']
    for fn in (collect_global.dedup_key, archive_platforms.item_key):
        assert fn(t_full) == fn(t_list)
    # 经校验与转换后键仍稳定，回帖锚点 url 与帖子不同键
    ok, cleaned = news_common.validate_news_item(t_full)
    assert ok
    conv = collect_global.convert_item(cleaned)
    assert collect_global.dedup_key(conv) == collect_global.dedup_key(t_list)
    reply = next(i for i in with_replies if i['url'].endswith('#c1004'))
    ok, rc = news_common.validate_news_item(reply)
    assert ok and rc['url'].endswith('#c1004')
    rconv = collect_global.convert_item(rc)
    assert rconv['metadata']['is_op'] is False and rconv['url'] == reply['url']
    assert collect_global.dedup_key(rconv) != collect_global.dedup_key(conv)
    assert len(collect_global.merge_and_dedup([], [conv, rconv, conv])) == 2


def test_archive_location_same_subtype():
    # 回帖与帖子同源同落点 steam/<区服>/discussion
    assert archive_layout.resolve_write_layout('steam_discussion', 'global', 'discussion') == (
        'steam', 'global', 'discussion')


def test_breaker_and_degrade(monkeypatch):
    topics = [(n, f'帖{n}', 1) for n in range(100, 106)]
    pages = {
        f'{BASE}/discussions/': INDEX_HTML.replace('eventcomments', 'x').replace('tradingforum', 'y'),
        f'{BASE}/discussions/0/': listing('discussions/0', topics),
    }
    for n, _, _ in topics:
        pages[f'{BASE}/discussions/0/{n}/'] = RuntimeError('boom')
    fake = install(monkeypatch, pages)
    items = gc._fetch_steam_discussions_one(APP, 'global', max_pages=1)
    # 列表数据完整保留（预览不丢），帖子页只试了熔断阈值次
    assert len(items) == 6 and all(i['metadata']['kind'] == 'thread' for i in items)
    thread_calls = [c for c in fake.calls if c.startswith(f'{BASE}/discussions/0/1')]
    assert len(thread_calls) == gc.STEAM_FAIL_BREAKER


def test_request_cap(monkeypatch):
    topics = [(n, f'帖{n}', 1) for n in range(100, 106)]
    pages = {
        f'{BASE}/discussions/': INDEX_HTML.replace('eventcomments', 'x').replace('tradingforum', 'y'),
        f'{BASE}/discussions/0/': listing('discussions/0', topics),
    }
    for n, _, _ in topics:
        pages[f'{BASE}/discussions/0/{n}/'] = thread_page(n, 900, '正文', [], total=0)
    fake = install(monkeypatch, pages)
    monkeypatch.setattr(gc, 'STEAM_MAX_REQUESTS_PER_RUN', 2)
    items = gc._fetch_steam_discussions_one(APP, 'global', max_pages=1)
    assert len(items) == 6
    assert len([c for c in fake.calls if '/discussions/0/1' in c]) == 2
