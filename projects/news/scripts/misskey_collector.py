"""Misskey（日本 Fediverse）公开笔记搜索采集（守密人 2026-10-03 批准）。

接口：POST https://<实例>/api/notes/search，JSON {"query", "limit", "sinceDate"(毫秒)}，免 token；机房 IP 实测 200。
实例清单常量化（INSTANCES，目前 misskey.io；misskey.design 可按需加入）。关键词：忘却前夜 / morimens，逐词检索后按 url 去重合并。
翻页用 untilId（取本页最旧笔记 id，Misskey id 随时间递增），不足一页 / 整页都在时窗外 / id 不前进即停。

字段落点：source=misskey / platform_region=jp；url=https://<实例>/notes/<id>（去重键即此 URL）；
author=user.name（缺则 username）；metadata.author_id=<userId>@<实例>（userId 是该实例内的稳定 ID）；
summary=text（有 cw 时「cw 原文 + 换行 + 正文」）；time=createdAt；engagement=转发数+回复数+反应总数；
lang 留空。同名噪声：「忘却前夜」「morimens」可能混入同名无关内容，正文无游戏线索词时标 metadata.keyword_only=True
（与 bluesky 同做法）；命中词不在正文 / cw 里的丢弃。
未登记区服 / 子类，归档平铺 Record/Community/misskey/<日期>.json。

稳健：每词翻页上限 MAX_PAGES、请求间隔 REQUEST_DELAY、只取 cutoff 之内（sinceDate + 本地过滤）；
单（实例，词）失败降级只告警，全部失败才抛错。
"""
import json
import logging
import time
from datetime import datetime, timedelta, UTC

import news_common

logger = logging.getLogger(__name__)

INSTANCES = ['misskey.io']
KEYWORDS = ['忘却前夜', 'morimens']
PAGE_LIMIT = 100
MAX_PAGES = 5
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 20
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; biav-sc-collector)', 'Accept': 'application/json'}
FALLBACK_HOURS = 24

# 游戏线索词（小写比对）
GAME_CUES = (
    'ゲーム', 'ガチャ', '育成', 'キャラ', 'アプリ', 'スマホ', '周回', '凸', 'ストーリー', '覚醒', '命輪',
    'steam', 'game', 'gacha', 'b.i.a.v', 'biav', 'android', 'ios', 'fanart', 'fan art', 'exalt', 'awaker',
    '游戏', '遊戲', '手游', '抽卡',
)


def _parse_time(s):
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _body(note):
    cw, text = (note.get('cw') or '').strip(), (note.get('text') or '').strip()
    return (cw + '\n' + text).strip() if cw and text else (cw or text)


def classify(note, keyword):
    """'drop'（词不在正文 / cw 里）/ 'keep'（有线索词，或非拉丁关键词本身即游戏名）/ 'keyword_only'。"""
    body = _body(note).lower()
    kw = keyword.lower()
    if kw not in body:
        return 'drop'
    if any(c in body for c in GAME_CUES) or not kw.isascii():
        return 'keep'
    return 'keyword_only'


def note_url(host, note):
    nid = str(note.get('id') or '')
    return f'https://{host}/notes/{nid}' if nid else ''


def _engagement(note):
    reactions = note.get('reactions')
    total = sum(v for v in reactions.values() if isinstance(v, int)) if isinstance(reactions, dict) else 0
    return int(note.get('renoteCount') or 0) + int(note.get('repliesCount') or 0) + total


def parse_note(note, host, keyword='', cutoff=None):
    """一条 note -> 标准 item；缺字段 / 时窗外 / 噪声返回 None。"""
    if not isinstance(note, dict):
        return None
    url = note_url(host, note)
    created = _parse_time(note.get('createdAt'))
    if not url or created is None:
        return None
    if cutoff is not None and created < cutoff:
        return None
    verdict = classify(note, keyword) if keyword else 'keep'
    if verdict == 'drop':
        return None
    user = note.get('user') or {}
    body = _body(note)
    eng = _engagement(note)
    files = note.get('files') or []
    media = str((files[0] or {}).get('thumbnailUrl') or (files[0] or {}).get('url') or '') if files else ''
    item = news_common.make_item(
        title='[Misskey] ' + body[:60],
        summary=body,
        source='misskey',
        platform_region='jp',
        time_str=created.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        url=url,
        engagement=eng,
        is_hot=eng >= 20,
        author=str(user.get('name') or user.get('username') or ''),
        lang='',
        media_url=media,
    )
    md = {'username': str(user.get('username') or ''), 'instance': host, 'keyword': keyword,
          'renote_count': int(note.get('renoteCount') or 0), 'replies_count': int(note.get('repliesCount') or 0)}
    uid = note.get('userId') or user.get('id')
    if uid:
        md['author_id'] = f'{uid}@{host}'
    if note.get('cw'):
        md['has_cw'] = True
    if verdict == 'keyword_only':
        md['keyword_only'] = True
    item['metadata'] = md
    return item


def _fetch_keyword(host, keyword, cutoff, poster, sleep):
    """单（实例, 词）翻页：空页 / 不足一页 / 整页时窗外 / untilId 不前进即停；返回 (items, 噪声丢弃数)。"""
    items, dropped, until_id = [], 0, None
    since_ms = int(cutoff.timestamp() * 1000)
    for page in range(MAX_PAGES):
        if page:
            sleep(REQUEST_DELAY)
        body = {'query': keyword, 'limit': PAGE_LIMIT, 'sinceDate': since_ms}
        if until_id:
            body['untilId'] = until_id
        data = poster(f'https://{host}/api/notes/search', body).json()
        if not isinstance(data, list):
            raise ValueError(f'Misskey {host} {keyword}: unexpected response')
        if not data:
            break
        in_window = 0
        for n in data:
            created = _parse_time((n or {}).get('createdAt'))
            if created is not None and created >= cutoff:
                in_window += 1
            item = parse_note(n, host, keyword, cutoff)
            if item is not None:
                items.append(item)
            elif created is not None and created >= cutoff and note_url(host, n or {}):
                dropped += 1
        last = str((data[-1] or {}).get('id') or '')
        if not in_window or len(data) < PAGE_LIMIT or not last or last == until_id:
            break
        until_id = last
    return items, dropped


def _default_poster(url, body):
    import requests
    last = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            if attempt < 2:
                time.sleep(attempt + 1)
    raise last


def fetch_misskey(cutoff=None, poster=None, sleep=time.sleep):
    """采集各实例 × 关键词时窗内的笔记，按 url 去重合并。单组失败只告警，全部失败才抛错。"""
    if cutoff is None:
        hours = news_common.env_int('HOURS_LOOKBACK', FALLBACK_HOURS)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
    poster = poster or _default_poster
    merged, failures, total, dropped_all = {}, 0, 0, 0
    for host in INSTANCES:
        for kw in KEYWORDS:
            if total:
                sleep(REQUEST_DELAY)
            total += 1
            try:
                got, dropped = _fetch_keyword(host, kw, cutoff, poster, sleep)
            except Exception as e:  # noqa: BLE001  单组失败降级
                failures += 1
                logger.warning('Misskey %s 关键词 %s 失败：%s', host, kw, e)
                continue
            dropped_all += dropped
            for item in got:
                prev = merged.get(item['url'])
                if prev is None:
                    merged[item['url']] = item
                elif prev['metadata'].get('keyword_only') and not item['metadata'].get('keyword_only'):
                    merged[item['url']] = item
            logger.info('Misskey %s %s: %d 条', host, kw, len(got))
    if failures == total:
        raise RuntimeError('Misskey: all queries failed')
    logger.info('Misskey 合计 %d 条（噪声丢弃 %d）', len(merged), dropped_all)
    return list(merged.values())


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    out = fetch_misskey()
    print(json.dumps({'count': len(out),
                      'keyword_only': sum(1 for i in out if i['metadata'].get('keyword_only'))}))
