"""T109 微博：作者 ID 与长微博全文补取。全部离线（mock _get / time.sleep），样本自造。"""
from unittest import mock

import _paths  # noqa: F401
import archive_platforms
import collect_global
import global_collectors as gc


def _resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    return r


def _card(mid, **kw):
    mblog = {'id': mid, 'text': f'自造正文{mid}', 'created_at': '2026-01-01 08:00',
             'reposts_count': 1, 'comments_count': 2, 'attitudes_count': 3}
    mblog.update(kw)
    return {'card_type': 9, 'mblog': mblog}


def _run(cards, get=None, budget=None, cookie=""):
    items = []
    with mock.patch.object(gc.time, 'sleep'), \
            mock.patch.object(gc, '_get', get or mock.Mock()) as g:
        gc._collect_weibo_cards(cards, items, budget, cookie)
    return items, g


def test_screen_name_used_and_id_kept():
    items, _ = _run([_card('1', user={'id': 998877, 'screen_name': '测试用户甲'})])
    assert items[0]['author'] == '测试用户甲'
    assert items[0]['metadata']['author_is_unknown'] is False
    assert items[0]['metadata']['author_id'] == '998877'


def test_missing_screen_name_keeps_id_and_marks_unknown():
    items, _ = _run([_card('2', user={'id': 5501})])
    assert items[0]['author'] == ''
    assert items[0]['metadata']['author_is_unknown'] is True
    assert items[0]['metadata']['author_id'] == '5501'


def test_no_user_at_all_has_no_author_id():
    items, _ = _run([_card('3', user=None)])
    assert items[0]['author'] == ''
    assert 'author_id' not in items[0]['metadata']
    assert items[0]['metadata']['author_is_unknown'] is True


def test_author_id_survives_convert_item_and_dedup_keys_stable():
    with_name, _ = _run([_card('4', user={'id': 7, 'screen_name': '甲'})])
    without_name, _ = _run([_card('4', user={'id': 7})])
    conv = collect_global.convert_item(without_name[0])
    assert conv['metadata']['author_id'] == '7'
    # 同一条微博前后两轮作者有无不同，两套去重键必须一致（均 URL 优先）
    assert collect_global.dedup_key(with_name[0]) == collect_global.dedup_key(without_name[0])
    assert archive_platforms.item_key(collect_global.convert_item(with_name[0])) == \
        archive_platforms.item_key(conv)


def test_long_text_fetched_and_html_stripped():
    get = mock.Mock(return_value=_resp(
        {'ok': 1, 'data': {'longTextContent': '第一段<br/>第二段 <a href="x">链接</a>&amp;尾'}}))
    items, g = _run([_card('10', isLongText=True)], get, [5], "SUB=x")
    assert items[0]['summary'] == '第一段\n第二段 链接&尾'
    assert 'long_text_truncated' not in items[0]['metadata']
    args, kwargs = g.call_args
    assert args[0] == gc.WEIBO_EXTEND_URL
    assert kwargs['params'] == {'id': '10'}
    assert kwargs['headers']['Cookie'] == "SUB=x"


def test_long_text_failure_degrades_to_truncated():
    get = mock.Mock(side_effect=RuntimeError('302 login'))
    items, _ = _run([_card('11', isLongText=True)], get, [5])
    assert items[0]['summary'] == '自造正文11'
    assert items[0]['metadata']['long_text_truncated'] is True


def test_long_text_empty_payload_degrades():
    get = mock.Mock(return_value=_resp({'ok': 0, 'data': None}))
    items, _ = _run([_card('12', isLongText=True)], get, [5])
    assert items[0]['metadata']['long_text_truncated'] is True


def test_budget_caps_requests():
    get = mock.Mock(return_value=_resp({'data': {'longTextContent': '全文'}}))
    cards = [_card(str(20 + i), isLongText=True) for i in range(4)]
    items, g = _run(cards, get, [2])
    assert g.call_count == 2
    assert [i['summary'] for i in items] == ['全文', '全文', '自造正文22', '自造正文23']
    assert [bool(i['metadata'].get('long_text_truncated')) for i in items] == [False, False, True, True]


def test_short_post_never_requests_extend():
    items, g = _run([_card('30')], budget=[5])
    assert g.call_count == 0
    assert items[0]['summary'] == '自造正文30'


def test_fetch_weibo_shares_budget_across_pages(monkeypatch):
    monkeypatch.setattr(gc, 'WEIBO_LONGTEXT_MAX_PER_RUN', 3)
    monkeypatch.setattr(gc, 'WEIBO_MAX_PAGES', 2)
    monkeypatch.setattr(gc.time, 'sleep', lambda *_: None)
    counter = {'n': 0}

    def fake_get(url, params=None, headers=None, timeout=15):
        if url == gc.WEIBO_EXTEND_URL:
            return _resp({'data': {'longTextContent': '全文'}})
        counter['n'] += 1
        base = counter['n'] * 10
        return _resp({'data': {'cards': [_card(str(base + i), isLongText=True) for i in range(3)]}})

    monkeypatch.setattr(gc, '_get', fake_get)
    items = gc.fetch_weibo()
    full = [i for i in items if i['summary'] == '全文']
    assert len(full) == 3  # 全轮（两关键词 x 两页）共享上限 3
    assert len(items) > 3
