"""Facebook 官方主页评论采集：解析、未配置跳过、令牌不入日志、翻页、去重键、落点、注册。全部离线，样本自造。"""
import logging
import os
from datetime import datetime, timedelta, UTC
from unittest import mock

import _paths  # noqa: F401
import archive_layout
import archive_platforms
import collect_global
import facebook_page_collector as fc
import sources

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)
TOKEN = 'FAKE-TOKEN-abc123XYZ'


def _t(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%S+0000')


def _post(pid, hours=2, msg='官方公告样本'):
    return {'id': pid, 'message': msg, 'created_time': _t(hours),
            'permalink_url': f'https://www.facebook.example/page/posts/{pid}'}


def _comment(cid, hours=1, msg='自造评论', uid='u1', name='自造昵称', likes=2, replies=1, parent=None):
    c = {'id': cid, 'message': msg, 'created_time': _t(hours), 'like_count': likes,
         'comment_count': replies, 'permalink_url': f'https://www.facebook.example/c/{cid}'}
    if uid or name:
        c['from'] = {k: v for k, v in (('id', uid), ('name', name)) if v}
    if parent:
        c['parent'] = {'id': parent}
    return c


def _page(data, after=None):
    d = {'data': data}
    if after:
        d['paging'] = {'cursors': {'after': after}, 'next': 'https://graph.example/next?access_token=' + TOKEN}
    return d


def _router(posts_pages, comments_by_post):
    """按路径分发的假 getter；记录调用。"""
    calls = []

    def getter(path, params):
        calls.append((path, dict(params)))
        if path.endswith('/posts'):
            return posts_pages.pop(0)
        pid = path.split('/')[0]
        lst = comments_by_post[pid]
        return lst.pop(0) if lst else _page([])
    getter.calls = calls
    return getter


def test_unconfigured_returns_empty_and_logs_info(caplog):
    with caplog.at_level(logging.INFO, logger=fc.logger.name):
        for pid, tok in (('', TOKEN), ('123', ''), ('', ''), (None, None)):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('FB_PAGE_ID', None)
                os.environ.pop('FB_PAGE_TOKEN', None)
                getter = mock.Mock()
                assert fc.fetch_facebook_page(CUTOFF, getter, lambda s: None, page_id=pid, token=tok) == []
                getter.assert_not_called()
    msgs = [r for r in caplog.records]
    assert msgs and all(r.levelno == logging.INFO and '未配置，跳过' in r.getMessage() for r in msgs)


def test_env_vars_are_read(monkeypatch):
    monkeypatch.setenv('FB_PAGE_ID', '777')
    monkeypatch.setenv('FB_PAGE_TOKEN', TOKEN)
    g = _router([_page([])], {})
    assert fc.fetch_facebook_page(CUTOFF, g, lambda s: None) == []
    assert g.calls[0][0] == '777/posts'


def test_parse_comment_fields_and_layout():
    item = fc.parse_comment(_comment('c1', parent='p9'), 'p1', 'https://www.facebook.example/page/posts/p1', '777')
    assert item['url'] == 'https://www.facebook.example/c/c1'
    assert item['author'] == '自造昵称' and item['metadata']['author_id'] == 'u1'
    assert item['summary'] == '自造评论' and item['engagement'] == 3
    assert item['source'] == 'facebook' and item['title'].startswith('[Facebook 评论]')
    md = item['metadata']
    assert md['is_official_post'] is False and md['post_id'] == 'p1' and md['parent_id'] == 'p9'
    assert md['post_url'].endswith('/p1')
    assert item['time'] == (NOW - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    # 落点：平铺 facebook/<日期>.json，评论与官方帖同档
    assert archive_layout.resolve_write_layout('facebook') == ('facebook', None, None)
    assert archive_layout.build_relpath('facebook', None, None, '2026-10-03').as_posix() == 'facebook/2026-10-03.json'


def test_parse_comment_edge_cases():
    anon = fc.parse_comment(_comment('c2', uid='', name=''), 'p1')
    assert anon['author'] == '' and anon['metadata']['author_is_unknown'] is True
    assert 'author_id' not in anon['metadata']
    assert fc.parse_comment(_comment('c3', msg=''), 'p1') is None  # 纯贴图无文本
    assert fc.parse_comment({'id': 'c4'}, 'p1') is None
    no_link = _comment('c5')
    del no_link['permalink_url']
    assert fc.parse_comment(no_link, 'p1')['url'] == 'https://www.facebook.com/c5'


def test_parse_post_marked_official():
    item = fc.parse_post(_post('p1'), '777')
    assert item['metadata']['is_official_post'] is True and item['metadata']['post_id'] == 'p1'
    assert item['title'].startswith('[Facebook 官方帖]')
    assert fc.parse_post({'id': 'x'}) is None


def test_fetch_flow_window_comments_posts_and_dedup():
    g = _router(
        [_page([_post('p1', hours=2), _post('p2', hours=80)])],  # p2 是老帖：不产官方帖条目，评论仍展开
        {'p1': [_page([_comment('c1'), _comment('c1'), _comment('c2', hours=30)])],
         'p2': [_page([_comment('c3', hours=3, parent='c9')])]})
    items = fc.fetch_facebook_page(CUTOFF, g, lambda s: None, page_id='777', token=TOKEN)
    by = {i['url'].rsplit('/', 1)[1]: i for i in items}
    assert set(by) == {'p1', 'c1', 'c3'}  # c1 去重；c2 时窗外；老帖 p2 不入
    assert by['p1']['metadata']['is_official_post'] is True
    assert by['c3']['metadata']['post_id'] == 'p2'
    posts_call = g.calls[0]
    assert posts_call[1]['fields'] == fc.POST_FIELDS and 'since' in posts_call[1]
    comm_call = [c for c in g.calls if c[0] == 'p1/comments'][0]
    assert comm_call[1]['filter'] == 'stream' and 'parent{id}' in comm_call[1]['fields']


def test_comment_pagination_uses_after_cursor_and_caps():
    pages = [_page([_comment('a')], after='A1'), _page([_comment('b')], after='A2'), _page([_comment('c')])]
    g = _router([_page([_post('p1')])], {'p1': pages})
    items = fc.fetch_facebook_page(CUTOFF, g, lambda s: None, page_id='777', token=TOKEN)
    assert len([i for i in items if not i['metadata']['is_official_post']]) == 3
    cc = [c for c in g.calls if c[0] == 'p1/comments']
    assert 'after' not in cc[0][1] and cc[1][1]['after'] == 'A1' and cc[2][1]['after'] == 'A2'
    # 永不结束的游标被页数上限截断
    forever = [_page([_comment(f'x{i}')], after=f'N{i}') for i in range(50)]
    g2 = _router([_page([_post('p1')])], {'p1': forever})
    fc.fetch_facebook_page(CUTOFF, g2, lambda s: None, page_id='777', token=TOKEN)
    assert len([c for c in g2.calls if c[0] == 'p1/comments']) == fc.MAX_COMMENT_PAGES


def test_posts_capped_per_run():
    posts = [_post(f'p{i}', hours=2) for i in range(fc.MAX_POSTS_PER_RUN + 5)]
    g = _router([_page(posts)], {p['id']: [_page([])] for p in posts})
    fc.fetch_facebook_page(CUTOFF, g, lambda s: None, page_id='777', token=TOKEN)
    assert len([c for c in g.calls if c[0].endswith('/comments')]) == fc.MAX_POSTS_PER_RUN


def test_single_post_comment_failure_degrades_but_posts_failure_raises(caplog):
    def getter(path, params):
        if path.endswith('/posts'):
            return _page([_post('p1'), _post('p2')])
        if path.startswith('p1'):
            raise RuntimeError(f'boom token={TOKEN}')
        return _page([_comment('c9')])
    with caplog.at_level(logging.WARNING, logger=fc.logger.name):
        items = fc.fetch_facebook_page(CUTOFF, getter, lambda s: None, page_id='777', token=TOKEN)
    assert any(i['url'].endswith('/c9') for i in items)
    assert TOKEN not in caplog.text and '***' in caplog.text

    def bad(path, params):
        raise RuntimeError(f'(#190) token expired {TOKEN}')
    try:
        fc.fetch_facebook_page(CUTOFF, bad, lambda s: None, page_id='777', token=TOKEN)
    except RuntimeError as e:
        assert TOKEN not in str(e) and 'Facebook 取主页帖子失败' in str(e)
    else:
        raise AssertionError('应抛错')


def test_token_only_in_header_never_in_url_or_params():
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {'data': []}
    with mock.patch.object(fc.requests, 'get', return_value=resp) as m:
        fc._make_getter(TOKEN)('777/posts', {'fields': 'id'})
    args, kwargs = m.call_args
    assert TOKEN not in args[0] and TOKEN not in str(kwargs['params'])
    assert kwargs['headers']['Authorization'] == f'Bearer {TOKEN}'
    assert fc.GRAPH_VERSION in args[0]


def test_http_error_message_has_no_token(caplog):
    resp = mock.Mock(status_code=400, text='x')
    resp.json.return_value = {'error': {'type': 'OAuthException', 'code': 190, 'message': 'Invalid OAuth'}}
    with mock.patch.object(fc.requests, 'get', return_value=resp):
        try:
            fc._make_getter(TOKEN)('777/posts', {})
        except RuntimeError as e:
            assert 'code=190' in str(e) and TOKEN not in str(e)
        else:
            raise AssertionError('应抛错')
    exc = fc.requests.ConnectionError(f'https://graph.example/?access_token={TOKEN}')
    with mock.patch.object(fc.requests, 'get', side_effect=exc):
        try:
            fc._make_getter(TOKEN)('777/posts', {})
        except RuntimeError as e:
            assert TOKEN not in str(e) and TOKEN not in repr(e.__cause__)


def test_dedup_keys_stable():
    a = fc.parse_comment(_comment('c7', msg='v1'), 'p1')
    b = fc.parse_comment(_comment('c7', msg='编辑后', name='改名'), 'p1')
    assert collect_global.dedup_key(a) == collect_global.dedup_key(b) == 'https://www.facebook.example/c/c7'
    assert archive_platforms.item_key(a) == archive_platforms.item_key(b)


def test_registered_and_auth_gated():
    assert 'facebook' in sources.KNOWN_SOURCES and 'facebook' in sources.SPARSE_SOURCES
    assert 'facebook' in sources.ARCHIVE_PLATFORMS
    assert sources.AUTH_GATED['facebook'] == 'FB_PAGE_TOKEN'
    assert collect_global.SOURCE_MAP['facebook'] == 'facebook'
    import global_collectors
    assert callable(global_collectors.fetch_facebook_page)
    assert fc.GRAPH_VERSION == 'v21.0'
