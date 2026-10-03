"""迁仓后实际采集路径的回归：时间语义、滚动覆盖和未知字段。"""
from datetime import datetime, timedelta, UTC
from unittest import mock

import pytest

import global_collectors as gc
import news_common as common
from archive_platforms import item_key


def topic(identifier, *, last=None, created=None):
    stamps = ''
    for klass, value in [('forum_topic_lastpost', last), ('forum_topic_created', created)]:
        if value is not None:
            stamps += f'<div data-timestamp="{value}" class="{klass} extra"></div>'
    return (f'<div class="forum_topic unread"><a class="forum_topic_overlay" '
            f'href="https://steamcommunity.com/app/3052450/discussions/0/{identifier}/"></a>'
            f'<div class="forum_topic_name">讨论 {identifier}</div>{stamps}</div>')


def steam_items(html, pages=1):
    response = mock.Mock(text=html)
    with mock.patch.object(gc.requests, 'get', return_value=response), mock.patch.object(gc.time, 'sleep'):
        return gc._fetch_steam_discussions_one('3052450', 'global', max_pages=pages)


def test_steam_last_reply_is_not_invented_creation_time():
    now = datetime.now(UTC).replace(microsecond=0)
    item = steam_items(topic(1, last=int(now.timestamp())))[0]
    valid, cleaned = common.validate_news_item(item)
    assert valid
    assert cleaned['time_basis'] == 'last_reply_at'
    assert cleaned['time'] == cleaned['last_reply_at'] == now.isoformat()
    assert cleaned['created_at'] is None
    assert datetime.fromisoformat(cleaned['fetched_at']) >= now


@pytest.mark.parametrize('stamp', [None, 'garbage', '999999999999999999999'])
def test_steam_missing_or_bad_timestamp_uses_marked_fetch_time(stamp):
    item = steam_items(topic(1, last=stamp))[0]
    valid, cleaned = common.validate_news_item(item)
    assert valid and cleaned['time_is_approximate']
    assert cleaned['time_basis'] == 'fetched_at'
    assert cleaned['time'] == cleaned['fetched_at']
    assert cleaned['last_reply_at'] is None
    assert cleaned['created_at'] is None


def test_steam_unverified_creation_element_does_not_invent_creation_time():
    now = datetime.now(UTC).replace(microsecond=0)
    created = now - timedelta(days=20)
    item = steam_items(topic(1, last=int(now.timestamp()), created=int(created.timestamp())))[0]
    assert item['created_at'] is None
    assert item['last_reply_at'] == now.isoformat()
    assert item['time_basis'] == 'last_reply_at'


def test_steam_creation_without_verified_provenance_stays_unknown():
    now = datetime.now(UTC).replace(microsecond=0)
    item = steam_items(topic(1, created=int(now.timestamp())))[0]
    assert item['time_basis'] == 'fetched_at'
    assert item['created_at'] is None
    assert item.get('time_is_approximate')


def test_steam_skips_old_pinned_thread_and_visits_page_one():
    now = datetime.now(UTC)
    old = int((now - timedelta(days=365)).timestamp())
    first = mock.Mock(text=topic('pinned', last=old) + topic(1, last=int(now.timestamp())))
    second = mock.Mock(text=topic(2, last=int(now.timestamp())))
    third = mock.Mock(text=topic(2, last=int(now.timestamp())))
    with mock.patch.object(gc.requests, 'get', side_effect=[mock.Mock(text=''), first, second, third]) as get, mock.patch.object(gc.time, 'sleep'):
        items = gc._fetch_steam_discussions_one('3052450', 'global', max_pages=3, fetch_replies=False)
    assert len(items) == 2
    assert get.call_args_list[2].args[0].endswith('?fp=1')
    assert get.call_args_list[3].args[0].endswith('?fp=2')
    assert item_key(items[0]) == item_key(dict(items[0], last_reply_at='later', fetched_at='later'))
