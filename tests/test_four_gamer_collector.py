"""Synthetic RSS fixtures; live feeds are smoke-tested separately."""
import unittest
from datetime import datetime, UTC
from unittest.mock import Mock, patch

import _paths  # noqa: F401
import four_gamer_collector as feeds
import sources


def rss(*entries):
    # Synthetic, not a saved publisher response.
    body = ''.join('<item><title>' + title + '</title><link>' + url +
                   '</link><dc:date>' + date + '</dc:date></item>'
                   for title, url, date in entries)
    return ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<channel><title>Test</title></channel>' + body + '</rdf:RDF>').encode()


class TestNewsFeeds(unittest.TestCase):
    def parse(self, *entries):
        return feeds.parse_feed(rss(*entries), 'four_gamer', feeds.FEEDS['four_gamer'][0])

    def test_topic_filter_dedup_and_real_date(self):
        entry = ('忘却前夜 synthetic update', 'https://www.4gamer.net/test', '2026-09-17T17:00:00+09:00')
        items = self.parse(entry, entry, ('Other game', 'https://www.4gamer.net/other', entry[2]))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['time'], entry[2])
        self.assertEqual(item['summary'], '')
        self.assertEqual(item['region'], 'jp')
        self.assertEqual(item['archive_subtype'], 'news')
        self.assertTrue(item['metadata']['engagement_is_unknown'])
        self.assertEqual(item['metadata']['time_basis'], 'publisher_feed')

    def test_alternative_names(self):
        for title in ('忘卻前夜', 'MORIMENS update'):
            with self.subTest(title=title):
                self.assertEqual(len(self.parse((title, 'https://www.4gamer.net/test', '2026-09-17T08:00:00Z'))), 1)

    def test_bad_dates_or_foreign_urls_not_fabricated(self):
        for date, url in [('', 'https://www.4gamer.net/test'), ('invalid', 'https://www.4gamer.net/test'),
                          ('2026-09-17T08:00:00', 'https://www.4gamer.net/test'),
                          ('2026-09-17T08:00:00Z', 'https://example.org/test')]:
            with self.subTest(date=date, url=url):
                self.assertEqual(self.parse(('忘却前夜 test', url, date)), [])

    def test_html_or_invalid_xml_is_failure(self):
        for data in (b'<html><body>Blocked</body></html>', b'broken xml'):
            with self.subTest(data=data), self.assertRaises(Exception):
                feeds.parse_feed(data, 'four_gamer', feeds.FEEDS['four_gamer'][0])

    def test_valid_feed_with_no_news_is_empty(self):
        self.assertEqual(self.parse(), [])

    def test_http_errors_propagate(self):
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError('HTTP 503')
        with patch.object(feeds.requests, 'get', return_value=response), self.assertRaises(RuntimeError):
            feeds.fetch_four_gamer()

    def test_four_gamer_cross_platform_dedup(self):
        response = Mock(content=rss(('忘却前夜 synthetic', 'https://www.4gamer.net/test', datetime.now(UTC).isoformat())))
        with patch.object(feeds.requests, 'get', return_value=response) as get:
            items = feeds.fetch_four_gamer()
        self.assertEqual(get.call_count, len(feeds.FEEDS['four_gamer']))
        self.assertEqual(len(items), 1)

    def test_historical_feed_does_not_report_new_output(self):
        response = Mock(content=rss(('忘却前夜 old', 'https://www.4gamer.net/test', '2020-01-01T00:00:00Z')))
        with patch.object(feeds.requests, 'get', return_value=response):
            self.assertEqual(feeds.fetch_four_gamer(), [])

    def test_sources_registered_sparse_and_archived(self):
        for source in feeds.FEEDS:
            self.assertIn(source, sources.KNOWN_SOURCES)
            self.assertIn(source, sources.ARCHIVE_PLATFORMS)
            self.assertIn(source, sources.SPARSE_SOURCES)

    def test_validation_and_archive_layout(self):
        import news_common
        import archive_layout
        item = self.parse(('忘却前夜 test', 'https://www.4gamer.net/test', '2026-09-17T17:00:00+09:00'))[0]
        ok, cleaned = news_common.validate_news_item(item)
        self.assertTrue(ok)
        self.assertEqual(cleaned['metadata'], item['metadata'])
        self.assertEqual(archive_layout.resolve_write_layout(cleaned['source'], cleaned['region'], cleaned['archive_subtype']),
                         ('four_gamer', 'jp', 'news'))


if __name__ == '__main__':
    unittest.main()
