"""T111 TapTap 评价采集：解析、字段落点、去重键、翻页 / 时窗 / 失败降级。全部离线，样本自造。"""
from datetime import datetime, timedelta, UTC
from unittest import mock

import _paths  # noqa: F401
import archive_layout
import archive_platforms
import collect_global
import global_collectors as gc
import sources
import taptap_collector as tc

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _entry(rid, ts, text='自造评价正文<br />第二行', score=5, name='自造昵称', uid=1001, supports=3):
    return {'type': 'moment', 'moment': {
        'id_str': f'9{rid}', 'created_time': ts, 'publish_time': ts,
        'author': {'user': {'id': uid, 'name': name}},
        'device': '自造设备',
        'review': {'id': rid, 'score': score, 'contents': {'text': text}},
        'stat': {'supports': supports, 'pv_total': 9}}}


def _resp(entries, next_page='/x'):
    r = mock.Mock()
    r.json.return_value = {'success': True, 'data': {'list': entries, 'next_page': next_page}}
    return r


def _ts(hours_ago):
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


def test_parse_fields_and_layout():
    item = tc.parse_review(_entry(5001, _ts(1)), '364992')
    assert item['url'] == 'https://www.taptap.cn/review/5001'
    assert item['author'] == '自造昵称'
    assert item['metadata']['author_id'] == '1001'
    assert item['metadata']['app_id'] == '364992'
    assert item['summary'] == '自造评价正文\n第二行'
    assert item['title'].startswith('[TapTap 好评] 自造评价正文')
    assert item['engagement'] == 3
    assert item['source'] == 'taptap_review' and item['lang'] == 'zh'
    assert item['time'] == (NOW - timedelta(hours=1)).isoformat()
    # 落点：taptap/cn/review/，与历史档同目录
    plat, region, sub = archive_layout.resolve_write_layout(
        item['source'], item['region'], item['archive_subtype'])
    assert (plat, region, sub) == ('taptap', 'cn', 'review')


def test_parse_skips_invalid_and_score_labels():
    assert tc.parse_review({'moment': {'review': {}}}, '1') is None
    assert tc.parse_review({'type': 'x'}, '1') is None
    assert tc.parse_review(_entry(1, 0), '1') is None  # 无时间
    assert '差评' in tc.parse_review(_entry(2, _ts(1), score=1), '1')['title']
    no_name = tc.parse_review(_entry(3, _ts(1), name='', uid=0), '1')
    assert no_name['author'] == '' and 'author_id' not in no_name['metadata']
    assert no_name['metadata']['author_is_unknown'] is True


def test_dedup_keys_stable_across_author_and_title_changes():
    a = tc.parse_review(_entry(7001, _ts(2)), '364992')
    b = tc.parse_review(_entry(7001, _ts(2), name='改名后', text='编辑后正文'), '374995')
    assert collect_global.dedup_key(a) == collect_global.dedup_key(b) == 'https://www.taptap.cn/review/7001'
    assert archive_platforms.item_key(a) == archive_platforms.item_key(b)


def test_window_and_pagination_stop():
    cutoff = NOW - timedelta(hours=24)
    pages = [_resp([_entry(1, _ts(1)), _entry(2, _ts(2))]),
             _resp([_entry(3, _ts(3)), _entry(4, _ts(30))]),
             _resp([_entry(5, _ts(40)), _entry(6, _ts(41))]),  # 整页过旧 → 停
             _resp([_entry(7, _ts(1))])]
    getter = mock.Mock(side_effect=pages)
    got = tc._fetch_app('364992', cutoff, getter, lambda s: None)
    assert [i['url'].rsplit('/', 1)[1] for i in got] == ['1', '2', '3']
    assert getter.call_count == 3
    assert 'from=2' in getter.call_args_list[1][0][0]
    assert 'X-UA=V%3D1' in getter.call_args_list[0][0][0]


def test_max_pages_and_delay_between_requests():
    cutoff = NOW - timedelta(hours=24)
    getter = mock.Mock(side_effect=lambda url: _resp([_entry(getter.call_count, _ts(1))]))
    sleeps = []
    with mock.patch.object(tc, 'MAX_PAGES', 2):
        tc._fetch_app('364992', cutoff, getter, sleeps.append)
    assert getter.call_count == 2 and sleeps == [tc.REQUEST_DELAY]


def test_one_app_failure_degrades():
    cutoff = NOW - timedelta(hours=24)

    def getter(url):
        if 'app_id=364992' in url:
            raise RuntimeError('boom')
        return _resp([_entry(11, _ts(1))], next_page='')

    got = tc.fetch_taptap_reviews(cutoff, getter, sleep=lambda s: None)
    assert [i['metadata']['app_id'] for i in got] == ['374995']


def test_all_apps_fail_raises():
    def getter(url):
        raise RuntimeError('boom')
    try:
        tc.fetch_taptap_reviews(NOW - timedelta(hours=24), getter, sleep=lambda s: None)
    except RuntimeError:
        return
    raise AssertionError('两应用全败应抛错交编排层记失败')


def test_bad_payload_is_failure_not_crash():
    bad = mock.Mock()
    bad.json.return_value = {'success': False}
    got = tc.fetch_taptap_reviews(NOW - timedelta(hours=24),
                                  mock.Mock(side_effect=[bad, _resp([_entry(21, _ts(1))], '')]),
                                  sleep=lambda s: None)
    assert len(got) == 1


def test_registration():
    assert 'taptap_review' in sources.KNOWN_SOURCES
    assert 'taptap_review' in sources.ARCHIVE_PLATFORMS
    assert collect_global.SOURCE_MAP['taptap_review'] == 'taptap_review'
    assert callable(gc.fetch_taptap_reviews)
    assert 'taptap' in sources.KNOWN_SOURCES and 'taptap' in sources.ARCHIVE_PLATFORMS
    assert 'taptap' in sources.SPARSE_SOURCES
    assert collect_global.SOURCE_MAP['taptap'] == 'taptap'
    assert callable(gc.fetch_taptap_posts) and callable(gc.fetch_taptap_io_reviews)


# ───────────────────────── 论坛帖子 + 回复（自造样本）─────────────────────────

def _post_entry(mid, ts, commented=None, title='自造帖标题', summary='自造摘要', comments=2,
                ups=4, name='自造楼主', uid=2001, tid=None, labels=('综合',)):
    return {'type': 'moment', 'moment': {
        'id_str': str(mid), 'created_time': ts, 'publish_time': ts,
        'commented_time': commented if commented is not None else ts,
        'author': {'user': {'id': uid, 'name': name}},
        'topic': {'id_str': str(tid or mid + 1), 'title': title, 'summary': summary},
        'stat': {'ups': ups, 'comments': comments, 'pv_total': 77},
        'labels': [{'name': n} for n in labels]}}


def _json_resp(data):
    r = mock.Mock()
    r.json.return_value = {'success': True, 'data': data}
    return r


def _detail(text):
    return _json_resp({'first_post': {'contents': {'json': [
        {'type': 'paragraph', 'children': [{'text': text}]},
        {'type': 'paragraph', 'children': [{'text': '第二段'}]}]}}})


def _reply(rid, ts, text='自造回复', name='自造回复者', uid=3001, child=()):
    return {'id': rid, 'position': 2, 'created_time': ts, 'ups': 1, 'comments': len(child),
            'contents': {'text': text + '<img alt="x" src="y">'},
            'author': {'id': uid, 'name': name}, 'child_posts': list(child)}


def _forum_getter(feed_pages, replies=None, detail_text='自造正文', fail=()):
    """按 URL 路由的假 getter：by-group 依次吐 feed_pages，detail 吐固定正文，by-topic 吐 replies。"""
    pages = iter(feed_pages)
    seen = []

    def getter(url):
        seen.append(url)
        for key in fail:
            if key in url:
                raise RuntimeError('boom')
        if '/feed/v7/by-group' in url:
            return next(pages)
        if '/moment/v3/detail' in url:
            return _detail(detail_text)
        if '/post/v3/by-topic' in url:
            return _json_resp({'list': replies or [], 'next_page': ''})
        raise AssertionError(url)
    getter.seen = seen
    return getter


def test_parse_post_fields_and_layout():
    item = tc.parse_post(_post_entry(8001, _ts(1)), '594490')
    assert item['url'] == 'https://www.taptap.cn/moment/8001'
    assert item['source'] == 'taptap' and item['title'] == '自造帖标题'
    assert item['author'] == '自造楼主' and item['metadata']['author_id'] == '2001'
    assert item['metadata']['comments_count'] == 2 and item['metadata']['views_count'] == 77
    assert item['metadata']['app_id'] == '364992' and item['tags'] == ['综合']
    assert item['engagement'] == 6
    plat, region, sub = archive_layout.resolve_write_layout(
        item['source'], item['region'], item['archive_subtype'])
    assert (plat, region, sub) == ('taptap', 'cn', 'post')
    assert tc.parse_post({'moment': {'topic': {}}}, '1') is None
    assert tc.parse_post(_post_entry(1, 0), '1') is None


def test_slate_body_and_truncation():
    detail = _detail('正文' * 1500).json.return_value
    body = tc._post_body(detail, 'fallback')
    assert body.startswith('正文') and len(body) == tc.BODY_LIMIT
    assert tc._post_body({}, '摘要') == '摘要'


def test_post_dedup_keys_stable():
    a = tc.parse_post(_post_entry(8100, _ts(2)), '594490')
    b = tc.parse_post(_post_entry(8100, _ts(2), title='编辑后', name='改名'), '612611')
    assert collect_global.dedup_key(a) == collect_global.dedup_key(b) == 'https://www.taptap.cn/moment/8100'
    assert archive_platforms.item_key(a) == archive_platforms.item_key(b)


def test_group_posts_replies_and_window():
    cutoff = NOW - timedelta(hours=24)
    child = _reply(51, _ts(1), text='楼中楼')
    feed = [_json_resp({'list': [
                _post_entry(8200, _ts(2), commented=_ts(1), tid=9200),
                _post_entry(8201, _ts(3), comments=0, tid=9201),
                _post_entry(8202, _ts(60), commented=_ts(1), tid=9202)],  # 旧帖但有新回复
            'next_page': '/x'}),
            _json_resp({'list': [_post_entry(8203, _ts(80), commented=_ts(70), tid=9203)],
                        'next_page': ''})]
    replies = [_reply(50, _ts(1), child=[child]), _reply(49, _ts(40), text='旧回复')]
    getter = _forum_getter(feed, replies)
    got = tc._fetch_group('594490', cutoff, getter, lambda s: None)
    posts = [i for i in got if i['metadata']['kind'] == 'post']
    reps = [i for i in got if i['metadata']['kind'] == 'reply']
    assert [p['url'].rsplit('/', 1)[1] for p in posts] == ['8200', '8201']
    assert posts[0]['summary'] == '自造正文\n第二段'
    # 8200 与旧帖 8202 各取一次回复（旧回复被 cutoff 滤掉）；回复 url 带稳定 #post-id
    assert len(reps) == 4 and {r['metadata']['parent'] for r in reps} == {
        'https://www.taptap.cn/moment/8200', 'https://www.taptap.cn/moment/8202'}
    assert reps[0]['url'] == 'https://www.taptap.cn/moment/8200#post-50'
    assert reps[1]['metadata']['parent_reply_id'] == '50' and reps[0]['summary'] == '自造回复'
    assert all(r['source'] == 'taptap' and r['archive_subtype'] == 'post' for r in reps)
    assert len({collect_global.dedup_key(i) for i in got}) == len(got)
    # 第二页整页过旧 → 停；没有第三次 by-group
    assert sum('/feed/v7/by-group' in u for u in getter.seen) == 2


def test_replies_per_post_cap():
    cutoff = NOW - timedelta(hours=24)
    feed = [_json_resp({'list': [_post_entry(8300, _ts(1), tid=9300)], 'next_page': ''})]
    replies = [_reply(100 + n, _ts(1)) for n in range(10)]
    with mock.patch.object(tc, 'MAX_REPLIES_PER_POST', 3):
        got = tc._fetch_group('594490', cutoff, _forum_getter(feed, replies), lambda s: None)
    assert len([i for i in got if i['metadata']['kind'] == 'reply']) == 3


def test_detail_and_reply_failures_degrade():
    cutoff = NOW - timedelta(hours=24)
    feed = [_json_resp({'list': [_post_entry(8400, _ts(1), summary='列表摘要', tid=9400)],
                        'next_page': ''})]
    getter = _forum_getter(feed, [_reply(1, _ts(1))], fail=('/moment/v3/detail', '/post/v3/by-topic'))
    got = tc._fetch_group('594490', cutoff, getter, lambda s: None)
    assert len(got) == 1 and got[0]['summary'] == '列表摘要'
    assert got[0]['metadata']['comments_count'] == 2  # 回复没采到时仍记计数


def test_forum_failure_modes():
    cutoff = NOW - timedelta(hours=24)

    def getter(url):
        if 'group_id=594490' in url:
            raise RuntimeError('boom')
        if '/feed/v7/by-group' in url:
            return _json_resp({'list': [_post_entry(8500, _ts(1), comments=0)], 'next_page': ''})
        return _detail('x')

    got = tc.fetch_taptap_posts(cutoff, getter, sleep=lambda s: None)
    assert [i['metadata']['group_id'] for i in got] == ['612611']

    def dead(url):
        raise RuntimeError('boom')
    try:
        tc.fetch_taptap_posts(cutoff, dead, sleep=lambda s: None)
    except RuntimeError:
        return
    raise AssertionError('两论坛全败应抛错')


def test_discover_group_id():
    html = ('<a href="/group/594490/info">a</a><a href="/group/594490/info">b</a>'
            '<a href="/group/111/info">c</a>')
    r = mock.Mock(text=html)
    assert tc.discover_group_id('364992', lambda url: r) == '594490'
    assert tc.discover_group_id('1', lambda url: mock.Mock(text='无')) is None


# ───────────────────────── 国际版评价（taptap.io，自造样本）─────────────────────────

def test_io_review_parse_layout_and_lang():
    entry = _entry(6001, _ts(1), text='Self made review')
    entry['moment']['review']['language'] = 'en_US'
    item = tc.parse_review(entry, '33875920', tc.SITE_IO)
    assert item['url'] == 'https://www.taptap.io/review/6001'
    assert item['metadata']['lang'] == 'en_US' and item['lang'] == 'en'
    assert item['region'] == 'global' and item['platform_region'] == 'global'
    assert item['source'] == 'taptap_review' and item['metadata']['app_label'] == 'taptap.io'
    plat, region, sub = archive_layout.resolve_write_layout(
        item['source'], item['region'], item['archive_subtype'])
    assert (plat, region, sub) == ('taptap', 'global', 'review')
    # 与国服同号评价 URL 域名不同，去重键互不碰撞
    cn = tc.parse_review(_entry(6001, _ts(1)), '364992')
    assert archive_platforms.item_key(item) != archive_platforms.item_key(cn)
    plain = tc.parse_review(_entry(6002, _ts(1)), '33875920', tc.SITE_IO)
    assert plain['metadata']['lang'] == 'en'


def test_io_fetch_pagination_and_degrade():
    cutoff = NOW - timedelta(hours=24)
    getter = mock.Mock(side_effect=[_resp([_entry(1, _ts(1)), _entry(2, _ts(2))]),
                                    _resp([_entry(3, _ts(50))])])
    got = tc.fetch_taptap_io_reviews(cutoff, getter, sleep=lambda s: None)
    assert [i['url'].rsplit('/', 1)[1] for i in got] == ['1', '2']
    assert getter.call_args_list[0][0][0].startswith('https://www.taptap.io/webapiv2/review/v2/list-by-app?')
    assert 'LANG%3Den_US' in getter.call_args_list[0][0][0]

    def http500(url):
        raise RuntimeError('500')
    assert tc.fetch_taptap_io_reviews(cutoff, http500, sleep=lambda s: None) == []  # 已知降级：不抛错
    assert tc.fetch_taptap_io_reviews(cutoff, lambda u: _resp([]), sleep=lambda s: None) == []
