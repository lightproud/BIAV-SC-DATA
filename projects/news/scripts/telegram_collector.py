"""Telegram 公开频道采集（守密人 2026-10-03 批准）。

接口：公开预览页 https://t.me/s/<频道>，免登录；机房 IP 实测 200。翻旧页用 ?before=<最小 post id>。
频道清单常量化（CHANNELS），目前只有 Morimens（俄语官方信息频道）。解析 tgme_widget_message 块：
post id（data-post）、时间（<time datetime>）、正文（<br> 转换行后去 HTML）、浏览数（tgme_widget_message_views，K/M 换算）、
是否转发（forwarded_from）、有无媒体（图 / 视频 / 文档 / 贴纸 / 语音）。

字段落点：source=telegram / platform_region=global；url=https://t.me/<频道>/<id>（去重键即此 URL，
collect_global.dedup_key 与 archive_platforms.item_key 都是 URL 优先）；author=频道显示名（缺则频道名），
频道名放 metadata.author_id；engagement 记 0（浏览数不等于互动，浏览数放 metadata.views）。
metadata：kind=channel_post（官方单向发布、无玩家评论，供舆情侧当事件 / 公告用）、is_official_channel=True、
views、lang（频道常量，ru）、forwarded、has_media、channel、post_id。
未登记区服 / 子类，归档平铺 Record/Community/telegram/<日期>.json（resolve_write_layout 默认）。

稳健：翻页上限 MAX_PAGES、请求间隔 REQUEST_DELAY、只取 cutoff 之内；某页失败时保留已取到的条目只告警；
某频道首页即失败则降级跳过，全部频道都失败才抛错交编排层记失败。
"""
import html as _html
import json
import logging
import re
import time
from datetime import datetime, timedelta, UTC

import news_common

logger = logging.getLogger(__name__)

# 频道名 -> 频道语言
CHANNELS = {
    'Morimens': 'ru',   # 俄语官方信息频道（Morimens RU Info Channel）
}
BASE_URL = 'https://t.me/s/{channel}'
MAX_PAGES = 5
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 20
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; biav-sc-collector)', 'Accept': 'text/html'}
FALLBACK_HOURS = 24

_BLOCK_SPLIT = re.compile(r'<div class="tgme_widget_message_wrap')
_POST_RE = re.compile(r'data-post="([^"/]+)/(\d+)"')
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_VIEWS_RE = re.compile(r'tgme_widget_message_views">([^<]+)<')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
_OWNER_RE = re.compile(r'tgme_widget_message_owner_name"[^>]*>\s*<span[^>]*>([^<]*)</span>')
_BG_RE = re.compile(r"background-image:url\('([^']+)'\)")
_MEDIA_CLS = ('tgme_widget_message_photo_wrap', 'tgme_widget_message_video_player',
              'tgme_widget_message_roundvideo', 'tgme_widget_message_document_wrap',
              'tgme_widget_message_sticker_wrap', 'tgme_widget_message_voice')


def parse_views(s):
    """'2.56K' / '1.2M' / '834' -> int；无法解析返回 0。"""
    s = str(s or '').strip().upper().replace(',', '')
    mult = 1
    if s.endswith('K'):
        mult, s = 1000, s[:-1]
    elif s.endswith('M'):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return 0


def _parse_time(s):
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _clean_text(raw):
    raw = re.sub(r'<br\s*/?>', '\n', raw or '')
    text = _html.unescape(news_common.strip_html(raw))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_blocks(page_html):
    """预览页 HTML -> 原始消息 dict 列表（页内顺序，旧到新）。缺 post id / 时间的块跳过。"""
    out = []
    for blk in _BLOCK_SPLIT.split(page_html or '')[1:]:
        m = _POST_RE.search(blk)
        t = _TIME_RE.search(blk)
        created = _parse_time(t.group(1)) if t else None
        if not m or created is None:
            continue
        tm = _TEXT_RE.search(blk)
        views = _VIEWS_RE.search(blk)
        owner = _OWNER_RE.search(blk)
        bg = _BG_RE.search(blk)
        out.append({
            'channel': m.group(1), 'post_id': int(m.group(2)), 'created': created,
            'text': _clean_text(tm.group(1)) if tm else '',
            'views': parse_views(views.group(1)) if views else 0,
            'forwarded': 'tgme_widget_message_forwarded_from' in blk,
            'has_media': any(c in blk for c in _MEDIA_CLS),
            'owner': _html.unescape(owner.group(1)).strip() if owner else '',
            'media_url': bg.group(1) if bg else '',
        })
    return out


def post_url(channel, post_id):
    return f'https://t.me/{channel}/{post_id}'


def to_item(raw, channel, lang, cutoff=None):
    """原始消息 -> 标准 item；时窗外返回 None。"""
    if cutoff is not None and raw['created'] < cutoff:
        return None
    text = raw['text']
    title = '[Telegram] ' + (text[:60] if text else f"{channel}#{raw['post_id']}")
    item = news_common.make_item(
        title=title,
        summary=text,
        source='telegram',
        platform_region='global',
        time_str=raw['created'].strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        url=post_url(channel, raw['post_id']),
        engagement=0,
        author=raw['owner'] or channel,
        lang=lang,
        media_url=raw['media_url'],
    )
    item['metadata'] = {
        'kind': 'channel_post', 'is_official_channel': True, 'views': raw['views'], 'lang': lang,
        'forwarded': raw['forwarded'], 'has_media': raw['has_media'],
        'channel': channel, 'post_id': raw['post_id'], 'author_id': channel,
    }
    return item


def _fetch_channel(channel, lang, cutoff, getter, sleep):
    """单频道翻页：无消息 / 页内最旧已早于 cutoff / before 不再前进 / 到上限即停。"""
    items, before = [], None
    for page in range(MAX_PAGES):
        if page:
            sleep(REQUEST_DELAY)
        params = {'before': before} if before else None
        try:
            resp = getter(BASE_URL.format(channel=channel), params)
            raws = parse_blocks(resp.text)
        except Exception as e:  # noqa: BLE001
            if page == 0:
                raise
            logger.warning('Telegram %s 第 %d 页失败，保留已取 %d 条：%s', channel, page + 1, len(items), e)
            break
        if not raws:
            break
        for raw in raws:
            it = to_item(raw, channel, lang, cutoff)
            if it is not None:
                items.append(it)
        oldest = min(r['post_id'] for r in raws)
        if min(r['created'] for r in raws) < cutoff or oldest <= 1 or (before and oldest >= before):
            break
        before = oldest
    return items


def fetch_telegram(cutoff=None, getter=None, sleep=time.sleep):
    """采集各频道时窗内的帖子，按 url 去重。单频道失败只告警，全部失败才抛错。"""
    if cutoff is None:
        hours = news_common.env_int('HOURS_LOOKBACK', FALLBACK_HOURS)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
    if getter is None:
        def getter(url, params):
            return news_common.get_with_retry(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    merged, failures = {}, 0
    for idx, (channel, lang) in enumerate(CHANNELS.items()):
        if idx:
            sleep(REQUEST_DELAY)
        try:
            got = _fetch_channel(channel, lang, cutoff, getter, sleep)
        except Exception as e:  # noqa: BLE001  单频道失败降级
            failures += 1
            logger.warning('Telegram 频道 %s 失败：%s', channel, e)
            continue
        for it in got:
            merged.setdefault(it['url'], it)
        logger.info('Telegram %s: %d 条', channel, len(got))
    if failures == len(CHANNELS):
        raise RuntimeError('Telegram: all channels failed')
    return list(merged.values())


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    out = fetch_telegram()
    print(json.dumps({'count': len(out)}))
