"""Public publisher RSS feeds: titles/links/dates only; no article scraping.

Filter every item by game name, including a shared PC news entry.
Malformed feeds raise so the orchestrator reports failure rather than silence.
Date windows and archive deduplication remain owned by collect_global.
"""
import logging
import re
from datetime import datetime, timedelta, UTC
from urllib.parse import urlparse

import requests
import news_common

logger = logging.getLogger(__name__)
RSS = '{http://purl.org/rss/1.0/}'
DC = '{http://purl.org/dc/elements/1.1/}'
FEEDS = {
    'four_gamer': (
        'https://www.4gamer.net/games/919/G091980/contents.xml',
        'https://www.4gamer.net/games/989/G098902/contents.xml',
    ),
}
HOSTS = {'four_gamer': 'www.4gamer.net'}
AUTHORS = {'four_gamer': '4Gamer.net'}
GAME = re.compile(r'忘[却卻]前夜|\bmorimens\b', re.I)


def parse_feed(data: bytes, source: str, feed_url: str) -> list[dict]:
    root = news_common.parse_xml_safely(data.decode('utf-8-sig'))
    if root.tag != '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF' or root.find(RSS + 'channel') is None:
        raise ValueError('Expected publisher RSS 1.0 feed')
    items = []
    seen = set()
    fetched_at = datetime.now(UTC).isoformat()
    for entry in root.findall(RSS + 'item'):
        title = (entry.findtext(RSS + 'title') or '').strip()
        if not GAME.search(title):
            continue
        url = (entry.findtext(RSS + 'link') or '').strip()
        raw_date = (entry.findtext(DC + 'date') or '').strip()
        try:
            published = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
            parsed_url = urlparse(url)
            if published.tzinfo is None or parsed_url.scheme != 'https' or parsed_url.hostname != HOSTS[source]:
                raise ValueError('Missing timezone or invalid publisher URL')
        except ValueError:
            logger.warning('%s: skipping item with invalid publication date/URL', source)
            continue
        if url in seen:
            continue
        seen.add(url)
        items.append({
            'title': title, 'summary': '', 'source': source,
            'time': published.isoformat(), 'url': url,
            'engagement': 0, 'is_hot': False, 'author': AUTHORS[source],
            'tags': ['news'], 'lang': 'ja', 'platform_region': 'JP',
            'region': 'jp', 'archive_subtype': 'news',
            'metadata': {'feed_url': feed_url, 'fetched_at': fetched_at,
                         'time_basis': 'publisher_feed',
                         'engagement_is_unknown': True,
                         'engagement_basis': 'unavailable'},
        })
    return items


def fetch_source(source: str) -> list[dict]:
    items = {}
    cutoff = datetime.now(UTC) - timedelta(hours=news_common.env_int('SPARSE_MAX_AGE_HOURS', 30 * 24))
    for url in FEEDS[source]:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        for item in parse_feed(response.content, source, url):
            if datetime.fromisoformat(item['time']) >= cutoff:
                items.setdefault(item['url'], item)
    return list(items.values())


def fetch_four_gamer() -> list[dict]:
    return fetch_source('four_gamer')
