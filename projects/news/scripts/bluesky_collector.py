"""Bluesky 公开帖搜索采集（守密人 2026-10-03 裁定：X 不付费，以 Bluesky 替代）。

接口：https://api.bsky.app/xrpc/app.bsky.feed.searchPosts，免登录；机房 IP 实测 200。
注意只能用 api.bsky.app——public.api.bsky.app 对机房 IP 返回 403，不要改用。
关键词：morimens / 忘却前夜 / 忘卻前夜 / モリメンス / 망각전야，逐词检索后按帖去重合并。

字段落点：source=bluesky / platform_region=global；url=https://bsky.app/profile/<did>/post/<rkey>（did 永久不变，作者改 handle 不改去重键）
（collect_global.dedup_key 与 archive_platforms.item_key 都是 URL 优先，故键 = 该 URL）；
author=displayName（空则 handle），稳定用户 ID（did）放 metadata.author_id；summary=全文；
engagement=点赞+转发+回复；time=createdAt（UTC）；lang 取帖子 langs 首项。
未登记区服 / 子类，归档平铺 Record/Community/bluesky/<日期>.json（resolve_write_layout 的默认）。

同名噪声：morimens 是个冷僻词，但直播预告、年度清单等帖里只是顺带提及。过滤只丢「命中仅来自作者
资料而非帖子本身」的条目；帖子本身含关键词但无游戏线索词的保留，标 metadata.keyword_only=True 供下游裁量。

稳健：每词翻页上限 MAX_PAGES、请求间隔 REQUEST_DELAY、只取 cutoff 之内、单词失败降级只告警，
全部关键词都失败才抛错交编排层记失败。
"""
import json
import logging
import time
from datetime import datetime, timedelta, UTC

import news_common

logger = logging.getLogger(__name__)

API_URL = 'https://api.bsky.app/xrpc/app.bsky.feed.searchPosts'
KEYWORDS = ['morimens', '忘却前夜', '忘卻前夜', 'モリメンス', '망각전야']
PAGE_LIMIT = 100
MAX_PAGES = 5
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 20
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; biav-sc-collector)', 'Accept': 'application/json'}
FALLBACK_HOURS = 24

# 游戏线索词（小写比对）：命中任一即认定与游戏相关；其余保留但标 keyword_only
GAME_CUES = (
    'game', 'gacha', 'b.i.a.v', 'biav', 'steam', 'app store', 'google play', 'android', 'ios',
    'mobile', 'f2p', 'banner', 'pull', 'chapter', 'exalt', 'awaker', 'wheel of destiny',
    'fanart', 'fan art', 'twitch', 'live',
    '游戏', '遊戲', '手游', '抽卡', 'ゲーム', 'ガチャ', '게임', '가챠', '育成', 'キャラ',
)


def _text_of(post):
    return (post.get('record') or {}).get('text') or ''


def _link_blob(post):
    """帖子里的链接 / 标签 / 外部卡片文本，拼成小写串供线索匹配。"""
    rec = post.get('record') or {}
    parts = [t for t in (rec.get('tags') or []) if isinstance(t, str)]
    for f in rec.get('facets') or []:
        for feat in f.get('features') or []:
            parts.append(str(feat.get('tag') or feat.get('uri') or ''))
    ext = (post.get('embed') or {}).get('external') or {}
    parts += [str(ext.get('uri') or ''), str(ext.get('title') or ''), str(ext.get('description') or '')]
    return ' '.join(parts).lower()


def classify(post, keyword):
    """返回 'drop' / 'keep' / 'keyword_only'。

    drop：关键词没出现在帖子本身（正文 / 标签 / 链接 / 卡片）里，命中只来自作者资料等外围；
    keep：含游戏线索词；keyword_only：含关键词但无线索词（拿不准，保留并标注）。"""
    text = _text_of(post).lower()
    blob = _link_blob(post)
    kw = keyword.lower()
    if kw not in text and kw not in blob:
        return 'drop'
    if any(c in text or c in blob for c in GAME_CUES):
        return 'keep'
    # 非拉丁关键词本身就是游戏名，没有同名歧义
    if not kw.isascii():
        return 'keep'
    return 'keyword_only'


def _parse_time(s):
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def post_url(post):
    """https://bsky.app/profile/<did>/post/<rkey>：用 did 而非 handle，作者改名后去重键不变；缺 did 回落 handle。"""
    uri = str(post.get('uri') or '')
    rkey = uri.rsplit('/', 1)[-1] if '/app.bsky.feed.post/' in uri else ''
    author = post.get('author') or {}
    who = author.get('did') or author.get('handle') or ''
    if not rkey or not who:
        return ''
    return f'https://bsky.app/profile/{who}/post/{rkey}'


def _first_image(post):
    emb = post.get('embed') or {}
    imgs = emb.get('images') or (emb.get('media') or {}).get('images') or []
    return str((imgs[0] or {}).get('thumb') or '') if imgs else ''


def parse_post(post, keyword='', cutoff=None):
    """搜索结果里的一个 post -> 标准 item；缺关键字段 / 时窗外 / 噪声返回 None。"""
    if not isinstance(post, dict):
        return None
    url = post_url(post)
    rec = post.get('record') or {}
    created = _parse_time(rec.get('createdAt') or post.get('indexedAt'))
    if not url or created is None:
        return None
    if cutoff is not None and created < cutoff:
        return None
    verdict = classify(post, keyword) if keyword else 'keep'
    if verdict == 'drop':
        return None
    text = _text_of(post).strip()
    author = post.get('author') or {}
    nickname = str(author.get('displayName') or author.get('handle') or '')
    likes = int(post.get('likeCount') or 0)
    reposts = int(post.get('repostCount') or 0)
    replies = int(post.get('replyCount') or 0)
    langs = [l for l in (rec.get('langs') or []) if isinstance(l, str)]
    item = news_common.make_item(
        title='[Bluesky] ' + text[:60],
        summary=text,
        source='bluesky',
        platform_region='global',
        time_str=created.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        url=url,
        engagement=likes + reposts + replies,
        is_hot=likes + reposts >= 50,
        author=nickname,
        lang=langs[0] if langs else '',
        media_url=_first_image(post),
    )
    md = {'likes_count': likes, 'reposts_count': reposts, 'replies_count': replies,
          'author_handle': str(author.get('handle') or ''), 'keyword': keyword}
    if author.get('did'):
        md['author_id'] = str(author['did'])
    if len(langs) > 1:
        md['langs'] = langs
    if verdict == 'keyword_only':
        md['keyword_only'] = True
    item['metadata'] = md
    return item


def _fetch_keyword(keyword, cutoff, getter, sleep):
    """单关键词翻页：无 cursor / 空页 / 整页都在时窗外即停；返回 (items, 噪声丢弃数)。"""
    items, dropped, cursor = [], 0, None
    since = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')
    for page in range(MAX_PAGES):
        if page:
            sleep(REQUEST_DELAY)
        params = {'q': keyword, 'since': since, 'limit': PAGE_LIMIT, 'sort': 'latest'}
        if cursor:
            params['cursor'] = cursor
        data = getter(params).json()
        if not isinstance(data, dict):
            raise ValueError(f'Bluesky {keyword}: unexpected response')
        posts = data.get('posts') or []
        if not posts:
            break
        in_window = 0
        for p in posts:
            created = _parse_time(((p or {}).get('record') or {}).get('createdAt'))
            if created is not None and created >= cutoff:
                in_window += 1
            item = parse_post(p, keyword, cutoff)
            if item is not None:
                items.append(item)
            elif created is not None and created >= cutoff and post_url(p or {}):
                dropped += 1
        cursor = data.get('cursor')
        if not cursor or not in_window:
            break
    return items, dropped


def fetch_bluesky(cutoff=None, getter=None, sleep=time.sleep):
    """采集五个关键词时窗内的帖子，按 url 去重合并。单词失败只告警，全部失败才抛错。"""
    if cutoff is None:
        hours = news_common.env_int('HOURS_LOOKBACK', FALLBACK_HOURS)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
    if getter is None:
        def getter(params):
            return news_common.get_with_retry(API_URL, params=params, headers=HEADERS,
                                              timeout=REQUEST_TIMEOUT)
    merged, failures, total_dropped = {}, 0, 0
    for idx, kw in enumerate(KEYWORDS):
        if idx:
            sleep(REQUEST_DELAY)
        try:
            got, dropped = _fetch_keyword(kw, cutoff, getter, sleep)
        except Exception as e:  # noqa: BLE001  单词失败降级
            failures += 1
            logger.warning('Bluesky 关键词 %s 失败：%s', kw, e)
            continue
        total_dropped += dropped
        for item in got:
            prev = merged.get(item['url'])
            if prev is None:
                merged[item['url']] = item
            elif prev['metadata'].get('keyword_only') and not item['metadata'].get('keyword_only'):
                merged[item['url']] = item  # 另一关键词给出了更确定的判断
        logger.info('Bluesky %s: %d 条', kw, len(got))
    if failures == len(KEYWORDS):
        raise RuntimeError('Bluesky: all keywords failed')
    logger.info('Bluesky 合计 %d 条（噪声丢弃 %d）', len(merged), total_dropped)
    return list(merged.values())


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    out = fetch_bluesky()
    print(json.dumps({'count': len(out),
                      'keyword_only': sum(1 for i in out if i['metadata'].get('keyword_only'))}))
