"""B 站视频评论采集：离线（mock http，样本全部自造，不含真实用户名与评论）。

覆盖：aid/bvid 换算、评论解析、楼中楼、风控码降级、cutoff、去重键、落点与登记、归档取视频。
"""
import json
from datetime import datetime, timedelta, UTC

import pytest

import _paths  # noqa: F401
import archive_layout
import bilibili_comments_collector as bc
import collect_global
import global_collectors as gc
import news_common
import sources

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)
AID = 900000000001
BVID = bc.av2bv(AID)


def _ts(hours_ago):
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


def _r(rpid, hours_ago=1, msg=None, mid=111, rcount=0, parent=0, root=0, like=3, uname='用户甲'):
    return {'rpid': rpid, 'mid': mid, 'ctime': _ts(hours_ago), 'like': like, 'rcount': rcount,
            'parent': parent, 'root': root, 'member': {'uname': uname, 'mid': str(mid)},
            'content': {'message': f'评论正文{rpid}' if msg is None else msg}}


def _main(replies, is_end=True, nxt=0, top=None, code=0):
    return {'code': code, 'data': {'replies': replies, 'top_replies': top or [],
                                   'cursor': {'is_end': is_end, 'next': nxt}}}


def _search(*videos):
    return {'code': 0, 'data': {'result': [
        {'aid': a, 'bvid': b, 'title': f'<em>忘却前夜</em>视频{a}', 'pubdate': _ts(2),
         'play': 100, 'danmaku': 5, 'arcurl': f'http://www.bilibili.com/video/av{a}'}
        for a, b in videos]}}


class Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class FakeHttp:
    """按 URL 路由；handlers[url] 可为 dict / list（逐次弹出）/ callable(params)。"""

    def __init__(self, handlers):
        self.handlers, self.calls = handlers, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        h = self.handlers.get(url)
        if callable(h):
            h = h(params)
        elif isinstance(h, list):
            h = h.pop(0)
        if isinstance(h, Resp):
            return h
        return Resp(h if h is not None else {'code': 0, 'data': {}})


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news_common, 'bilibili_spi_cookies', lambda *a, **k: {})
    monkeypatch.setattr(news_common, 'get_wbi_mixin_key', lambda *a, **k: None)


def _run(handlers, archive_dir, cutoff=CUTOFF, sleeps=None, keywords=('忘却前夜',)):
    http = FakeHttp(handlers)
    items = bc.fetch_bilibili_comments(
        cutoff, http=http, sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        keywords=list(keywords), archive_dir=archive_dir, now=NOW)
    return items, http


def _plain(handlers_main, tmp_path, **kw):
    handlers = {bc.SEARCH_URL: _search((AID, BVID)), bc.REPLY_MAIN_URL: handlers_main}
    return _run(handlers, tmp_path, **kw)


# ── aid / bvid ──
def test_av_bv_roundtrip_known_pair():
    assert bc.av2bv(170001) == 'BV17x411w7KC'
    assert bc.bv2av('BV17x411w7KC') == 170001
    assert bc.bv2av(bc.av2bv(AID)) == AID


def test_video_ids_from_url():
    assert bc.video_ids_from_url(f'https://www.bilibili.com/video/av{AID}') == (AID, BVID)
    assert bc.video_ids_from_url(f'https://www.bilibili.com/video/{BVID}/') == (AID, BVID)
    assert bc.video_ids_from_url('https://example.com/x') == (None, None)


# ── 解析 ──
def test_parse_fields_and_url(tmp_path):
    items, http = _plain(_main([_r(1001, like=60)]), tmp_path)
    assert len(items) == 1
    it = items[0]
    assert it['source'] == 'bilibili_comment'
    assert it['url'] == f'https://www.bilibili.com/video/{BVID}#reply1001'
    assert it['author'] == '用户甲' and it['summary'] == '评论正文1001'
    assert it['time'] == (NOW - timedelta(hours=1)).isoformat()
    assert it['is_hot'] is True and it['engagement'] == 60
    m = it['metadata']
    assert m == {'author_id': '111', 'video_aid': str(AID), 'video_bvid': BVID, 'parent_rpid': '',
                 'like': 60, 'reply_count': 0, 'is_reply': False}
    assert (it['region'], it['archive_subtype']) == ('cn', 'comment')
    url, params, headers = http.calls[1]
    assert url == bc.REPLY_MAIN_URL
    assert (params['type'], params['oid'], params['mode']) == (1, AID, 2)
    assert headers['Referer'] == 'https://www.bilibili.com/' and 'Mozilla' in headers['User-Agent']


def test_empty_body_and_malformed_skipped(tmp_path):
    bad = {'ctime': _ts(1), 'content': {'message': 'x'}}  # 缺 rpid
    items, _ = _plain(_main([_r(1, msg='   '), bad, _r(2)]), tmp_path)
    assert [i['url'].rsplit('#reply', 1)[1] for i in items] == ['2']


# ── cutoff ──
def test_cutoff_drops_old_comments_and_stops_paging(tmp_path):
    pages = [_main([_r(3, 1), _r(2, 30)], is_end=False, nxt=2)]
    items, http = _plain(pages, tmp_path)
    assert [i['metadata']['like'] for i in items] == [3]
    assert sum(1 for c in http.calls if c[0] == bc.REPLY_MAIN_URL) == 1  # 页内有时窗外且无下一页需求


def test_paging_follows_cursor_when_all_recent(tmp_path):
    pages = [_main([_r(5, 1), _r(4, 2)], is_end=False, nxt=2), _main([_r(3, 3)], is_end=True)]
    items, http = _plain(pages, tmp_path)
    assert len(items) == 3
    main_calls = [c for c in http.calls if c[0] == bc.REPLY_MAIN_URL]
    assert [c[1]['next'] for c in main_calls] == [0, 2]


def test_page_limit(tmp_path):
    pages = [_main([_r(10 + i, 1)], is_end=False, nxt=i + 1) for i in range(5)]
    items, http = _plain(pages, tmp_path)
    assert sum(1 for c in http.calls if c[0] == bc.REPLY_MAIN_URL) == bc.MAX_PAGES_PER_VIDEO


# ── 楼中楼 ──
def test_subreplies_expanded_with_parent_link(tmp_path):
    root = _r(100, 2, rcount=2)
    sub = {'code': 0, 'data': {'replies': [_r(101, 1, parent=100, root=100), _r(102, 1, parent=101, root=100)],
                               'page': {'num': 1, 'size': 20, 'count': 2}}}
    handlers = {bc.SEARCH_URL: _search((AID, BVID)), bc.REPLY_MAIN_URL: _main([root]), bc.REPLY_SUB_URL: sub}
    items, http = _run(handlers, tmp_path)
    by = {i['url'].rsplit('#reply', 1)[1]: i for i in items}
    assert set(by) == {'100', '101', '102'}
    assert by['100']['metadata']['is_reply'] is False and by['100']['metadata']['reply_count'] == 2
    assert by['101']['metadata'] | {} and by['101']['metadata']['is_reply'] is True
    assert by['102']['metadata']['parent_rpid'] == '101'
    sub_call = [c for c in http.calls if c[0] == bc.REPLY_SUB_URL][0]
    assert sub_call[1]['root'] == 100 and sub_call[1]['pn'] == 1


def test_expand_cap_per_video(tmp_path):
    roots = [_r(200 + i, 1, rcount=1) for i in range(5)]
    sub = {'code': 0, 'data': {'replies': [], 'page': {'count': 0}}}
    handlers = {bc.SEARCH_URL: _search((AID, BVID)), bc.REPLY_MAIN_URL: _main(roots), bc.REPLY_SUB_URL: sub}
    _, http = _run(handlers, tmp_path)
    assert sum(1 for c in http.calls if c[0] == bc.REPLY_SUB_URL) == bc.MAX_EXPAND_PER_VIDEO


# ── 风控降级 ──
@pytest.mark.parametrize('risk', [-412, -352])
def test_risk_code_stops_run_with_partial_result(tmp_path, risk):
    a2 = AID + 1
    b2 = bc.av2bv(a2)
    seq = [_main([_r(1, 1)]), {'code': risk, 'message': 'x'}, _main([_r(9, 1)])]
    handlers = {bc.SEARCH_URL: _search((AID, BVID), (a2, b2), (AID + 2, bc.av2bv(AID + 2))),
                bc.REPLY_MAIN_URL: seq}
    items, http = _run(handlers, tmp_path)
    assert len(items) == 1                                    # 风控前已采的保留
    assert sum(1 for c in http.calls if c[0] == bc.REPLY_MAIN_URL) == 2   # 第三个视频不再请求


def test_http_412_stops_run(tmp_path):
    handlers = {bc.SEARCH_URL: _search((AID, BVID), (AID + 1, bc.av2bv(AID + 1))),
                bc.REPLY_MAIN_URL: [Resp({}, 412), _main([_r(9, 1)])]}
    items, http = _run(handlers, tmp_path)
    assert items == [] and sum(1 for c in http.calls if c[0] == bc.REPLY_MAIN_URL) == 1


def test_closed_comments_skip_only_that_video(tmp_path):
    a2 = AID + 1
    handlers = {bc.SEARCH_URL: _search((AID, BVID), (a2, bc.av2bv(a2))),
                bc.REPLY_MAIN_URL: [{'code': 12002, 'message': '评论区已关闭'}, _main([_r(7, 1)])]}
    items, _ = _run(handlers, tmp_path)
    assert len(items) == 1


def test_video_exception_degrades_not_raises(tmp_path):
    a2 = AID + 1
    handlers = {bc.SEARCH_URL: _search((AID, BVID), (a2, bc.av2bv(a2))),
                bc.REPLY_MAIN_URL: [{'code': -400, 'message': 'bad'}, _main([_r(7, 1)])]}
    items, _ = _run(handlers, tmp_path)
    assert len(items) == 1


def test_request_budget_and_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, 'MAX_REQUESTS', 3)
    vids = [(AID + i, bc.av2bv(AID + i)) for i in range(6)]
    sleeps = []
    handlers = {bc.SEARCH_URL: _search(*vids), bc.REPLY_MAIN_URL: lambda p: _main([_r(p['oid'] % 1000, 1)])}
    items, http = _run(handlers, tmp_path, sleeps=sleeps)
    assert sum(1 for c in http.calls if c[0] == bc.REPLY_MAIN_URL) == 3
    assert len(sleeps) == 2 and all(s >= 1.0 for s in sleeps)


# ── 去重 ──
def test_dedup_key_stable_and_same_comment_once(tmp_path):
    items, _ = _plain(_main([_r(1), _r(1)], ), tmp_path)
    assert len(items) == 1
    assert collect_global.dedup_key(items[0]) == items[0]['url']


# ── 视频来源：归档 ──
def _write_archive(tmp_path, day, items):
    p = tmp_path / 'bilibili' / f'{day}.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'items': items}), encoding='utf-8')


def test_archived_videos_read_via_layout_sorted_by_engagement(tmp_path):
    _write_archive(tmp_path, '2026-10-02', [
        {'url': f'https://www.bilibili.com/video/av{AID}', 'title': 't1', 'engagement': 10, 'content_type': 'video'},
        {'url': f'https://www.bilibili.com/video/av{AID + 1}', 'title': 't2', 'engagement': 99, 'content_type': 'video'},
        {'url': 'https://example.com/none', 'title': 'x'}])
    _write_archive(tmp_path, '2026-09-01', [  # 超出近 N 天
        {'url': f'https://www.bilibili.com/video/av{AID + 2}', 'engagement': 1000}])
    # 评论折叠目录里的文件不应被当视频读
    com = tmp_path / 'bilibili' / 'cn' / 'comment' / '2026-10-02.json'
    com.parent.mkdir(parents=True)
    com.write_text(json.dumps({'items': [{'url': f'https://www.bilibili.com/video/av{AID + 3}'}]}), encoding='utf-8')
    vids = bc.archived_videos(archive_dir=tmp_path, today=NOW.date())
    assert [v['aid'] for v in vids] == [AID + 1, AID]
    assert vids[0]['bvid'] == bc.av2bv(AID + 1)


def test_old_videos_use_wider_window_and_caps(tmp_path):
    old_vid = AID + 50
    _write_archive(tmp_path, '2026-10-02', [
        {'url': f'https://www.bilibili.com/video/av{old_vid}', 'title': '老视频', 'engagement': 5, 'content_type': 'video'}])
    # 搜索无结果；老视频 2 天前的评论（超 24h cutoff）应被收，5 天前的不收
    handlers = {bc.SEARCH_URL: _search(), bc.REPLY_MAIN_URL: _main([_r(1, 48), _r(2, 24 * 5)])}
    items, _ = _run(handlers, tmp_path)
    assert [i['url'].rsplit('#reply', 1)[1] for i in items] == ['1']
    assert items[0]['metadata']['video_aid'] == str(old_vid)


def test_select_videos_prefers_search_and_caps():
    s = [{'aid': i, 'bvid': 'b', 'title': '', 'engagement': 0} for i in range(1, 11)]
    a = [{'aid': i, 'bvid': 'b', 'title': '', 'engagement': 0} for i in range(8, 40)]
    plan = bc.select_videos(s, a, max_videos=20, old_limit=5)
    assert len(plan) == 15 + 0 or len(plan) == 15
    assert [v['aid'] for v, old in plan if not old] == list(range(1, 11))
    assert sum(1 for _, old in plan if old) == 5
    assert len({v['aid'] for v, _ in plan}) == len(plan)


# ── 落点与登记 ──
def test_layout_target_and_registration(tmp_path):
    items, _ = _plain(_main([_r(1)]), tmp_path)
    it = items[0]
    plat, region, sub = archive_layout.resolve_write_layout('bilibili_comment', it['region'], it['archive_subtype'])
    assert str(archive_layout.build_relpath(plat, region, sub, '2026-10-03')) == \
        'bilibili/cn/comment/2026-10-03.json'
    assert 'bilibili_comment' in sources.KNOWN_SOURCES and 'bilibili_comment' in sources.ARCHIVE_PLATFORMS
    assert 'bilibili_comment' in sources.SPARSE_SOURCES
    assert collect_global.SOURCE_MAP['bilibili_comment'] == 'bilibili_comment'
    assert callable(gc.fetch_bilibili_comments)
    ok, cleaned = news_common.validate_news_item(it)
    assert ok and cleaned['archive_subtype'] == 'comment'


def test_host_traversal_skips_comment_subdir(tmp_path):
    vid = tmp_path / 'bilibili' / '2026-10-03.json'
    com = tmp_path / 'bilibili' / 'cn' / 'comment' / '2026-10-03.json'
    for p in (vid, com):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{}', encoding='utf-8')
    assert set(archive_layout.iter_source_files('bilibili', tmp_path)) == {vid}
    assert set(archive_layout.iter_source_files('bilibili_comment', tmp_path)) == {com}
