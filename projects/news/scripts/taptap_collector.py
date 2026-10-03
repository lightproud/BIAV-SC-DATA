"""TapTap 采集：国服评价 + 国服论坛帖子（含回复）+ 国际版评价（T111，守密人 2026-10-03 裁定重启 / 扩充）。

一、国服评价：公开评价列表 /webapiv2/review/v2/list-by-app（sort=new，from/limit 翻页），
无需登录；机房 IP 实测可用。正式服 364992 + 测试服 374995 合并落 taptap/cn/review/
（守密人 2026-06-21 裁定，条目 metadata.app_id 区分）。
字段落点（与历史档同构）：source=taptap_review / platform_region=cn /
region=cn / archive_subtype=review；url=https://www.taptap.cn/review/<review id>
（去重键稳定：collect_global.dedup_key 与 archive_platforms.item_key 都是 URL 优先）；
author=昵称，稳定用户 ID 放 metadata.author_id；summary=评价全文（去 HTML）。

二、国服论坛帖子 + 回复（已实现，原「需 group_id、无登录态拿不到」作废）：
  * 帖子列表 GET /webapiv2/feed/v7/by-group?group_id=<gid>&type=feed&limit=10&from=N（免登录，
    created_time 倒序）。论坛 group_id：正式服 364992 → 594490，测试服 374995 → 612611；
    discover_group_id() 可从 /app/<id>/topic 页 HTML 的 /group/<gid>/info 链接兜底发现。
  * 正文取详情接口 /webapiv2/moment/v3/detail?id=<moment id> 的 first_post.contents.json（Slate 树，
    抽纯文本、截 2000 字）；详情失败降级为列表里的 topic.summary。
  * 回复取 /webapiv2/post/v3/by-topic?topic_id=<topic id>&limit=10&from=N：顶层回复 + child_posts
    （楼中楼）都拍平成独立条目，每帖上限 MAX_REPLIES_PER_POST，metadata.parent 指向帖子 URL。
    只保留 created_time 在 cutoff 之后的回复（旧帖近期有新回复也能采到）。
  * 落点：source=taptap（archive_layout 默认落点 taptap/cn/post/，与 2026 年 6~8 月的历史帖档同构），
    region=cn / archive_subtype=post；url=https://www.taptap.cn/moment/<id>（稳定 id，历史档同样写法），
    回复 url=<帖子 url>#post-<回复 id>；author=昵称，metadata.author_id=稳定 ID。

三、国际版评价（taptap.io，app 33875920）：同 list-by-app 接口形态，落 taptap/global/review/
（source=taptap_review + region=global，沿用既有 review 映射，无需新增映射），metadata.lang。
2026-10-03 实测：该 app（包名 jp.co.altplus.boukyakuzenya）review_count=0，且 taptap.io 的
list-by-app 对任意 app 机房直连都回 500，故当前 0 产出属预期；接口恢复 / 有评价后自动生效。

稳健：请求间隔 REQUEST_DELAY、翻页上限 MAX_PAGES、单应用 / 单论坛失败降级（只记警告，
不影响其余目标；全部目标都失败才抛错交编排层记失败）、只取 cutoff 之后的新条目
（时间倒序，整页都在时窗外即停止翻页）。
"""
import html as html_lib
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, UTC
from urllib.parse import urlencode

import news_common

logger = logging.getLogger(__name__)

API_URL = 'https://www.taptap.cn/webapiv2/review/v2/list-by-app'
# 应用编号 → 版本标签（守密人 2026-06-21 裁定两者合并进 taptap/cn/）
TAPTAP_APPS = {'364992': '正式服', '374995': '测试服'}
PAGE_LIMIT = 10
MAX_PAGES = int(os.environ.get('TAPTAP_MAX_PAGES') or 5)
REQUEST_DELAY = 1.0
_SCORE_LABEL = {5: '好评', 4: '好评', 3: '中评', 2: '差评', 1: '差评'}

# 国际版 taptap.io：app 33875920（包名 jp.co.altplus.boukyakuzenya，2026-10-03 实测 review_count=0）
TAPTAP_IO_APPS = {'33875920': 'taptap.io'}
SITE_CN = {'base': 'https://www.taptap.cn', 'region': 'cn', 'lang': 'zh',
           'apps': TAPTAP_APPS, 'ua': ('zh_CN', 'CN')}
SITE_IO = {'base': 'https://www.taptap.io', 'region': 'global', 'lang': 'en',
           'apps': TAPTAP_IO_APPS, 'ua': ('en_US', 'US')}

# 论坛：group_id → (应用编号, 版本标签)
FORUM_BASE = 'https://www.taptap.cn'
FORUM_GROUPS = {'594490': ('364992', '正式服'), '612611': ('374995', '测试服')}
MAX_REPLIES_PER_POST = int(os.environ.get('TAPTAP_MAX_REPLIES') or 30)
MAX_REPLY_PAGES = 3
BODY_LIMIT = 2000


def _x_ua(lang='zh_CN', loc='CN'):
    """X-UA 参数（已 URL 编码）：UID 每轮随机，其余沿用浏览器 WebApp 形态。"""
    uid = ''.join(random.choices('0123456789abcdef', k=32))
    raw = (f'V=1&PN=WebApp&LANG={lang}&VN_CODE=102&LOC={loc}&PLT=PC&DS=Android'
           f'&UID={uid}&DT=PC')
    return raw.replace('=', '%3D').replace('&', '%26')


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _ts_to_dt(ts):
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, UTC)
    return None


def _review_time(moment):
    """publish_time / created_time（Unix 秒）→ UTC datetime；缺失返回 None。"""
    for key in ('publish_time', 'created_time'):
        dt = _ts_to_dt(moment.get(key))
        if dt:
            return dt
    return None


def _clean_text(raw):
    """去 HTML（<br> 换行、实体反转义）。"""
    raw = str(raw or '')
    for br in ('<br />', '<br/>', '<br>'):
        raw = raw.replace(br, '\n')
    return html_lib.unescape(news_common.strip_html(raw)).strip()


# ─────────────────────────── 评价（国服 / 国际版）───────────────────────────

def parse_review(entry, app_id, site=None):
    """把 data.list[] 的一个元素解析成标准 item；非评价 / 缺关键字段返回 None。

    site 缺省为国服（taptap.cn）；国际版传 SITE_IO（落 taptap/global/review/，metadata 加 lang）。
    """
    site = site or SITE_CN
    if not isinstance(entry, dict):
        return None
    moment = entry.get('moment')
    if not isinstance(moment, dict):
        return None
    review = moment.get('review')
    if not isinstance(review, dict):
        return None
    review_id = str(review.get('id') or '').strip()
    published = _review_time(moment)
    if not review_id or published is None:
        return None
    contents = review.get('contents') or {}
    text = _clean_text(contents.get('text') or contents.get('raw_text'))
    score = _to_int(review.get('score'))
    user = (moment.get('author') or {}).get('user') or {}
    nickname = str(user.get('name') or '')
    author_id = str(user.get('id') or '')
    stat = moment.get('stat') or {}
    likes = _to_int(stat.get('supports'))
    comments = _to_int(stat.get('comments'))
    label = _SCORE_LABEL.get(score)
    prefix = f'[TapTap {label}] ' if label else '[TapTap] '
    item = news_common.make_item(
        title=prefix + text[:60],
        summary=text,
        source='taptap_review',
        platform_region=site['region'],
        time_str=published.isoformat(),
        url=f"{site['base']}/review/{review_id}",
        engagement=likes + comments,
        is_hot=likes >= 50,
        author=nickname,
        tags=[label] if label else [],
        lang=site['lang'],
        region=site['region'],
        archive_subtype='review',
    )
    metadata = {
        'app_id': str(app_id),
        'app_label': site['apps'].get(str(app_id), ''),
        'score': score,
        'likes_count': likes,
        'comments_count': comments,
        'author_is_unknown': not nickname,
    }
    if author_id:
        metadata['author_id'] = author_id
    if moment.get('device'):
        metadata['device'] = str(moment['device'])
    if site is SITE_IO:
        lang = str(review.get('language') or review.get('lang') or site['lang'])
        metadata['lang'] = lang
        item['lang'] = lang[:2].lower() or site['lang']
    item['metadata'] = metadata
    return item


def _fetch_app(app_id, cutoff, getter, sleep, site=None):
    """翻页取单应用时窗内评价。sort=new 时间倒序，整页都在时窗外即停止。"""
    site = site or SITE_CN
    items, seen = [], set()
    offset = 0
    for page in range(MAX_PAGES):
        if page:
            sleep(REQUEST_DELAY)
        params = {'app_id': app_id, 'limit': PAGE_LIMIT, 'sort': 'new', 'from': offset}
        url = (f"{site['base']}/webapiv2/review/v2/list-by-app?{urlencode(params)}"
               f"&X-UA={_x_ua(*site['ua'])}")
        data = getter(url).json()
        if not isinstance(data, dict) or not data.get('success', True):
            raise ValueError(f'TapTap app {app_id}: unexpected response')
        body = data.get('data') or {}
        entries = body.get('list') or []
        if not entries:
            break
        parsed = [parse_review(e, app_id, site) for e in entries]
        parsed = [p for p in parsed if p is not None]
        fresh = [p for p in parsed if datetime.fromisoformat(p['time']) >= cutoff]
        for item in fresh:
            if item['url'] not in seen:
                seen.add(item['url'])
                items.append(item)
        if not fresh:
            break
        offset += len(entries)
        if not body.get('next_page'):
            break
    return items


def _default_cutoff():
    hours = news_common.env_int('HOURS_LOOKBACK', 24)
    return datetime.now(UTC) - timedelta(hours=hours)


def _default_getter(referer):
    def getter(url):
        return news_common.get_with_retry(
            url, timeout=15,
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': referer})
    return getter


def _run_targets(targets, label, fetch_one, sleep):
    """逐目标采集；单目标失败只告警，全部失败才抛错。"""
    items, failures = [], 0
    for idx, target in enumerate(targets):
        if idx:
            sleep(REQUEST_DELAY)
        try:
            got = fetch_one(target)
            logger.info('%s %s: %d items in window', label, target, len(got))
            items.extend(got)
        except Exception as e:  # noqa: BLE001  单目标失败降级
            failures += 1
            logger.warning('%s %s failed: %s', label, target, e)
    if targets and failures == len(targets):
        raise RuntimeError(f'{label}: all targets failed')
    return items


def fetch_taptap_reviews(cutoff=None, getter=None, sleep=time.sleep):
    """采集正式服 + 测试服时窗内的新评价。单应用失败只告警，不让整轮失败。"""
    cutoff = cutoff or _default_cutoff()
    getter = getter or _default_getter('https://www.taptap.cn/app/364992/review')
    return _run_targets(list(TAPTAP_APPS), 'TapTap app',
                        lambda app_id: _fetch_app(app_id, cutoff, getter, sleep), sleep)


def fetch_taptap_io_reviews(cutoff=None, getter=None, sleep=time.sleep):
    """国际版（taptap.io）评价；落 taptap/global/review/。接口 500 / 无评价时安静返回空（不抛错）。

    国际版接口目前对机房直连回 500（见模块说明），失败属已知降级：全部失败也只告警返回空，
    不让整轮进失败清单；接口恢复后自动出数。
    """
    cutoff = cutoff or _default_cutoff()
    getter = getter or _default_getter('https://www.taptap.io/app/33875920/review')
    try:
        return _run_targets(list(TAPTAP_IO_APPS), 'TapTap IO app',
                            lambda app_id: _fetch_app(app_id, cutoff, getter, sleep, SITE_IO), sleep)
    except RuntimeError as e:
        logger.warning('%s（国际版接口不可用，按已知降级返回空）', e)
        return []


# ─────────────────────────── 论坛帖子 + 回复（国服）───────────────────────────

def discover_group_id(app_id, getter):
    """从 /app/<id>/topic 页 HTML 找论坛 group_id（取出现次数最多的 /group/<gid>/info）；找不到返回 None。"""
    text = getter(f'{FORUM_BASE}/app/{app_id}/topic').text
    ids = re.findall(r'/group/(\d+)/info', text or '')
    if not ids:
        return None
    return max(set(ids), key=ids.count)


def _slate_text(nodes):
    """Slate 树（contents.json）→ 纯文本；段落换行，其余节点递归取 text。"""
    out = []

    def walk(n):
        if isinstance(n, list):
            for c in n:
                walk(c)
        elif isinstance(n, dict):
            if 'text' in n and isinstance(n['text'], str):
                out.append(n['text'])
            walk(n.get('children'))
            if n.get('type') in ('paragraph', 'heading', 'quote', 'list-item') and out and out[-1] != '\n':
                out.append('\n')
    walk(nodes)
    return ''.join(out).strip()


def _post_body(detail, fallback):
    """详情 JSON → 帖子正文纯文本（截 BODY_LIMIT）；解析不出回退列表摘要。"""
    try:
        fp = (detail.get('data') or {}).get('first_post') or {}
        contents = fp.get('contents') or {}
        text = _slate_text(contents.get('json')) or _clean_text(contents.get('text'))
    except AttributeError:
        text = ''
    return (text or fallback or '')[:BODY_LIMIT]


def parse_post(entry, group_id, body=None):
    """feed/v7/by-group 的 data.list[] 元素 → 帖子 item；非 moment / 缺关键字段返回 None。"""
    if not isinstance(entry, dict):
        return None
    moment = entry.get('moment')
    if not isinstance(moment, dict):
        return None
    mid = str(moment.get('id_str') or moment.get('id') or '').strip()
    published = _review_time(moment)
    topic = moment.get('topic') or {}
    if not mid or published is None or not topic:
        return None
    title = _clean_text(topic.get('title'))
    summary = (body if body is not None else _clean_text(topic.get('summary')))[:BODY_LIMIT]
    user = (moment.get('author') or {}).get('user') or {}
    nickname = str(user.get('name') or '')
    author_id = str(user.get('id') or '')
    stat = moment.get('stat') or {}
    ups = _to_int(stat.get('ups'))
    comments = _to_int(stat.get('comments'))
    labels = [str(lb.get('name')) for lb in (moment.get('labels') or [])
              if isinstance(lb, dict) and lb.get('name')]
    app_id, app_label = FORUM_GROUPS.get(str(group_id), ('', ''))
    item = news_common.make_item(
        title=title or summary[:60],
        summary=summary,
        source='taptap',
        platform_region='cn',
        time_str=published.isoformat(),
        url=f'{FORUM_BASE}/moment/{mid}',
        engagement=ups + comments,
        is_hot=ups >= 50 or comments >= 30,
        author=nickname,
        tags=labels,
        lang='zh',
        region='cn',
        archive_subtype='post',
    )
    metadata = {
        'kind': 'post',
        'group_id': str(group_id),
        'app_id': app_id,
        'app_label': app_label,
        'topic_id': str(topic.get('id_str') or ''),
        'likes_count': ups,
        'comments_count': comments,
        'views_count': _to_int(stat.get('pv_total')),
        'author_is_unknown': not nickname,
    }
    if author_id:
        metadata['author_id'] = author_id
    item['metadata'] = metadata
    return item


def parse_reply(post, parent_url, parent_title, group_id, app_id, app_label, parent_reply_id=None):
    """by-topic 的回复（或 child_posts 楼中楼）→ 回复 item；缺 id / 时间 / 正文返回 None。"""
    if not isinstance(post, dict):
        return None
    rid = str(post.get('id_str') or post.get('id') or '').strip()
    created = _ts_to_dt(post.get('created_time'))
    contents = post.get('contents') or {}
    text = _clean_text(contents.get('text') or contents.get('raw_text'))
    if not rid or created is None or not text:
        return None
    author = post.get('author') or {}
    nickname = str(author.get('name') or '')
    author_id = str(author.get('id') or '')
    ups = _to_int(post.get('ups'))
    item = news_common.make_item(
        title='[TapTap 回复] ' + text[:60],
        summary=text[:BODY_LIMIT],
        source='taptap',
        platform_region='cn',
        time_str=created.isoformat(),
        url=f'{parent_url}#post-{rid}',
        engagement=ups,
        is_hot=ups >= 50,
        author=nickname,
        tags=['回复'],
        lang='zh',
        region='cn',
        archive_subtype='post',
    )
    metadata = {
        'kind': 'reply',
        'parent': parent_url,
        'parent_title': parent_title[:60],
        'group_id': str(group_id),
        'app_id': app_id,
        'app_label': app_label,
        'likes_count': ups,
        'comments_count': _to_int(post.get('comments')),
        'author_is_unknown': not nickname,
    }
    if parent_reply_id:
        metadata['parent_reply_id'] = str(parent_reply_id)
    if post.get('position') is not None:
        metadata['floor'] = _to_int(post.get('position'))
    if author_id:
        metadata['author_id'] = author_id
    item['metadata'] = metadata
    return item


def _get_json(getter, url):
    data = getter(url).json()
    if not isinstance(data, dict) or not data.get('success', True):
        raise ValueError('TapTap forum: unexpected response')
    return data.get('data') or {}


def _fetch_replies(post_item, topic_id, cutoff, getter, sleep, group_id, app_id, app_label):
    """取一个帖子 cutoff 之后的回复（顶层 + 楼中楼），每帖上限 MAX_REPLIES_PER_POST。"""
    out, seen = [], set()
    offset = 0
    for page in range(MAX_REPLY_PAGES):
        if page:
            sleep(REQUEST_DELAY)
        params = {'topic_id': topic_id, 'limit': PAGE_LIMIT, 'from': offset}
        body = _get_json(getter, f'{FORUM_BASE}/webapiv2/post/v3/by-topic?{urlencode(params)}'
                                 f'&X-UA={_x_ua()}')
        entries = body.get('list') or []
        if not entries:
            break
        for e in entries:
            flat = [(e, None)] + [(c, e.get('id')) for c in (e.get('child_posts') or [])]
            for post, parent_rid in flat:
                item = parse_reply(post, post_item['url'], post_item['title'], group_id,
                                   app_id, app_label, parent_rid)
                if item is None or item['url'] in seen:
                    continue
                if datetime.fromisoformat(item['time']) < cutoff:
                    continue
                seen.add(item['url'])
                out.append(item)
                if len(out) >= MAX_REPLIES_PER_POST:
                    return out
        offset += len(entries)
        if not body.get('next_page'):
            break
    return out


def _fetch_group(group_id, cutoff, getter, sleep, with_replies=True):
    """翻页取单论坛时窗内的帖子（+ 近期回复）。created 倒序，整页都在时窗外即停止翻页。"""
    app_id, app_label = FORUM_GROUPS.get(str(group_id), ('', ''))
    items, seen = [], set()
    offset = 0
    for page in range(MAX_PAGES):
        if page:
            sleep(REQUEST_DELAY)
        params = {'group_id': group_id, 'type': 'feed', 'limit': PAGE_LIMIT, 'from': offset}
        body = _get_json(getter, f'{FORUM_BASE}/webapiv2/feed/v7/by-group?{urlencode(params)}'
                                 f'&X-UA={_x_ua()}')
        entries = body.get('list') or []
        if not entries:
            break
        fresh_in_page = False
        for e in entries:
            post = parse_post(e, group_id)
            if post is None or post['url'] in seen:
                continue
            moment = e['moment']
            created = datetime.fromisoformat(post['time'])
            commented = _ts_to_dt(moment.get('commented_time'))
            is_fresh = created >= cutoff
            fresh_in_page = fresh_in_page or is_fresh
            has_new_replies = (post['metadata']['comments_count'] > 0
                               and commented is not None and commented >= cutoff)
            if not (is_fresh or has_new_replies):
                continue
            seen.add(post['url'])
            topic_id = post['metadata']['topic_id']
            if is_fresh:
                try:
                    sleep(REQUEST_DELAY)
                    detail = getter(f"{FORUM_BASE}/webapiv2/moment/v3/detail?id={moment.get('id_str')}"
                                    f'&X-UA={_x_ua()}').json()
                    body_text = _post_body(detail, post['summary'])
                except Exception as err:  # noqa: BLE001  详情失败降级为列表摘要
                    logger.warning('TapTap post %s detail failed: %s', moment.get('id_str'), err)
                    body_text = post['summary']
                post['summary'] = body_text
                items.append(post)
            if with_replies and topic_id and has_new_replies:
                try:
                    sleep(REQUEST_DELAY)
                    items.extend(_fetch_replies(post, topic_id, cutoff, getter, sleep,
                                                group_id, app_id, app_label))
                except Exception as err:  # noqa: BLE001  回复失败降级：只留帖子 + comments 计数
                    logger.warning('TapTap replies of %s failed: %s', topic_id, err)
        if not fresh_in_page:
            break
        offset += len(entries)
        if not body.get('next_page'):
            break
    return items


def fetch_taptap_posts(cutoff=None, getter=None, sleep=time.sleep):
    """采集正式服 + 测试服论坛时窗内的新帖子与回复。单论坛失败只告警，全部失败才抛错。"""
    cutoff = cutoff or _default_cutoff()
    getter = getter or _default_getter(f'{FORUM_BASE}/app/364992/topic')
    return _run_targets(list(FORUM_GROUPS), 'TapTap forum',
                        lambda gid: _fetch_group(gid, cutoff, getter, sleep), sleep)
