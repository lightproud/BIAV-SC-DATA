"""HTML DOM contracts and bounded scroll traversal; no live browser/network."""
from pathlib import Path
import re
from unittest import mock
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

import _paths  # noqa: F401
import collect_global
import global_collectors
import news_common
import playwright_collectors as pc
from test_playwright_collectors import _install_fake_playwright
from weibo_common import parse_count


class Element:
    def __init__(self, node):
        self.node = node

    def inner_text(self):
        return ''.join(self.node.itertext())

    def get_attribute(self, name):
        return self.node.get(name)

    def query_selector_all(self, selector):
        def matches(node, part):
            if ' ' in part:  # No descendant selectors used by these fixtures.
                return False
            if part.startswith('.'):
                return part[1:] in node.get('class', '').split()
            match = re.fullmatch(r'(\w*)\[([\w-]+)(\*?)="([^"]+)"\]', part)
            if match:
                tag, attr, contains, value = match.groups()
                actual = node.get(attr, '')
                return (not tag or node.tag == tag) and (value in actual if contains else value == actual)
            return node.tag == part
        parts = [part.strip() for part in selector.split(',')]
        return [Element(node) for node in self.node.iter() if node is not self.node
                and any(matches(node, part) for part in parts)]

    def query_selector(self, selector):
        return next(iter(self.query_selector_all(selector)), None)


FIXTURE = ET.parse(Path(__file__).parent / 'fixtures/weibo_search.html').getroot()


def articles(snapshot):
    section = next(node for node in FIXTURE if node.get('id') == snapshot)
    return Element(section).query_selector_all('article')


class Page:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.position = 0
        self.scrolls = 0
        self.visits = []

    def goto(self, url, **kwargs):
        self.visits.append(url)
        self.position = 0

    def set_default_timeout(self, ms):
        pass

    def wait_for_timeout(self, ms):
        pass

    def query_selector_all(self, selector):
        return self.snapshots[self.position]

    def evaluate(self, expression):
        self.scrolls += 1
        self.position = min(self.position + 1, len(self.snapshots) - 1)


def test_visible_fields_and_real_zero():
    item = pc._parse_weibo_article(articles('first')[0])
    assert item['author'] == '测试作者'
    assert item['url'] == 'https://m.weibo.cn/detail/10001'
    assert item['engagement'] == 12002
    assert item['metadata']['comments_count'] == 0
    assert item['metadata']['engagement_total'] == 12002
    assert item['metadata']['counts_are_approximate'] is True


def test_missing_counts_are_null_through_validation_and_conversion():
    item = pc._parse_weibo_article(articles('unknown')[0])
    valid, cleaned = news_common.validate_news_item(item)
    assert valid
    converted = collect_global.convert_item(cleaned)
    metadata = converted['metadata']
    assert metadata['engagement_is_unknown'] is True
    assert metadata['engagement_total'] is None
    assert metadata['author_is_unknown'] is True
    assert all(metadata[key] is None for key in ('reposts_count', 'comments_count', 'likes_count'))
    assert converted['engagement'] == 0  # Numeric sort fallback, not measured zero.


def test_footer_labels_extract_only_visible_counts():
    item = pc._parse_weibo_article(articles('second')[0])
    assert item['metadata']['comments_count'] == 3
    assert item['metadata']['reposts_count'] is None
    assert item['metadata']['likes_count'] is None
    assert item['metadata']['engagement_total'] is None


def test_scroll_collects_second_screen_dedups_and_terminates():
    page = Page([articles('first'), articles('first') + articles('second')])
    items = pc._collect_weibo_search(page, set(), 20)
    assert len(items) == 2
    assert page.scrolls == 3  # Two consecutive snapshots without new entries stop.


def test_empty_and_budget_termination():
    page = Page([[]])
    assert pc._collect_weibo_search(page, set(), 20) == []
    assert page.scrolls == 1


def test_scroll_failure_keeps_already_collected_items():
    page = Page([articles('first')])
    with mock.patch.object(page, 'evaluate', side_effect=RuntimeError('page closed')):
        assert len(pc._collect_weibo_search(page, set(), 5)) == 1
    page = Page([articles('first'), articles('second')])
    assert len(pc._collect_weibo_search(page, set(), 1)) == 1
    assert page.scrolls == 1


def test_keywords_share_dedup():
    page = Page([articles('first'), articles('second')])
    fake = _install_fake_playwright(page)
    with mock.patch.dict('sys.modules', {'playwright.sync_api': fake}), \
            mock.patch.dict('os.environ', {'WEIBO_COOKIE': ''}):
        items = pc.fetch_weibo_playwright()
    assert len(items) == 2
    queries = [parse_qs(urlparse(url).query)['containerid'][0] for url in page.visits]
    assert queries == ['100103type=1&q=忘却前夜', '100103type=1&q=忘卻前夜']


def test_cookie_is_applied_and_browser_closes_on_navigation_failure():
    page = mock.MagicMock()
    page.goto.side_effect = RuntimeError('navigation failure')
    browser = mock.MagicMock()
    browser.new_page.return_value = page
    playwright = mock.MagicMock()
    playwright.chromium.launch.return_value = browser
    sync_api = mock.MagicMock()
    sync_api.sync_playwright.return_value.__enter__.return_value = playwright
    with mock.patch.dict('sys.modules', {'playwright.sync_api': sync_api}), \
            mock.patch.dict('os.environ', {'WEIBO_COOKIE': 'SUB=test-only; other=a=b'}):
        assert pc.fetch_weibo_playwright() == []
    page.context.add_cookies.assert_called_once_with([
        {'name': 'SUB', 'value': 'test-only', 'domain': '.weibo.cn', 'path': '/'},
        {'name': 'other', 'value': 'a=b', 'domain': '.weibo.cn', 'path': '/'},
    ])
    assert page.goto.call_count == 2
    browser.close.assert_called_once()


def test_api_missing_fields_follow_same_unknown_contract():
    items = []
    global_collectors._collect_weibo_cards([{'mblog': {
        'id': '10003', 'text': '忘却前夜足够长的测试正文内容', 'user': None,
        'comments_count': 0, 'attitudes_count': None}}], items)
    assert items[0]['metadata']['comments_count'] == 0
    assert items[0]['metadata']['likes_count'] is None
    assert items[0]['metadata']['engagement_total'] is None
    assert items[0]['metadata']['author_is_unknown'] is True


def test_unknown_labels_are_not_zero():
    assert parse_count('赞') is None
    assert parse_count('转发 0') == 0
    assert parse_count('1,234') == 1234
    assert parse_count('-1') is None
