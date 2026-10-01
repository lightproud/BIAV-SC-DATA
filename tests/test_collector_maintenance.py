"""迁仓后实际采集路径的回归：时间语义、滚动覆盖和未知字段。"""
from datetime import datetime, timedelta, UTC
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

import global_collectors as gc
import news_common as common
import playwright_collectors as pc
from archive_platforms import item_key
from test_playwright_collectors import FakeEl, FakePage, _install_fake_playwright


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
    assert cleaned['time_semantics'] == 'last_reply'
    assert cleaned['time'] == cleaned['last_reply_at'] == now.isoformat()
    assert cleaned['created_at'] is None
    assert datetime.fromisoformat(cleaned['fetched_at']) >= now


@pytest.mark.parametrize('stamp', [None, 'garbage', '999999999999999999999'])
def test_steam_missing_or_bad_timestamp_uses_marked_fetch_time(stamp):
    item = steam_items(topic(1, last=stamp))[0]
    valid, cleaned = common.validate_news_item(item)
    assert valid and cleaned['time_is_approximate']
    assert cleaned['time_semantics'] == 'fetched'
    assert cleaned['time'] == cleaned['fetched_at']
    assert cleaned['last_reply_at'] is None
    assert cleaned['created_at'] is None


def test_steam_explicit_creation_and_reply_stay_distinct():
    now = datetime.now(UTC).replace(microsecond=0)
    created = now - timedelta(days=20)
    item = steam_items(topic(1, last=int(now.timestamp()), created=int(created.timestamp())))[0]
    assert item['created_at'] == created.isoformat()
    assert item['last_reply_at'] == now.isoformat()
    assert item['time_semantics'] == 'last_reply'


def test_steam_explicit_creation_can_be_used_when_reply_is_missing():
    now = datetime.now(UTC).replace(microsecond=0)
    item = steam_items(topic(1, created=int(now.timestamp())))[0]
    assert item['time_semantics'] == 'published'
    assert item['time'] == now.isoformat()
    assert not item.get('time_is_approximate')


def test_steam_skips_old_pinned_thread_and_visits_page_one():
    now = datetime.now(UTC)
    old = int((now - timedelta(days=365)).timestamp())
    first = mock.Mock(text=topic('pinned', last=old) + topic(1, last=int(now.timestamp())))
    second = mock.Mock(text=topic(2, last=int(now.timestamp())))
    third = mock.Mock(text=topic(2, last=int(now.timestamp())))
    with mock.patch.object(gc.requests, 'get', side_effect=[first, second, third]) as get, mock.patch.object(gc.time, 'sleep'):
        items = gc._fetch_steam_discussions_one('3052450', 'global', max_pages=3)
    assert len(items) == 2
    assert get.call_args_list[1].args[0].endswith('?fp=1')
    assert get.call_args_list[2].args[0].endswith('?fp=2')
    assert item_key(items[0]) == item_key(dict(items[0], last_reply_at='later', fetched_at='later'))


def article(identifier, *, footer=None, author=None):
    return FakeEl(children={
        '.weibo-text, .content, p': FakeEl(text=f'忘却前夜的足够长正文内容 {identifier}'),
        'a[href*="status"]': FakeEl(attrs={'href': f'/status/{identifier}'}) if identifier else None,
        'footer, .m-ctrl-box, .card-act': FakeEl(text=footer) if footer else None,
        '.weibo-top .m-text-cut, .card-wrap .name, .name, .m-text-cut': FakeEl(text=author) if author else None,
    })


@pytest.mark.parametrize('footer', ['转发 2\n评论 3\n赞 1.2万', '2 转发\n3 评论\n1.2万 赞'])
def test_weibo_extracts_author_and_real_counts(footer):
    item = pc._parse_weibo_article(article('1', footer=footer, author='玩家'))
    valid, cleaned = common.validate_news_item(item)
    assert valid and cleaned['author'] == '玩家'
    assert cleaned['engagement'] == 12005
    assert cleaned['metadata']['engagement_components'] == {'reposts': 2, 'comments': 3, 'likes': 12000}
    assert not cleaned.get('engagement_is_unknown')


def test_weibo_unknown_counts_survive_validation_as_unknown():
    item = pc._parse_weibo_article(article('1', footer='转发\n评论\n赞'))
    valid, cleaned = common.validate_news_item(item)
    assert valid and cleaned['engagement'] == 0
    assert cleaned['engagement_is_unknown'] and cleaned['author_is_unknown']
    assert all(value is None for value in cleaned['metadata']['engagement_components'].values())


class GrowingPage(FakePage):
    def __init__(self, fail_first=False, growing_forever=False):
        super().__init__()
        self.urls = []
        self.turn = 0
        self.scrolls = []
        self.fail_first = fail_first
        self.growing_forever = growing_forever

    def goto(self, url, **kwargs):
        self.urls.append(url)
        self.turn = 0
        if self.fail_first and len(self.urls) == 1:
            raise RuntimeError('first keyword unavailable')

    def evaluate(self, expression):
        self.turn += 1
        self.scrolls.append(expression)

    def query_selector_all(self, selector):
        count = 25 + (self.turn if self.growing_forever else min(self.turn, 1))
        return [article(str(i)) for i in range(count)] + [article('')]


def fetch_with_page(page):
    fake = _install_fake_playwright(page)
    with mock.patch.dict('sys.modules', {'playwright': mock.Mock(), 'playwright.sync_api': fake}):
        return pc.fetch_weibo_playwright()


def test_weibo_collects_more_than_twenty_scrolls_and_deduplicates_both_keywords():
    page = GrowingPage()
    items = fetch_with_page(page)
    assert len(items) == 27  # 26 URLs + 1 无 URL、近似时间的稳定正文身份
    assert len(page.urls) == 2
    queries = [parse_qs(urlparse(url).query)['containerid'][0] for url in page.urls]
    assert '忘却前夜' in queries[0] and '忘卻前夜' in queries[1]
    assert len(page.scrolls) == 6  # 每词：新增一轮，再连续两轮无新增停止


def test_weibo_navigation_failure_does_not_drop_second_keyword():
    page = GrowingPage(fail_first=True)
    assert len(fetch_with_page(page)) == 27
    assert len(page.urls) == 2


def test_weibo_infinite_growth_is_bounded():
    page = GrowingPage(growing_forever=True)
    with mock.patch.object(pc, 'WEIBO_MAX_SCROLLS', 2):
        items = fetch_with_page(page)
    assert len(items) == 28
    assert len(page.scrolls) == 4


def test_weibo_missing_counter_does_not_borrow_next_buttons_number():
    item = pc._parse_weibo_article(article('1', footer='转发\n2 评论\n赞'))
    assert item['metadata']['engagement_components'] == {'reposts': None, 'comments': 2, 'likes': None}
    assert item['engagement'] == 2 and item['engagement_is_unknown']
