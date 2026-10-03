"""Telegram 公开频道 + Misskey 采集：解析、翻页停止、cutoff、噪声标注、去重键、归档落点、注册。全部离线，样本自造。"""
import json
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401
import archive_layout
import archive_platforms
import collect_global
import misskey_collector as mk
import sources
import telegram_collector as tg

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%S+00:00')


# ───────────────────────── Telegram ─────────────────────────

def _tg_block(pid, hours=1, text='Первая строка<br/>вторая &amp; третья', views='2.5K', fwd=False,
              media=False, channel='fakechan'):
    parts = [f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
             f'<div class="tgme_widget_message" data-post="{channel}/{pid}">']
    parts.append('<div class="tgme_widget_message_author"><a class="tgme_widget_message_owner_name" href="x">'
                 '<span dir="auto">自造频道名</span></a></div>')
    if fwd:
        parts.append('<div class="tgme_widget_message_forwarded_from">fwd</div>')
    if media:
        parts.append("<a class=\"tgme_widget_message_photo_wrap\" style=\"background-image:url('https://img.example.test/a.jpg')\"></a>")
    if text is not None:
        parts.append(f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>')
    parts.append(f'<span class="tgme_widget_message_views">{views}</span>')
    parts.append(f'<a class="tgme_widget_message_date"><time datetime="{_iso(hours)}"></time></a>')
    parts.append('</div></div>')
    return ''.join(parts)


def _page(*blocks):
    return '<html><body>' + ''.join(blocks) + '</body></html>'


def _resp(html):
    r = mock.Mock()
    r.text = html
    return r


def test_parse_views():
    assert tg.parse_views('2.56K') == 2560 and tg.parse_views('1.2M') == 1_200_000
    assert tg.parse_views('834') == 834 and tg.parse_views('') == 0 and tg.parse_views('abc') == 0


def test_telegram_parse_fields_and_layout():
    raws = tg.parse_blocks(_page(_tg_block(101, fwd=True, media=True)))
    assert len(raws) == 1
    item = tg.to_item(raws[0], 'fakechan', 'ru', CUTOFF)
    assert item['url'] == 'https://t.me/fakechan/101'
    assert item['author'] == '自造频道名' and item['source'] == 'telegram' and item['lang'] == 'ru'
    assert item['summary'] == 'Первая строка\nвторая & третья'
    assert item['engagement'] == 0
    assert item['media_url'] == 'https://img.example.test/a.jpg'
    md = item['metadata']
    assert md['kind'] == 'channel_post' and md['is_official_channel'] is True
    assert md['views'] == 2500 and md['forwarded'] is True and md['has_media'] is True
    assert md['author_id'] == 'fakechan' and md['post_id'] == 101
    assert archive_layout.resolve_write_layout('telegram') == ('telegram', None, None)
    assert archive_layout.build_relpath('telegram', None, None, '2026-10-03').as_posix() == 'telegram/2026-10-03.json'


def test_telegram_no_text_post_and_skip_broken_blocks():
    page = _page(_tg_block(5, text=None), '<div class="tgme_widget_message_wrap"><div>无 data-post</div></div>')
    raws = tg.parse_blocks(page)
    assert [r['post_id'] for r in raws] == [5]
    item = tg.to_item(raws[0], 'fakechan', 'ru', CUTOFF)
    assert item['summary'] == '' and item['title'].startswith('[Telegram] fakechan#5')
    assert item['metadata']['forwarded'] is False and item['metadata']['has_media'] is False


def test_telegram_cutoff_filters_item():
    old = tg.parse_blocks(_page(_tg_block(7, hours=30)))[0]
    assert tg.to_item(old, 'fakechan', 'ru', CUTOFF) is None


def test_telegram_paging_walks_before_and_stops_at_cutoff():
    pages = {
        None: _page(_tg_block(30, hours=1), _tg_block(31, hours=0.5)),
        30: _page(_tg_block(20, hours=10), _tg_block(21, hours=9)),
        20: _page(_tg_block(10, hours=40), _tg_block(11, hours=23)),   # 含时窗外 -> 取完本页即停
        10: _page(_tg_block(1, hours=50)),                              # 不应被请求
    }
    calls = []

    def getter(url, params):
        before = (params or {}).get('before')
        calls.append(before)
        return _resp(pages[before])

    with mock.patch.object(tg, 'CHANNELS', {'fakechan': 'ru'}):
        items = tg.fetch_telegram(CUTOFF, getter, lambda s: None)
    assert calls == [None, 30, 20]
    assert sorted(i['metadata']['post_id'] for i in items) == [11, 20, 21, 30, 31]


def test_telegram_stops_on_empty_and_max_pages():
    def endless(url, params):
        before = (params or {}).get('before') or 1000
        return _resp(_page(_tg_block(before - 1, hours=1)))
    calls = []

    def getter(url, params):
        calls.append(1)
        return endless(url, params)

    with mock.patch.object(tg, 'CHANNELS', {'fakechan': 'ru'}):
        tg.fetch_telegram(CUTOFF, getter, lambda s: None)
    assert len(calls) == tg.MAX_PAGES
    with mock.patch.object(tg, 'CHANNELS', {'fakechan': 'ru'}):
        assert tg.fetch_telegram(CUTOFF, lambda u, p: _resp('<html></html>'), lambda s: None) == []


def test_telegram_failure_degrades():
    def boom(url, params):
        raise RuntimeError('net down')

    with mock.patch.object(tg, 'CHANNELS', {'a': 'ru', 'b': 'ru'}):
        try:
            tg.fetch_telegram(CUTOFF, boom, lambda s: None)
            assert False, '全部失败应抛错'
        except RuntimeError:
            pass

        def half(url, params):
            if '/s/a' in url:
                raise RuntimeError('x')
            return _resp(_page(_tg_block(3, hours=1, channel='b')))
        assert len(tg.fetch_telegram(CUTOFF, half, lambda s: None)) == 1

    # 第二页失败：保留第一页已取条目
    def second_fails(url, params):
        if params:
            raise RuntimeError('page2')
        return _resp(_page(_tg_block(50, hours=1), _tg_block(51, hours=1)))
    with mock.patch.object(tg, 'CHANNELS', {'fakechan': 'ru'}):
        assert len(tg.fetch_telegram(CUTOFF, second_fails, lambda s: None)) == 2


def test_telegram_dedup_keys():
    a = tg.to_item(tg.parse_blocks(_page(_tg_block(9, text='v1')))[0], 'fakechan', 'ru', CUTOFF)
    b = tg.to_item(tg.parse_blocks(_page(_tg_block(9, text='v2 已编辑', views='9K')))[0], 'fakechan', 'ru', CUTOFF)
    want = 'https://t.me/fakechan/9'
    assert collect_global.dedup_key(a) == collect_global.dedup_key(b) == want
    assert archive_platforms.item_key(a) == archive_platforms.item_key(b) == want


# ───────────────────────── Misskey ─────────────────────────

def _ms_iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%S.123Z')


def _note(nid, text='忘却前夜のガチャ回した', hours=1, name='自造昵称', username='user_a', uid='uid0001',
          cw=None, renotes=1, replies=2, reactions=None, files=None):
    return {'id': nid, 'createdAt': _ms_iso(hours), 'userId': uid,
            'user': {'id': uid, 'name': name, 'username': username, 'host': None},
            'text': text, 'cw': cw, 'renoteCount': renotes, 'repliesCount': replies,
            'reactions': reactions if reactions is not None else {':a:': 3, ':b@.:': 1}, 'files': files or []}


def _mresp(notes):
    r = mock.Mock()
    r.json.return_value = notes
    return r


def test_misskey_parse_fields_and_layout():
    item = mk.parse_note(_note('n001', cw='ネタバレ注意'), 'misskey.io', '忘却前夜', CUTOFF)
    assert item['url'] == 'https://misskey.io/notes/n001'
    assert item['author'] == '自造昵称' and item['source'] == 'misskey' and item['lang'] == ''
    assert item['metadata']['author_id'] == 'uid0001@misskey.io'
    assert item['summary'] == 'ネタバレ注意\n忘却前夜のガチャ回した'
    assert item['engagement'] == 1 + 2 + 4
    assert item['metadata']['has_cw'] is True and 'keyword_only' not in item['metadata']
    assert item['time'] == (NOW - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    assert archive_layout.resolve_write_layout('misskey') == ('misskey', None, None)
    assert archive_layout.build_relpath('misskey', None, None, '2026-10-03').as_posix() == 'misskey/2026-10-03.json'


def test_misskey_author_falls_back_to_username_and_handles_odd_reactions():
    n = _note('n002', name=None, reactions=None)
    n['reactions'] = None
    item = mk.parse_note(n, 'misskey.io', '忘却前夜', CUTOFF)
    assert item['author'] == 'user_a' and item['engagement'] == 3


def test_misskey_noise_classification():
    assert mk.parse_note(_note('a', text='今日の天気'), 'misskey.io', '忘却前夜', CUTOFF) is None
    # 拉丁词无线索词 -> keyword_only；带线索词 -> keep
    ko = mk.parse_note(_note('b', text='morimens'), 'misskey.io', 'morimens', CUTOFF)
    assert ko['metadata']['keyword_only'] is True
    kp = mk.parse_note(_note('c', text='morimens ガチャ'), 'misskey.io', 'morimens', CUTOFF)
    assert 'keyword_only' not in kp['metadata']
    # 非拉丁关键词本身即游戏名，不标 keyword_only
    assert 'keyword_only' not in mk.parse_note(_note('d', text='忘却前夜'), 'misskey.io', '忘却前夜', CUTOFF)['metadata']


def test_misskey_cutoff():
    assert mk.parse_note(_note('old', hours=30), 'misskey.io', '忘却前夜', CUTOFF) is None


def test_misskey_paging_until_id_and_since_date():
    full = [_note(f'p1-{i:03d}', hours=1) for i in range(mk.PAGE_LIMIT)]
    page2 = [_note('p2-001', hours=5), _note('p2-002', hours=40)]   # 不足一页 -> 即停
    bodies = []

    def poster(url, body):
        bodies.append((url, dict(body)))
        return _mresp(full if 'untilId' not in body else page2)

    with mock.patch.object(mk, 'KEYWORDS', ['忘却前夜']):
        items = mk.fetch_misskey(CUTOFF, poster, lambda s: None)
    assert len(bodies) == 2
    assert bodies[0][0] == 'https://misskey.io/api/notes/search'
    assert bodies[0][1]['sinceDate'] == int(CUTOFF.timestamp() * 1000) and bodies[0][1]['limit'] == 100
    assert bodies[1][1]['untilId'] == full[-1]['id']
    assert len(items) == mk.PAGE_LIMIT + 1   # p2-002 在时窗外被滤


def test_misskey_stops_when_page_all_outside_window_and_empty():
    full_old = [_note(f'o{i:03d}', hours=48) for i in range(mk.PAGE_LIMIT)]
    calls = []

    def poster(url, body):
        calls.append(1)
        return _mresp(full_old)
    with mock.patch.object(mk, 'KEYWORDS', ['忘却前夜']):
        assert mk.fetch_misskey(CUTOFF, poster, lambda s: None) == []
    assert len(calls) == 1
    with mock.patch.object(mk, 'KEYWORDS', ['忘却前夜']):
        assert mk.fetch_misskey(CUTOFF, lambda u, b: _mresp([]), lambda s: None) == []


def test_misskey_merge_dedup_and_failure_degrade():
    def poster(url, body):
        if body['query'] == 'morimens':
            return _mresp([_note('same', text='morimens ガチャ'), _note('x2', text='morimens')])
        return _mresp([_note('same', text='忘却前夜 morimens')])
    items = mk.fetch_misskey(CUTOFF, poster, lambda s: None)
    by = {i['url']: i for i in items}
    assert set(by) == {'https://misskey.io/notes/same', 'https://misskey.io/notes/x2'}
    assert by['https://misskey.io/notes/x2']['metadata']['keyword_only'] is True

    def half(url, body):
        if body['query'] == 'morimens':
            raise RuntimeError('x')
        return _mresp([_note('k1')])
    assert len(mk.fetch_misskey(CUTOFF, half, lambda s: None)) == 1

    def boom(url, body):
        raise RuntimeError('down')
    try:
        mk.fetch_misskey(CUTOFF, boom, lambda s: None)
        assert False
    except RuntimeError:
        pass


def test_misskey_dedup_keys():
    a = mk.parse_note(_note('zz', text='忘却前夜 v1'), 'misskey.io', '忘却前夜', CUTOFF)
    b = mk.parse_note(_note('zz', text='忘却前夜 v2', name='改名'), 'misskey.io', '忘却前夜', CUTOFF)
    want = 'https://misskey.io/notes/zz'
    assert collect_global.dedup_key(a) == collect_global.dedup_key(b) == want
    assert archive_platforms.item_key(a) == archive_platforms.item_key(b) == want


# ───────────────────────── 登记 ─────────────────────────

def test_registered_as_sparse_sources_and_wired():
    import global_collectors
    reg = json.loads((Path(sources.__file__).parent / 'tier_registry.json').read_text(encoding='utf-8'))
    for name, fn in (('telegram', 'fetch_telegram'), ('misskey', 'fetch_misskey')):
        assert name in sources.KNOWN_SOURCES and name in sources.SPARSE_SOURCES
        assert name in sources.ARCHIVE_PLATFORMS
        assert collect_global.SOURCE_MAP[name] == name
        assert callable(getattr(global_collectors, fn))
        assert reg['modules'][f'{name}_collector']['tier'] == 'T1'
