"""Bluesky 采集：解析、噪声过滤、翻页停止、失败降级、去重键、归档落点、注册。全部离线，样本自造。"""
from datetime import datetime, timedelta, UTC
from unittest import mock

import _paths  # noqa: F401
import archive_layout
import archive_platforms
import bluesky_collector as bc
import collect_global
import sources

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)


def _ts(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%S.123Z')


def _post(rkey, text='this gacha game is fun morimens', hours=1, handle='user-a.example.test',
          name='自造昵称', did='did:plc:fake0001', likes=2, reposts=1, replies=3, langs=('en',),
          tags=None, facets=None, embed=None):
    rec = {'$type': 'app.bsky.feed.post', 'text': text, 'createdAt': _ts(hours), 'langs': list(langs)}
    if tags:
        rec['tags'] = tags
    if facets:
        rec['facets'] = facets
    p = {'uri': f'at://{did}/app.bsky.feed.post/{rkey}', 'cid': 'x',
         'author': {'did': did, 'handle': handle, 'displayName': name},
         'record': rec, 'likeCount': likes, 'repostCount': reposts, 'replyCount': replies,
         'indexedAt': _ts(hours)}
    if embed:
        p['embed'] = embed
    return p


def _resp(posts, cursor=None):
    r = mock.Mock()
    d = {'posts': posts}
    if cursor:
        d['cursor'] = cursor
    r.json.return_value = d
    return r


def test_parse_fields_and_layout():
    item = bc.parse_post(_post('3abc'), 'morimens', CUTOFF)
    assert item['url'] == 'https://bsky.app/profile/did:plc:fake0001/post/3abc'
    assert item['author'] == '自造昵称'
    assert item['metadata']['author_id'] == 'did:plc:fake0001'
    assert item['summary'] == 'this gacha game is fun morimens'
    assert item['engagement'] == 6
    assert item['source'] == 'bluesky' and item['lang'] == 'en'
    assert item['time'] == (NOW - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    assert 'keyword_only' not in item['metadata']
    # 落点：平铺 bluesky/<日期>.json
    assert archive_layout.resolve_write_layout('bluesky') == ('bluesky', None, None)
    assert archive_layout.build_relpath('bluesky', None, None, '2026-10-03').as_posix() == 'bluesky/2026-10-03.json'


def test_author_falls_back_to_handle_and_bad_posts_skipped():
    item = bc.parse_post(_post('3abd', name=''), 'morimens', CUTOFF)
    assert item['author'] == 'user-a.example.test'
    assert bc.parse_post({'uri': 'at://d/app.bsky.feed.post/1', 'record': {}}, 'morimens') is None
    assert bc.parse_post(_post('3abe', hours=48), 'morimens', CUTOFF) is None  # 时窗外
    assert bc.parse_post('junk') is None


def test_noise_filter_three_verdicts():
    # 关键词只出现在作者资料之类外围（帖子本身无）-> 丢弃
    assert bc.parse_post(_post('1', text='完全无关的话'), 'morimens', CUTOFF) is None
    # 含关键词但无游戏线索 -> 保留并标 keyword_only
    kw = bc.parse_post(_post('2', text='my account mvp #morimens'), 'morimens', CUTOFF)
    assert kw['metadata']['keyword_only'] is True
    # 含线索词 -> 保留不标
    ok = bc.parse_post(_post('3', text='morimens chapter 8 clear'), 'morimens', CUTOFF)
    assert 'keyword_only' not in ok['metadata']
    # 线索在外部卡片 / 标签里
    card = bc.parse_post(_post('4', text='morimens', embed={'external': {'uri': 'https://store.steampowered.example/x',
                                                                         'title': '', 'description': ''}}),
                         'morimens', CUTOFF)
    assert 'keyword_only' not in card['metadata']
    # 非拉丁关键词即游戏名，无歧义
    jp = bc.parse_post(_post('5', text='忘却前夜おもしろい', langs=('ja',)), '忘却前夜', CUTOFF)
    assert 'keyword_only' not in jp['metadata'] and jp['lang'] == 'ja'


def test_pagination_cursor_and_stop_on_old_page():
    pages = [_resp([_post('a', hours=1), _post('b', hours=2)], cursor='c1'),
             _resp([_post('c', hours=3), _post('d', hours=30)], cursor='c2'),
             _resp([_post('e', hours=40), _post('f', hours=41)], cursor='c3'),  # 整页过旧 -> 停
             _resp([_post('g', hours=1)])]
    calls = []

    def getter(params):
        calls.append(dict(params))
        return pages[len(calls) - 1]
    items, _ = bc._fetch_keyword('morimens', CUTOFF, getter, lambda s: None)
    assert [i['url'].rsplit('/', 1)[1] for i in items] == ['a', 'b', 'c']
    assert len(calls) == 3
    assert 'cursor' not in calls[0] and calls[1]['cursor'] == 'c1'
    assert calls[0]['since'] == CUTOFF.strftime('%Y-%m-%dT%H:%M:%SZ') and calls[0]['limit'] == 100


def test_pagination_stops_without_cursor_and_at_max_pages():
    g = mock.Mock(side_effect=[_resp([_post('a')])])
    bc._fetch_keyword('morimens', CUTOFF, g, lambda s: None)
    assert g.call_count == 1
    g2 = mock.Mock(side_effect=lambda params: _resp([_post(f'p{params.get("cursor", "0")}')], cursor='n' + str(g2.call_count)))
    bc._fetch_keyword('morimens', CUTOFF, g2, lambda s: None)
    assert g2.call_count == bc.MAX_PAGES


def test_fetch_merges_keywords_dedups_and_degrades():
    seen_q = []

    def getter(params):
        seen_q.append(params['q'])
        if params['q'] == '망각전야':
            raise OSError('down')
        # 同一帖被多个关键词命中
        return _resp([_post('same', text='morimens 忘却前夜 game')])
    items = bc.fetch_bluesky(CUTOFF, getter, lambda s: None)
    assert seen_q == bc.KEYWORDS
    assert len(items) == 1  # 去重
    assert items[0]['url'].endswith('/post/same')


def test_all_keywords_failed_raises():
    def boom(params):
        raise OSError('down')
    try:
        bc.fetch_bluesky(CUTOFF, boom, lambda s: None)
    except RuntimeError as e:
        assert 'all keywords failed' in str(e)
    else:
        raise AssertionError('应抛错')


def test_dedup_keys_stable_across_text_edits_and_keywords():
    a = bc.parse_post(_post('9z', text='morimens game v1'), 'morimens', CUTOFF)
    b = bc.parse_post(_post('9z', text='忘却前夜 game v2', name='改名'), '忘却前夜', CUTOFF)
    want = 'https://bsky.app/profile/did:plc:fake0001/post/9z'
    assert collect_global.dedup_key(a) == collect_global.dedup_key(b) == want
    assert archive_platforms.item_key(a) == archive_platforms.item_key(b) == want


def test_registered_as_sparse_source_and_wired():
    assert 'bluesky' in sources.KNOWN_SOURCES and 'bluesky' in sources.SPARSE_SOURCES
    assert 'bluesky' in sources.ARCHIVE_PLATFORMS
    assert collect_global.SOURCE_MAP['bluesky'] == 'bluesky'
    import global_collectors
    assert callable(global_collectors.fetch_bluesky)
    import json
    from pathlib import Path
    reg = json.loads((Path(sources.__file__).parent / 'tier_registry.json').read_text(encoding='utf-8'))
    assert reg['modules']['bluesky_collector']['tier'] == 'T1'


def test_url_uses_did_so_handle_change_keeps_key():
    a = bc.parse_post(_post('7k'), 'morimens', CUTOFF)
    p = _post('7k')
    p['author'] = dict(p['author'], handle='renamed.example.test')
    b = bc.parse_post(p, 'morimens', CUTOFF)
    assert a['url'] == b['url'] and 'did:plc:fake0001' in a['url']
