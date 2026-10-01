"""日本发行方 PR TIMES 官方新闻：公开 RSS 发现，正文页补充内容和图片。"""

import logging
import re
from datetime import datetime, timedelta, UTC
from html.parser import HTMLParser
from urllib.parse import urlsplit

import news_common

logger = logging.getLogger(__name__)
FEED_URL = 'https://prtimes.jp/companyrdf.php?company_id=53906'
RSS = '{http://purl.org/rss/1.0/}'
DC = '{http://purl.org/dc/elements/1.1/}'
ARTICLE_PATH = re.compile(r'^/main/html/rd/p/\d{9}\.000053906\.html$')


def _article_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.netloc == 'prtimes.jp' and bool(ARTICLE_PATH.fullmatch(parsed.path)) and not parsed.query and not parsed.fragment


def parse_feed(text, cutoff):
    """只接受本游戏、本发行方、有真实带时区发布日期的条目。"""
    root = news_common.parse_xml_safely(text)
    if root.tag != '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF':
        raise ValueError('PR TIMES response is not an RDF feed')
    items, seen = [], set()
    for entry in root.findall(RSS + 'item'):
        title = entry.findtext(RSS + 'title', '')
        url = entry.findtext(RSS + 'link', '').strip()
        if not any(keyword in title for keyword in ('忘却前夜', '忘卻前夜', 'Morimens')):
            continue
        if not _article_url(url) or url in seen:
            continue
        try:
            published = datetime.fromisoformat(entry.findtext(DC + 'date', ''))
            if published.tzinfo is None:
                raise ValueError('missing timezone')
        except ValueError:
            logger.warning('PR TIMES: skipping entry without reliable publication time: %s', url)
            continue
        if published < cutoff:
            continue
        seen.add(url)
        summary = entry.findtext(RSS + 'description', '')
        image_match = re.search(r'\[画像\d+:\s*(https://prtimes\.jp/[^\s\]]+)', summary)
        item = news_common.make_item(
            title=title, summary=summary, source='prtimes', platform_region='jp',
            time_str=published.astimezone(UTC).isoformat(), url=url,
            author=entry.findtext(DC + 'corp', ''), lang='ja',
            region='jp', archive_subtype='news',
            media_url=image_match.group(1) if image_match else '', content_type='image' if image_match else 'text',
        )
        item['metadata'] = {'publisher': 'PR TIMES', 'summary_is_excerpt': True,
                            'engagement_is_unknown': True,
                            'fetched_at': datetime.now(UTC).isoformat(),
                            'media_urls': [item['media_url']] if item['media_url'] else []}
        items.append(item)
    return items


class _BodyParser(HTMLParser):
    """读取实际页面的 press-release-body，排除页眉、推荐和脚本。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.hidden = 0
        self.parts = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.depth:
            if attrs.get('id') == 'press-release-body':
                self.depth = 1
            return
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.depth += 1
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'p', 'div', 'br', 'h1', 'h2', 'h3', 'li', 'tr'}:
            self.parts.append('\n')
        if tag == 'img' and not self.hidden:
            url = attrs.get('src', '')
            parsed = urlsplit(url)
            if parsed.scheme == 'https' and parsed.netloc in {'prtimes.jp', 'prcdn.freetls.fastly.net'} and url not in self.images:
                self.images.append(url)

    def handle_endtag(self, tag):
        if self.depth:
            if tag in {'script', 'style'} and self.hidden:
                self.hidden -= 1
            if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
                self.depth -= 1
            if tag in {'p', 'div', 'li', 'tr'}:
                self.parts.append('\n')

    def handle_data(self, data):
        if self.depth and not self.hidden:
            self.parts.append(data)


def parse_article(text):
    parser = _BodyParser()
    parser.feed(text)
    body = '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
    if not body:
        raise ValueError('PR TIMES article body missing (possible challenge or markup change)')
    return body, parser.images


def fetch_prtimes():
    hours = max(news_common.env_int('SPARSE_MAX_AGE_HOURS', 30 * 24), news_common.env_int('HOURS_LOOKBACK', 0))
    items = parse_feed(news_common.get_with_retry(FEED_URL).text,
                       datetime.now(UTC) - timedelta(hours=hours))
    for item in items:
        try:
            body, images = parse_article(news_common.get_with_retry(item['url']).text)
            item['summary'] = body
            item['metadata']['summary_is_excerpt'] = False
            item['metadata']['media_urls'] = images or item['metadata']['media_urls']
            if images:
                item['media_url'] = images[0]
                item['content_type'] = 'image'
        except Exception as exc:
            # RSS 保留真实信息，但显式标出正文未取得，不能把摘要说成全文。
            item['metadata']['article_fetch_failed'] = True
            logger.warning('PR TIMES article fetch failed: %s: %s', item['url'], exc)
    logger.info('PR TIMES: %d releases', len(items))
    return items
