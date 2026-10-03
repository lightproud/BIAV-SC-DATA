"""Facebook 官方主页评论采集（守密人 2026-10-03 裁定：主页由 Studio 管理，可用主页访问令牌）。

走 Graph API（版本号见 GRAPH_VERSION）。环境变量：FB_PAGE_ID、FB_PAGE_TOKEN（主页访问令牌）；
**任一缺失即返回空列表并记 info「未配置，跳过」，不算失败**（sources.AUTH_GATED 登记
facebook -> FB_PAGE_TOKEN，collect_global 据此标注「待配」而非故障）。

流程：① /{page-id}/posts 取近期官方帖（回看 POST_LOOKBACK_DAYS，使老帖下新增评论也能捞到）；
② 对每帖 /{post-id}/comments?filter=stream 取全部评论（含楼中楼，parent.id 记入 metadata）。
条目以评论为主；官方帖本身是官方发言，只在自身发布于时窗内时作为上下文条目产出，
标 metadata.is_official_post=True。

归档落点（archive_layout）：不登记区服 / 子类，评论与官方帖同落平铺的
Record/Community/facebook/<日期>.json，用 metadata.is_official_post 区分；评论的 metadata.post_id /
post_url 指回所属官方帖。理由：单主页量小，无需拆目录；不改 archive_layout 的读写映射，零破坏。

字段：评论 url=Graph 返回的 permalink_url（缺则 https://www.facebook.com/<comment id>），
author=from.name，稳定用户 ID（页面范围 ID）放 metadata.author_id；summary=评论全文；
engagement=like_count+comment_count；time=created_time（UTC）。from 可能因隐私缺失，
此时标 metadata.author_is_unknown。

令牌安全：令牌只经 Authorization 头发送，不进 URL / 参数 / 日志；翻页自己用 after 游标拼，
不跟随 paging.next（其中会带 access_token）；所有异常文本经 _redact 抹去令牌后才记录。
稳健：翻页上限、请求间隔、单帖评论失败降级只告警；取帖失败（令牌失效等）抛错交编排层记失败。
"""
import logging
import os
import time
from datetime import datetime, timedelta, UTC

import requests

import news_common

logger = logging.getLogger(__name__)

GRAPH_VERSION = 'v21.0'
GRAPH_BASE = f'https://graph.facebook.com/{GRAPH_VERSION}'
POST_FIELDS = 'id,message,created_time,permalink_url'
COMMENT_FIELDS = ('id,message,created_time,from{id,name},like_count,comment_count,'
                  'permalink_url,parent{id}')
POSTS_PAGE_LIMIT = 25
COMMENTS_PAGE_LIMIT = 100
MAX_POST_PAGES = 2
MAX_COMMENT_PAGES = 5
MAX_POSTS_PER_RUN = 25         # 每轮最多展开评论的官方帖数
POST_LOOKBACK_DAYS = 7         # 官方帖回看天数（评论仍只取 cutoff 之内）
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 20
FALLBACK_HOURS = 24


def _redact(text, token):
    s = str(text)
    return s.replace(token, '***') if token else s


def _parse_time(s):
    """Graph 时间形如 2026-10-03T01:02:03+0000；兼容 Z / 带冒号偏移。失败返回 None。"""
    if not s:
        return None
    s = str(s)
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S.%f%z'):
        try:
            return datetime.strptime(s.replace('Z', '+0000'), fmt).astimezone(UTC)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except ValueError:
        return None


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _fallback_url(obj_id):
    return f'https://www.facebook.com/{obj_id}'


def parse_post(post, page_id=''):
    """官方帖 -> item（上下文条目）；缺 id / 时间返回 None。"""
    if not isinstance(post, dict):
        return None
    pid = str(post.get('id') or '')
    created = _parse_time(post.get('created_time'))
    if not pid or created is None:
        return None
    text = str(post.get('message') or '').strip()
    item = news_common.make_item(
        title='[Facebook 官方帖] ' + text[:60],
        summary=text,
        source='facebook',
        platform_region='global',
        time_str=_iso(created),
        url=str(post.get('permalink_url') or _fallback_url(pid)),
        author='官方主页',
        lang='',
    )
    item['metadata'] = {'is_official_post': True, 'post_id': pid, 'page_id': str(page_id)}
    return item


def parse_comment(comment, post_id, post_url='', page_id=''):
    """评论（含楼中楼）-> item；缺 id / 时间 / 正文返回 None（纯贴图评论无文本也跳过）。"""
    if not isinstance(comment, dict):
        return None
    cid = str(comment.get('id') or '')
    created = _parse_time(comment.get('created_time'))
    text = str(comment.get('message') or '').strip()
    if not cid or created is None or not text:
        return None
    frm = comment.get('from') or {}
    name = str(frm.get('name') or '')
    likes = int(comment.get('like_count') or 0)
    replies = int(comment.get('comment_count') or 0)
    item = news_common.make_item(
        title='[Facebook 评论] ' + text[:60],
        summary=text,
        source='facebook',
        platform_region='global',
        time_str=_iso(created),
        url=str(comment.get('permalink_url') or _fallback_url(cid)),
        engagement=likes + replies,
        is_hot=likes >= 50,
        author=name,
        lang='',
    )
    md = {'is_official_post': False, 'post_id': str(post_id), 'page_id': str(page_id),
          'likes_count': likes, 'replies_count': replies}
    if post_url:
        md['post_url'] = post_url
    if frm.get('id'):
        md['author_id'] = str(frm['id'])
    if not name:
        md['author_is_unknown'] = True
    parent = (comment.get('parent') or {}).get('id')
    if parent:
        md['parent_id'] = str(parent)
    item['metadata'] = md
    return item


def _make_getter(token):
    def getter(path, params):
        try:
            resp = requests.get(f'{GRAPH_BASE}/{path}', params=params,
                                headers={'Authorization': f'Bearer {token}'},
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                try:
                    err = (resp.json().get('error') or {})
                    msg = f"{err.get('type', '')} code={err.get('code', '')} {err.get('message', '')}"
                except ValueError:
                    msg = resp.text[:120]
                raise RuntimeError(f'Graph API HTTP {resp.status_code}: {msg}')
            return resp.json()
        except requests.RequestException as e:
            raise RuntimeError(f'Graph API 请求失败：{type(e).__name__}') from None
    return getter


def _paged(getter, path, params, max_pages, sleep, token):
    """按 paging.cursors.after 翻页（不跟随含令牌的 next 链接），逐条产出 data 元素。"""
    after = None
    for page in range(max_pages):
        if page:
            sleep(REQUEST_DELAY)
        q = dict(params)
        if after:
            q['after'] = after
        data = getter(path, q)
        if not isinstance(data, dict):
            raise ValueError('Graph API: unexpected response')
        yield from (data.get('data') or [])
        paging = data.get('paging') or {}
        after = (paging.get('cursors') or {}).get('after')
        if not after or not paging.get('next'):
            return


def fetch_facebook_page(cutoff=None, getter=None, sleep=time.sleep, page_id=None, token=None):
    """采集官方主页时窗内评论（+ 时窗内官方帖作上下文）。未配置返回 []（不算失败）。"""
    page_id = (os.environ.get('FB_PAGE_ID') if page_id is None else page_id) or ''
    token = (os.environ.get('FB_PAGE_TOKEN') if token is None else token) or ''
    page_id, token = page_id.strip(), token.strip()
    if not page_id or not token:
        logger.info('Facebook 主页：FB_PAGE_ID / FB_PAGE_TOKEN 未配置，跳过')
        return []
    if cutoff is None:
        cutoff = datetime.now(UTC) - timedelta(hours=news_common.env_int('HOURS_LOOKBACK', FALLBACK_HOURS))
    getter = getter or _make_getter(token)
    post_since = cutoff - timedelta(days=POST_LOOKBACK_DAYS)

    try:
        posts = list(_paged(getter, f'{page_id}/posts',
                            {'fields': POST_FIELDS, 'since': int(post_since.timestamp()),
                             'limit': POSTS_PAGE_LIMIT},
                            MAX_POST_PAGES, sleep, token))
    except Exception as e:  # noqa: BLE001  令牌失效 / 权限不足等：抛错交编排层记失败
        raise RuntimeError(f'Facebook 取主页帖子失败：{_redact(e, token)}') from None

    items, seen = [], set()
    expanded = 0
    for post in posts[:MAX_POSTS_PER_RUN]:
        pid = str(post.get('id') or '')
        if not pid:
            continue
        post_item = parse_post(post, page_id)
        post_url = post_item['url'] if post_item else str(post.get('permalink_url') or '')
        if post_item and datetime.fromisoformat(post_item['time'].replace('Z', '+00:00')) >= cutoff:
            items.append(post_item)
            seen.add(post_item['url'])
        if expanded:
            sleep(REQUEST_DELAY)
        expanded += 1
        try:
            for c in _paged(getter, f'{pid}/comments',
                            {'fields': COMMENT_FIELDS, 'filter': 'stream',
                             'order': 'reverse_chronological', 'limit': COMMENTS_PAGE_LIMIT},
                            MAX_COMMENT_PAGES, sleep, token):
                item = parse_comment(c, pid, post_url, page_id)
                if item is None or item['url'] in seen:
                    continue
                if datetime.fromisoformat(item['time'].replace('Z', '+00:00')) < cutoff:
                    continue
                seen.add(item['url'])
                items.append(item)
        except Exception as e:  # noqa: BLE001  单帖评论失败降级
            logger.warning('Facebook 帖 %s 评论失败：%s', pid, _redact(e, token))
    logger.info('Facebook 主页：%d 帖展开，产出 %d 条', expanded, len(items))
    return items
