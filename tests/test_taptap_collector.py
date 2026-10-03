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
