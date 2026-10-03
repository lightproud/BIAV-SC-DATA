"""TapTap 国服评价采集（T111，守密人 2026-10-03 裁定重启；旧采集器已删，本模块为重写）。

接口：公开评价列表 /webapiv2/review/v2/list-by-app（sort=new，from/limit 翻页），
无需登录；机房 IP 实测可用。正式服 364992 + 测试服 374995 合并落 taptap/cn/review/
（守密人 2026-06-21 裁定，条目 metadata.app_id 区分）。

字段落点（与历史档同构）：source=taptap_review / platform_region=cn /
region=cn / archive_subtype=review；url=https://www.taptap.cn/review/<review id>
（去重键稳定：collect_global.dedup_key 与 archive_platforms.item_key 都是 URL 优先）；
author=昵称，稳定用户 ID 放 metadata.author_id；summary=评价全文（去 HTML）。
论坛帖子（taptap/cn/post/）未做：公开接口需 group_id，无登录态拿不到。

稳健：请求间隔 REQUEST_DELAY、每应用页数上限 MAX_PAGES、单应用失败降级（只记警告，
不影响另一应用与整轮；两应用都失败才抛错交编排层记失败）、只取 cutoff 之后的新条目
（sort=new 时间倒序，整页都在时窗外即停止翻页）。
"""
import logging
import os
import random
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


def _x_ua():
    """X-UA 参数（已 URL 编码）：UID 每轮随机，其余沿用浏览器 WebApp 形态。"""
    uid = ''.join(random.choices('0123456789abcdef', k=32))
    raw = (f'V=1&PN=WebApp&LANG=zh_CN&VN_CODE=102&LOC=CN&PLT=PC&DS=Android'
           f'&UID={uid}&DT=PC')
    return raw.replace('=', '%3D').replace('&', '%26')


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _review_time(moment):
    """publish_time / created_time（Unix 秒）→ UTC datetime；缺失返回 None。"""
    for key in ('publish_time', 'created_time'):
        ts = moment.get(key)
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts, UTC)
    return None


def parse_review(entry, app_id):
    """把 data.list[] 的一个元素解析成标准 item；非评价 / 缺关键字段返回 None。"""
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
    raw = str(contents.get('text') or contents.get('raw_text') or '')
    for br in ('<br />', '<br/>', '<br>'):
        raw = raw.replace(br, '\n')
    text = news_common.strip_html(raw).strip()
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
        platform_region='cn',
        time_str=published.isoformat(),
        url=f'https://www.taptap.cn/review/{review_id}',
        engagement=likes + comments,
        is_hot=likes >= 50,
        author=nickname,
        tags=[label] if label else [],
        lang='zh',
        region='cn',
        archive_subtype='review',
    )
    metadata = {
        'app_id': str(app_id),
        'app_label': TAPTAP_APPS.get(str(app_id), ''),
        'score': score,
        'likes_count': likes,
        'comments_count': comments,
        'author_is_unknown': not nickname,
    }
    if author_id:
        metadata['author_id'] = author_id
    if moment.get('device'):
        metadata['device'] = str(moment['device'])
    item['metadata'] = metadata
    return item


def _fetch_app(app_id, cutoff, getter, sleep):
    """翻页取单应用时窗内评价。sort=new 时间倒序，整页都在时窗外即停止。"""
    items, seen = [], set()
    offset = 0
    for page in range(MAX_PAGES):
        if page:
            sleep(REQUEST_DELAY)
        params = {'app_id': app_id, 'limit': PAGE_LIMIT, 'sort': 'new', 'from': offset}
        url = f'{API_URL}?{urlencode(params)}&X-UA={_x_ua()}'
        data = getter(url).json()
        if not isinstance(data, dict) or not data.get('success', True):
            raise ValueError(f'TapTap app {app_id}: unexpected response')
        body = data.get('data') or {}
        entries = body.get('list') or []
        if not entries:
            break
        parsed = [parse_review(e, app_id) for e in entries]
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


def fetch_taptap_reviews(cutoff=None, getter=None, sleep=time.sleep):
    """采集正式服 + 测试服时窗内的新评价。单应用失败只告警，不让整轮失败。"""
    if cutoff is None:
        hours = news_common.env_int('HOURS_LOOKBACK', 24)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
    if getter is None:
        def getter(url):
            return news_common.get_with_retry(
                url, timeout=15,
                headers={'User-Agent': 'Mozilla/5.0',
                         'Referer': 'https://www.taptap.cn/app/364992/review'})
    items, failures = [], 0
    for idx, app_id in enumerate(TAPTAP_APPS):
        if idx:
            sleep(REQUEST_DELAY)
        try:
            got = _fetch_app(app_id, cutoff, getter, sleep)
            logger.info('TapTap app %s: %d reviews in window', app_id, len(got))
            items.extend(got)
        except Exception as e:  # noqa: BLE001  单应用失败降级
            failures += 1
            logger.warning('TapTap app %s failed: %s', app_id, e)
    if failures == len(TAPTAP_APPS):
        raise RuntimeError('TapTap: all apps failed')
    return items
