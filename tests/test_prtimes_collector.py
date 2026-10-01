import json
from datetime import datetime, UTC
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import prtimes_collector as pc
import news_common
import collect_global
import archive_platforms

FIXTURES = Path(__file__).parent / 'fixtures'
FEED = (FIXTURES / 'prtimes_feed.xml').read_text()
ARTICLE = (FIXTURES / 'prtimes_article.html').read_text()
CUTOFF = datetime(2026, 9, 1, tzinfo=UTC)


def test_feed_filters_noise_deduplicates_and_preserves_real_time():
    items = pc.parse_feed(FEED, CUTOFF)
    assert len(items) == 1
    item = items[0]
    assert item['time'] == '2026-09-17T08:30:02+00:00'
    assert item['author'] == '株式会社オルトプラス'
    assert item['media_url'] == 'https://prtimes.jp/i/53906/355/resize/test.png'
    assert item['metadata']['engagement_is_unknown'] is True
    assert item['metadata']['summary_is_excerpt'] is True
    assert item['region'] == 'jp' and item['archive_subtype'] == 'news'


def test_body_excludes_chrome_scripts_and_untrusted_images():
    body, images = pc.parse_article(ARTICLE)
    assert '第一段落 & 第二段落' in body
    assert '続きの本文' in body
    assert 'おすすめ' not in body and '関連ニュース' not in body and 'hidden script' not in body
    assert images == ['https://prcdn.freetls.fastly.net/release_image/53906/355/test.png']


@pytest.mark.parametrize('text', ['<html>captcha</html>', '<rss><channel/></rss>'])
def test_invalid_feed_is_failure_instead_of_silent_empty(text):
    with pytest.raises(ValueError):
        pc.parse_feed(text, CUTOFF)


def test_xml_entities_rejected():
    with pytest.raises(ValueError):
        pc.parse_feed('<!DOCTYPE rdf [<!ENTITY x "bad">]><rdf/>', CUTOFF)


def test_body_missing_raises():
    with pytest.raises(ValueError):
        pc.parse_article('<html>captcha</html>')


def test_live_path_enriches_body_without_network():
    item = pc.parse_feed(FEED, CUTOFF)[0]
    with mock.patch.object(pc, 'parse_feed', return_value=[item]), mock.patch.object(
            news_common, 'get_with_retry', side_effect=[SimpleNamespace(text=FEED), SimpleNamespace(text=ARTICLE)]):
        result = pc.fetch_prtimes()[0]
    assert result['metadata']['summary_is_excerpt'] is False
    assert result['summary'].endswith('続きの本文')
    assert result['media_url'].startswith('https://prcdn.freetls.fastly.net/')


def test_article_failure_preserves_marked_feed_excerpt():
    item = pc.parse_feed(FEED, CUTOFF)[0]
    with mock.patch.object(pc, 'parse_feed', return_value=[item]), mock.patch.object(
            news_common, 'get_with_retry', side_effect=[SimpleNamespace(text=FEED), RuntimeError('offline')]):
        result = pc.fetch_prtimes()[0]
    assert result['metadata']['summary_is_excerpt'] is True
    assert result['metadata']['article_fetch_failed'] is True
    assert result['time'] == item['time']


def test_validation_archive_and_repeat_dedup(tmp_path):
    item = pc.parse_feed(FEED, CUTOFF)[0]
    valid, clean = news_common.validate_news_item(collect_global.convert_item(item))
    assert valid and clean['metadata']['summary_is_excerpt'] is True
    with mock.patch.object(archive_platforms, 'ARCHIVE_DIR', tmp_path), mock.patch.object(
            archive_platforms, 'load_news', return_value=[clean]):
        assert archive_platforms.archive_all(None, '2026-10-01') == {'prtimes': 1}
        archive_platforms.archive_all(None, '2026-10-01')
    data = json.loads((tmp_path / 'prtimes/jp/news/2026-09-17.json').read_text())
    assert data['item_count'] == 1
    assert data['items'][0]['metadata']['media_urls'] == item['metadata']['media_urls']
