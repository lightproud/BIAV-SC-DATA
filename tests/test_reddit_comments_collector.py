"""Reddit 评论采集：离线（mock token 与评论树，样本自造）。

覆盖：评论树展开、more 节点上限、删除评论跳过、未配置跳过、凭据不入日志、去重键、落点、限速头处理。
"""
import logging
from datetime import datetime, timedelta, UTC

import _paths  # noqa: F401
import archive_layout
import archive_platforms
import collect_global
import global_collectors as gc
import reddit_comments_collector as rc
import sources

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)
ENV = {'REDDIT_CLIENT_ID': 'cid_SECRETID', 'REDDIT_CLIENT_SECRET': 'sec_SECRETVALUE'}
KW = ['Morimens']


def _ts(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).timestamp()


def _c(cid, hours_ago=1, body=None, author='alice', depth=0, parent='t3_p1', link='t3_p1',
       replies=None, score=5, sub='Morimens'):
    d = {'id': cid, 'body': body if body is not None else f'评论 {cid}', 'author': author,
         'author_fullname': 't2_abc', 'created_utc': _ts(hours_ago), 'score': score,
         'permalink': f'/r/{sub}/comments/p1/title/{cid}/', 'link_id': link,
         'parent_id': parent, 'depth': depth, 'subreddit': sub, 'link_title': 'Morimens 帖'}
    if replies is not None:
        d['replies'] = {'kind': 'Listing', 'data': {'children': replies}}
    return {'kind': 't1', 'data': d}


def _more(ids):
    return {'kind': 'more', 'data': {'children': ids, 'count': len(ids)}}


def _listing(children):
    return {'kind': 'Listing', 'data': {'children': children}}


def _post(pid, hours_ago=2, title='Morimens 讨论', n=3):
    return {'kind': 't3', 'data': {'id': pid, 'title': title, 'created_utc': _ts(hours_ago),
                                   'num_comments': n}}


def _cid(item):
    return item['url'].rstrip('/').rsplit('/', 1)[-1]


class FakeResp:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload, self.status_code, self.headers = payload, status, headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class FakeHttp:
    """按路径路由的假 HTTP；记录全部请求（供凭据 / 头断言）。"""

    def __init__(self, routes, headers=None, token='tok_ABC123'):
        self.routes, self.headers, self.token = routes, headers or {}, token
        self.gets, self.posts = [], []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return FakeResp({'access_token': self.token, 'expires_in': 3600})

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params, headers))
        path = url.replace(rc.API_BASE, '')
        if path in self.routes:
            val = self.routes[path]
            resp = val(params) if callable(val) else val
            return resp if isinstance(resp, FakeResp) else FakeResp(resp, headers=self.headers)
        return FakeResp(_listing([]), headers=self.headers)


def _routes(tree_children, new_posts=None, stream=None, extra=None):
    r = {
        '/r/Morimens/new': _listing(new_posts if new_posts is not None else [_post('p1')]),
        '/r/Morimens/comments': _listing(stream or []),
        '/r/Morimens/comments/p1.json': [_listing([_post('p1')]), _listing(tree_children)],
    }
    r.update(extra or {})
    return r


def _run(routes, headers=None, env=None, subreddits=('Morimens',)):
    http = FakeHttp(routes, headers)
    items = rc.fetch_reddit_comments(
        CUTOFF, env=ENV if env is None else env, http=http, sleep=lambda s: None,
        clock=lambda: 0.0, subreddits=list(subreddits), keywords=KW)
    return items, http


def _client(http, **kw):
    return rc.RedditClient(rc.credentials_from_env(ENV), rc.DEFAULT_UA, http=http,
                           sleep=kw.pop('sleep', lambda s: None), clock=lambda: 0.0, **kw)


# ── 评论树展开 ──
def test_tree_flattened_with_fields():
    tree = [_c('a', replies=[_c('b', depth=1, parent='t1_a', author='bob', replies=[
        _c('c', depth=2, parent='t1_b', author='carol')])]), _c('d', author='dave')]
    items, _ = _run(_routes(tree))
    assert sorted(_cid(i) for i in items) == ['a', 'b', 'c', 'd']
    b = next(i for i in items if _cid(i) == 'b')
    assert b['url'] == 'https://www.reddit.com/r/Morimens/comments/p1/title/b/'
    assert b['author'] == 'bob'
    assert b['summary'] == '评论 b'
    assert b['source'] == 'reddit_comment'
    assert b['metadata'] == {'post_id': 'p1', 'parent_id': 't1_a', 'score': 5, 'subreddit': 'Morimens',
                             'depth': 1, 'author_id': 't2_abc'}
    assert datetime.fromisoformat(b['time']) == datetime.fromtimestamp(_ts(1), UTC)


def test_tree_request_params():
    _, http = _run(_routes([_c('a')]))
    url, params, headers = next(g for g in http.gets if 'comments/p1' in g[0])
    assert url == 'https://oauth.reddit.com/r/Morimens/comments/p1.json'
    assert params['sort'] == 'new' and params['limit'] == 500 and params['depth'] == 10
    assert headers['Authorization'] == 'bearer tok_ABC123'
    assert '(by /u/' in headers['User-Agent']


def test_old_comments_outside_window_dropped():
    items, _ = _run(_routes([_c('old', hours_ago=48), _c('new', hours_ago=1)]))
    assert [_cid(i) for i in items] == ['new']


# ── 删除 / 移除 ──
def test_deleted_and_removed_skipped_and_counted():
    stats = {}
    tree = [_c('a'), _c('x', body='[deleted]'), _c('y', body='[removed]'), _c('z', author='[deleted]')]
    http = FakeHttp({'/r/Morimens/comments/p1.json': [_listing([_post('p1')]), _listing(tree)]})
    got = rc.fetch_post_comments(_client(http), 'Morimens', 'p1', 't', CUTOFF, stats)
    assert [_cid(i) for i in got] == ['a']
    assert stats['deleted'] == 3


# ── more 节点 ──
def test_more_nodes_expanded_via_morechildren():
    tree = [_c('a'), _more(['m1', 'm2'])]
    more_resp = {'json': {'data': {'things': [_c('m1', depth=0), _c('m2', depth=1)]}}}
    items, http = _run(_routes(tree, extra={'/api/morechildren': more_resp}))
    assert {_cid(i) for i in items} == {'a', 'm1', 'm2'}
    call = next(g for g in http.gets if g[0].endswith('/api/morechildren'))
    assert call[1]['children'] == 'm1,m2' and call[1]['link_id'] == 't3_p1'


def test_more_calls_capped_per_post():
    ids = [f'k{i}' for i in range(rc.MORE_CHUNK * (rc.MAX_MORE_CALLS_PER_POST + 2))]
    stats = {}
    http = FakeHttp({'/r/Morimens/comments/p1.json': [_listing([]), _listing([_c('a'), _more(ids)])],
                     '/api/morechildren': {'json': {'data': {'things': []}}}})
    rc.fetch_post_comments(_client(http), 'Morimens', 'p1', 't', CUTOFF, stats)
    calls = [g for g in http.gets if g[0].endswith('/api/morechildren')]
    assert len(calls) == rc.MAX_MORE_CALLS_PER_POST
    assert stats['more_skipped'] == rc.MORE_CHUNK * 2


# ── 未配置 / 凭据 ──
def test_unconfigured_returns_empty_and_logs_info(caplog):
    http = FakeHttp({})
    with caplog.at_level(logging.INFO, logger=rc.logger.name):
        for env in ({}, {'REDDIT_CLIENT_ID': 'only_id'}, {'REDDIT_CLIENT_SECRET': 'only_secret'}):
            assert rc.fetch_reddit_comments(CUTOFF, env=env, http=http, keywords=KW) == []
    assert '未配置，跳过' in caplog.text
    assert not http.gets and not http.posts


def test_unconfigured_is_degraded_not_failed():
    # collect_global 据 AUTH_GATED 把「0 条且未配密钥」标成待配降级，不计采集故障
    assert sources.AUTH_GATED['reddit_comment'] == 'REDDIT_CLIENT_ID'
    assert 'reddit_comment' not in sources.CORE_SOURCES


def test_client_credentials_vs_password_grant():
    _, http = _run(_routes([_c('a')]))
    url, kw = http.posts[0]
    assert url == 'https://www.reddit.com/api/v1/access_token'
    assert kw['data']['grant_type'] == 'client_credentials'
    assert kw['auth'] == ('cid_SECRETID', 'sec_SECRETVALUE')
    _, http2 = _run(_routes([_c('a')]), env={**ENV, 'REDDIT_USERNAME': 'u', 'REDDIT_PASSWORD': 'pw_XYZ'})
    assert http2.posts[0][1]['data'] == {'grant_type': 'password', 'username': 'u', 'password': 'pw_XYZ'}


def test_user_agent_rules():
    assert rc.build_user_agent('') == rc.DEFAULT_UA
    assert rc.build_user_agent('light') == 'python:biav-sc-news:1.0 (by /u/light)'
    assert rc.build_user_agent('/u/light') == 'python:biav-sc-news:1.0 (by /u/light)'
    full = 'python:x:2 (by /u/bob)'
    assert rc.build_user_agent(full) == full


def test_credentials_never_in_logs_or_items(caplog):
    class Boom(FakeHttp):
        def get(self, url, **kw):
            raise RuntimeError('fail with sec_SECRETVALUE and cid_SECRETID and tok_ABC123 and pw_XYZ')

    env = {**ENV, 'REDDIT_PASSWORD': 'pw_XYZ', 'REDDIT_USERNAME': 'u'}
    with caplog.at_level(logging.DEBUG):
        try:
            rc.fetch_reddit_comments(CUTOFF, env=env, http=Boom({}), sleep=lambda s: None,
                                     clock=lambda: 0.0, subreddits=['Morimens'], keywords=KW)
        except RuntimeError as e:
            for secret in ('sec_SECRETVALUE', 'cid_SECRETID', 'tok_ABC123', 'pw_XYZ'):
                assert secret not in str(e)
    for secret in ('sec_SECRETVALUE', 'cid_SECRETID', 'tok_ABC123', 'pw_XYZ'):
        assert secret not in caplog.text
    assert 'failed' in caplog.text.lower() or '失败' in caplog.text  # 确实走了降级日志
    items, _ = _run(_routes([_c('a')]))
    assert 'SECRET' not in repr(items)


def test_token_failure_is_sanitized_failure():
    class Bad(FakeHttp):
        def post(self, url, **kw):
            raise RuntimeError('401 for sec_SECRETVALUE')
    try:
        rc.fetch_reddit_comments(CUTOFF, env=ENV, http=Bad({}), keywords=KW)
    except RuntimeError as e:
        assert 'sec_SECRETVALUE' not in str(e)
        return
    raise AssertionError('已配置凭据但取 token 失败应抛错')


# ── 去重键 / 落点 ──
def test_dedup_key_is_stable_url_and_stream_dedups_with_tree():
    items, _ = _run(_routes([_c('a'), _c('b')], stream=[_c('a')]))
    urls = [i['url'] for i in items]
    assert len(urls) == len(set(urls)) == 2
    assert collect_global.dedup_key(items[0]) == items[0]['url'].rstrip('/')
    assert archive_platforms.item_key(items[0]) == archive_platforms.item_key(dict(items[0]))


def test_stream_comments_kept_when_post_tree_not_fetched():
    # 评论树路由缺失（返回空 listing → 形状异常 → 该帖降级），评论流里的评论仍入库
    routes = _routes([], new_posts=[], stream=[_c('s1', link='t3_zz'), _c('s2', link='t3_zz')])
    items, _ = _run(routes)
    assert {_cid(i) for i in items} == {'s1', 's2'}


def test_stream_comment_without_depth_omits_field():
    c = _c('s1', link='t3_zz')
    del c['data']['depth']
    items, _ = _run(_routes([], new_posts=[], stream=[c]))
    assert 'depth' not in items[0]['metadata']


def test_layout_target_and_registration():
    items, _ = _run(_routes([_c('a')]))
    it = items[0]
    assert (it['region'], it['archive_subtype']) == ('global', 'comment')
    plat, region, sub = archive_layout.resolve_write_layout('reddit_comment', it['region'], it['archive_subtype'])
    assert str(archive_layout.build_relpath(plat, region, sub, '2026-10-03')) == \
        'reddit/global/comment/2026-10-03.json'
    assert 'reddit_comment' in sources.KNOWN_SOURCES and 'reddit_comment' in sources.ARCHIVE_PLATFORMS
    assert collect_global.SOURCE_MAP['reddit_comment'] == 'reddit_comment'
    assert collect_global.convert_item(it)['archive_subtype'] == 'comment'
    assert callable(gc.fetch_reddit_comments)


def test_reddit_host_traversal_skips_comment_subdir(tmp_path):
    post = tmp_path / 'reddit' / '2026-10-03.json'
    com = tmp_path / 'reddit' / 'global' / 'comment' / '2026-10-03.json'
    for p in (post, com):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{}', encoding='utf-8')
    assert set(archive_layout.iter_source_files('reddit', tmp_path)) == {post}
    assert set(archive_layout.iter_source_files('reddit_comment', tmp_path)) == {com}


def test_validation_chain_accepts_item():
    import news_common
    items, _ = _run(_routes([_c('a')]))
    ok, cleaned = news_common.validate_news_item(items[0])
    assert ok and cleaned['archive_subtype'] == 'comment' and cleaned['metadata']['author_id'] == 't2_abc'


# ── 限速 ──
def test_low_remaining_waits_until_reset():
    sleeps = []
    http = FakeHttp(_routes([_c('a')]), headers={'X-Ratelimit-Remaining': '1', 'X-Ratelimit-Reset': '30'})
    rc.fetch_reddit_comments(CUTOFF, env=ENV, http=http, sleep=sleeps.append, clock=lambda: 0.0,
                             subreddits=['Morimens'], keywords=KW)
    assert 31 in sleeps  # reset + 1


def test_reset_too_far_stops_run_with_partial_result():
    http = FakeHttp(_routes([_c('a')]), headers={'X-Ratelimit-Remaining': '0', 'X-Ratelimit-Reset': '500'})
    items = rc.fetch_reddit_comments(CUTOFF, env=ENV, http=http, sleep=lambda s: None,
                                     clock=lambda: 0.0, subreddits=['Morimens', 'MorimensGame'],
                                     keywords=KW)
    assert items == []            # 首个请求后额度见底且重置太远 → 收工，不抛
    assert len(http.gets) == 1    # 之后一个请求都没再发


def test_healthy_headers_do_not_wait_beyond_delay():
    sleeps = []
    http = FakeHttp(_routes([_c('a')]), headers={'X-Ratelimit-Remaining': '90', 'X-Ratelimit-Reset': '40'})
    rc.fetch_reddit_comments(CUTOFF, env=ENV, http=http, sleep=sleeps.append, clock=lambda: 0.0,
                             subreddits=['Morimens'], keywords=KW)
    assert sleeps and set(sleeps) == {rc.REQUEST_DELAY}


def test_429_waits_once_then_retries_then_gives_up():
    sleeps, n = [], {'c': 0}

    def limited(_p):
        n['c'] += 1
        return FakeResp(status=429, headers={'Retry-After': '5'})
    http = FakeHttp({'/r/Morimens/new': limited})
    items = rc.fetch_reddit_comments(CUTOFF, env=ENV, http=http, sleep=sleeps.append, clock=lambda: 0.0,
                                     subreddits=['Morimens'], keywords=KW)
    assert n['c'] == 2 and 6 in sleeps and items == []


def test_401_refreshes_token_once():
    state = {'n': 0}

    def flaky(_p):
        state['n'] += 1
        return FakeResp(status=401) if state['n'] == 1 else FakeResp(_listing([]))
    http = FakeHttp({'/r/Morimens/new': flaky})
    rc.fetch_reddit_comments(CUTOFF, env=ENV, http=http, sleep=lambda s: None, clock=lambda: 0.0,
                             subreddits=['Morimens'], keywords=KW)
    assert len(http.posts) == 2  # 启动取一次 + 401 后刷新一次


def test_request_budget_cap():
    client = _client(FakeHttp({}), max_requests=2)
    client.get_json('/x')
    client.get_json('/x')
    try:
        client.get_json('/x')
    except rc.RateLimitExhausted:
        return
    raise AssertionError('超预算应抛 RateLimitExhausted')


# ── 关键词过滤 / 降级 ──
def test_general_subreddit_keyword_filter():
    routes = {
        '/r/gachagaming/new': _listing([_post('g1', title='Morimens tier list'),
                                         _post('g2', title='别的游戏')]),
        '/r/gachagaming/comments': _listing([]),
        '/r/gachagaming/comments/g1.json': [_listing([]), _listing([_c('x', sub='gachagaming', link='t3_g1')])],
        '/r/gachagaming/comments/g2.json': [_listing([]), _listing([_c('y', sub='gachagaming', link='t3_g2')])],
    }
    items, http = _run(routes, subreddits=['gachagaming'])
    assert [_cid(i) for i in items] == ['x']
    assert not any('g2.json' in g[0] for g in http.gets)


def test_post_cap_per_subreddit():
    posts = [_post(f'q{i}', hours_ago=i + 1) for i in range(rc.MAX_POSTS_PER_SUB + 5)]
    routes = {'/r/Morimens/new': _listing(posts), '/r/Morimens/comments': _listing([])}
    _, http = _run(routes)
    assert len([g for g in http.gets if '/comments/q' in g[0]]) == rc.MAX_POSTS_PER_SUB


def test_single_post_failure_degrades():
    routes = _routes([_c('a')], new_posts=[_post('p1'), _post('p2', hours_ago=3)],
                     extra={'/r/Morimens/comments/p2.json': FakeResp(status=500)})
    items, _ = _run(routes)
    assert [_cid(i) for i in items] == ['a']


def test_all_discovery_failing_raises():
    http = FakeHttp({'/r/Morimens/new': FakeResp(status=500)})
    try:
        rc.fetch_reddit_comments(CUTOFF, env=ENV, http=http, sleep=lambda s: None, clock=lambda: 0.0,
                                 subreddits=['Morimens'], keywords=KW)
    except RuntimeError:
        return
    raise AssertionError('全部子版块发现失败应抛错交编排层')
