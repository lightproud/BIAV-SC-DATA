"""B 站视频评论采集（公开接口，免登录免签名）。

背景：fetch_bilibili 只用 wbi 视频搜索采视频标题 / 简介 / 播放量，评论区一条没采。
评论接口 `x/v2/reply/main` 实测免签名可用（带浏览器 UA + Referer）。

视频来源（每轮上限 MAX_VIDEOS）：
  1. 本轮 wbi 视频搜索（与 fetch_bilibili 同一套关键词 / 签名，news_common 共享）：时窗内的新视频，
     评论取 cutoff 之后的新评论。
  2. 近 ARCHIVE_DAYS 天已归档的 bilibili 视频（经 archive_layout 读，aid 取自归档 url 的 av 号或 BV 号离线换算），
     按热度（engagement）取前 OLD_VIDEO_LIMIT 个；老视频的评论时窗放宽到近 OLD_COMMENT_DAYS 天
     （同一条评论重复采到由 URL 去重键兜住）。无状态近似：不另存评论数快照，未做精确的「评论数增长」判定。

取数：评论按时间序（mode=2）翻页，一页最老一条早于时窗即止；根评论带 rcount>0 的最多展开
  MAX_EXPAND_PER_VIDEO 条楼中楼（x/v2/reply/reply，最多 MAX_SUB_PAGES 页）。已删除 / 空正文 / 时窗外丢弃。

风控：请求间隔 REQUEST_DELAY（≥1 秒）；遇 -412 / -352 / -799 / -509 / HTTP 412 / 429 立即停本轮，
  返回已采部分（降级，不抛错、不拖垮整轮）；单视频评论区关闭 / 视频失效 / 异常只跳过该视频。
  每轮请求总数上限 MAX_REQUESTS（含搜索请求之外的全部评论请求）。

落点：source=bilibili_comment，经 archive_layout.FOLDED_SOURCE_LAYOUT 折叠到宿主平台 bilibili，
  归档 bilibili/cn/comment/YYYY-MM-DD.json（与 reddit_comment 同一机制）；视频仍平铺 bilibili/YYYY-MM-DD.json，
  宿主遍历靠 CLAIMED_SUBTYPES 避开 comment/ 防双计。
  去重键 = https://www.bilibili.com/video/<bvid>#reply<rpid>（URL 优先，稳定）。
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, UTC

import requests

import archive_layout
import news_common

logger = logging.getLogger(__name__)

SEARCH_URL = 'https://api.bilibili.com/x/web-interface/wbi/search/type'
REPLY_MAIN_URL = 'https://api.bilibili.com/x/v2/reply/main'
REPLY_SUB_URL = 'https://api.bilibili.com/x/v2/reply/reply'
VIDEO_URL = 'https://www.bilibili.com/video/'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
BASE_HEADERS = {'User-Agent': UA, 'Referer': 'https://www.bilibili.com/'}

REQUEST_DELAY = 1.0            # 请求间隔（秒），守密人要求 >= 1
MAX_REQUESTS = 80              # 每轮评论接口请求总数上限（不含 spi / nav 取签名）
MAX_VIDEOS = 30                # 每轮视频数上限
OLD_VIDEO_LIMIT = 15           # 其中来自归档的老视频上限
SEARCH_PER_KEYWORD = 25        # 与 fetch_bilibili 一致
ARCHIVE_DAYS = 7               # 读最近几天的 bilibili 归档
OLD_COMMENT_DAYS = 3           # 老视频评论时窗
PAGE_SIZE = 20
MAX_PAGES_PER_VIDEO = 2
MAX_EXPAND_PER_VIDEO = 2       # 每视频最多展开几条根评论的楼中楼
MAX_SUB_PAGES = 2
RISK_CODES = {-412, -352, -799, -509}   # 风控：停本轮
SKIP_VIDEO_CODES = {12002, 12061, -404, 12009, 12010}  # 评论区关闭 / 视频失效：只跳过该视频


class RiskControl(Exception):
    """触发 B 站风控或请求预算用尽：本轮收工，返回已采部分。"""


# ── aid <-> bvid（官方算法，离线换算，不出网）──
_XOR = 23442827791579
_MASK = 2251799813685247
_MAXAID = 1 << 51
_BASE = 58
_ALPHABET = 'FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf'


def av2bv(aid):
    tmp = (_MAXAID | int(aid)) ^ _XOR
    bv = list('BV1000000000')
    i = len(bv) - 1
    while tmp > 0:
        bv[i] = _ALPHABET[tmp % _BASE]
        tmp //= _BASE
        i -= 1
    bv[3], bv[9] = bv[9], bv[3]
    bv[4], bv[7] = bv[7], bv[4]
    return ''.join(bv)


def bv2av(bvid):
    bv = list(bvid)
    bv[3], bv[9] = bv[9], bv[3]
    bv[4], bv[7] = bv[7], bv[4]
    tmp = 0
    for ch in bv[3:]:
        tmp = tmp * _BASE + _ALPHABET.index(ch)
    return (tmp & _MASK) ^ _XOR


_AV_RE = re.compile(r'/video/av(\d+)', re.I)
_BV_RE = re.compile(r'/video/(BV[0-9A-Za-z]{10})')


def video_ids_from_url(url):
    """归档 / 搜索条目 url → (aid, bvid)；认不出返回 (None, None)。"""
    url = url or ''
    m = _AV_RE.search(url)
    if m:
        aid = int(m.group(1))
        return aid, av2bv(aid)
    m = _BV_RE.search(url)
    if m:
        try:
            return bv2av(m.group(1)), m.group(1)
        except (ValueError, IndexError):
            return None, None
    return None, None


# ── HTTP 客户端 ──
class BiliClient:
    def __init__(self, http=None, sleep=time.sleep, max_requests=None, delay=None):
        self._http = http or requests.Session()
        self._sleep = sleep
        self.max_requests = MAX_REQUESTS if max_requests is None else max_requests
        self.delay = REQUEST_DELAY if delay is None else delay
        self.requests_made = 0
        self.headers = dict(BASE_HEADERS)

    def get_json(self, url, params):
        """GET 并返回 JSON；风控 / 预算用尽抛 RiskControl；其余网络 / 解析异常原样上抛交调用方降级。"""
        if self.requests_made >= self.max_requests:
            raise RiskControl('本轮请求预算用尽')
        if self.requests_made:
            self._sleep(self.delay)
        self.requests_made += 1
        resp = self._http.get(url, params=params, headers=self.headers, timeout=15)
        if resp.status_code in (412, 429):
            raise RiskControl(f'HTTP {resp.status_code} 风控')
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError('响应形状异常')
        code = body.get('code')
        if code in RISK_CODES:
            raise RiskControl(f'风控码 {code}')
        return body


# ── 视频发现 ──
def search_videos(http=None, keywords=None, cutoff=None):
    """wbi 视频搜索（同 fetch_bilibili）；返回 [{'aid','bvid','title','engagement'}]。失败返回已得部分。"""
    if keywords is None:
        import global_collectors  # 延迟导入：global_collectors 顶层导入本模块，避免循环
        keywords = global_collectors.KEYWORDS['zh']
    sess = http or requests
    headers = dict(BASE_HEADERS)
    spi = news_common.bilibili_spi_cookies(headers)
    if spi:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in spi.items())
    mixin_key = news_common.get_wbi_mixin_key(headers)
    videos, seen = [], set()
    for kw in keywords:
        try:
            params = {'search_type': 'video', 'keyword': kw, 'order': 'pubdate', 'page': 1}
            if mixin_key:
                params = news_common.sign_wbi_params(params, mixin_key)
            data = sess.get(SEARCH_URL, params=params, headers=headers, timeout=15).json()
            for v in ((data.get('data') or {}).get('result') or [])[:SEARCH_PER_KEYWORD]:
                pubdate = v.get('pubdate') or 0
                if not pubdate:
                    continue
                if cutoff is not None and datetime.fromtimestamp(pubdate, UTC) < cutoff:
                    continue
                aid, bvid = v.get('aid'), v.get('bvid')
                if not aid:
                    aid, bvid = video_ids_from_url(v.get('arcurl'))
                if not aid:
                    continue
                aid = int(aid)
                if aid in seen:
                    continue
                seen.add(aid)
                videos.append({'aid': aid, 'bvid': bvid or av2bv(aid),
                               'title': news_common.strip_html(v.get('title') or ''),
                               'engagement': (v.get('play') or 0) + (v.get('danmaku') or 0)})
        except Exception as e:  # noqa: BLE001  单关键词失败降级
            logger.warning('B 站评论：搜索 "%s" 失败：%s', kw, type(e).__name__)
    return videos


def archived_videos(days=ARCHIVE_DAYS, archive_dir=None, today=None):
    """近 days 天 bilibili 视频归档（经 archive_layout 读）→ 按 engagement 降序的视频列表。"""
    root = archive_dir or archive_layout.community_root()
    today = today or datetime.now(UTC).date()
    stems = {(today - timedelta(days=i)).isoformat() for i in range(days + 1)}
    videos = {}
    try:
        files = [f for f in archive_layout.dated_files('bilibili', root)
                 if archive_layout.date_stem(f) in stems]
    except OSError as e:
        logger.warning('B 站评论：读归档失败：%s', type(e).__name__)
        return []
    for f in files:
        try:
            with archive_layout.open_archive_text(f) as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            logger.warning('B 站评论：归档 %s 不可读：%s', f.name, type(e).__name__)
            continue
        for it in (doc.get('items') or []) if isinstance(doc, dict) else []:
            if it.get('content_type') not in (None, 'video'):
                continue
            aid, bvid = video_ids_from_url(it.get('url'))
            if not aid:
                continue
            eng = it.get('engagement') or 0
            prev = videos.get(aid)
            if prev is None or eng > prev['engagement']:
                videos[aid] = {'aid': aid, 'bvid': bvid, 'title': it.get('title') or '',
                               'engagement': eng}
    return sorted(videos.values(), key=lambda v: -v['engagement'])


# ── 解析 ──
def parse_reply(r, video, stats, cutoff, is_reply=False):
    """评论对象 → 标准 item；已删除 / 空正文 / 时窗外 / 缺关键字段返回 None（并在 stats 计数）。"""
    if not isinstance(r, dict):
        return None
    rpid, ctime = r.get('rpid'), r.get('ctime')
    if not rpid or ctime is None:
        stats['malformed'] = stats.get('malformed', 0) + 1
        return None
    body = ((r.get('content') or {}).get('message') or '').strip()
    if not body:
        stats['empty'] = stats.get('empty', 0) + 1
        return None
    when = datetime.fromtimestamp(int(ctime), UTC)
    if cutoff is not None and when < cutoff:
        stats['stale'] = stats.get('stale', 0) + 1
        return None
    member = r.get('member') or {}
    like = r.get('like') if isinstance(r.get('like'), int) else 0
    parent = r.get('parent') or 0
    item = news_common.make_item(
        title=f'[B站评论] {video.get("title") or video["bvid"]}'[:120],
        summary=body,
        source='bilibili_comment',
        platform_region='cn',
        time_str=when.isoformat(),
        url=f'{VIDEO_URL}{video["bvid"]}#reply{rpid}',
        engagement=like,
        is_hot=like >= 50,
        author=member.get('uname') or '',
        tags=[],
        lang='zh',
        region='cn',
        archive_subtype='comment',
    )
    item['metadata'] = {
        'author_id': str(r.get('mid') or member.get('mid') or ''),
        'video_aid': str(video['aid']),
        'video_bvid': video['bvid'],
        'parent_rpid': str(parent) if parent else '',
        'like': like,
        'reply_count': int(r.get('rcount') or 0),
        'is_reply': bool(is_reply or (r.get('root') or 0)),
    }
    return item


def fetch_video_comments(client, video, cutoff, stats):
    """一个视频的评论（时间序翻页 + 少量楼中楼）。风控 / 预算用尽抛 RiskControl（调用方收工）。"""
    items, expand_left, nxt = [], MAX_EXPAND_PER_VIDEO, 0
    for _ in range(MAX_PAGES_PER_VIDEO):
        body = client.get_json(REPLY_MAIN_URL, {'type': 1, 'oid': video['aid'], 'mode': 2,
                                                 'next': nxt, 'ps': PAGE_SIZE})
        code = body.get('code')
        if code in SKIP_VIDEO_CODES:
            stats['skipped_videos'] = stats.get('skipped_videos', 0) + 1
            return items
        if code != 0:
            raise ValueError(f'评论接口返回 code={code}')
        data = body.get('data') or {}
        tops = (data.get('top_replies') or []) if nxt == 0 else []
        timeline = list(data.get('replies') or [])
        page_has_stale = bool(timeline) and not _recent(timeline[-1], cutoff)  # 时间序：末条已过时窗 → 无需翻页
        for r in list(tops) + timeline:
            it = parse_reply(r, video, stats, cutoff)
            if it is not None:
                items.append(it)
            if isinstance(r, dict) and (r.get('rcount') or 0) > 0 and expand_left > 0 \
                    and _recent(r, cutoff):
                expand_left -= 1
                items.extend(_fetch_subreplies(client, video, r.get('rpid'), cutoff, stats))
        cursor = data.get('cursor') or {}
        if cursor.get('is_end') or page_has_stale or not timeline:
            break
        nxt = cursor.get('next') or 0
        if not nxt:
            break
    return items


def _recent(r, cutoff):
    try:
        return cutoff is None or datetime.fromtimestamp(int(r.get('ctime')), UTC) >= cutoff
    except (TypeError, ValueError):
        return False


def _fetch_subreplies(client, video, root_rpid, cutoff, stats):
    out = []
    for pn in range(1, MAX_SUB_PAGES + 1):
        body = client.get_json(REPLY_SUB_URL, {'type': 1, 'oid': video['aid'], 'root': root_rpid,
                                                'pn': pn, 'ps': PAGE_SIZE})
        if body.get('code') != 0:
            break
        data = body.get('data') or {}
        replies = data.get('replies') or []
        for r in replies:
            it = parse_reply(r, video, stats, cutoff, is_reply=True)
            if it is not None:
                out.append(it)
        page = data.get('page') or {}
        if len(replies) < PAGE_SIZE or pn * PAGE_SIZE >= (page.get('count') or 0):
            break
    return out


def select_videos(searched, archived, max_videos=MAX_VIDEOS, old_limit=OLD_VIDEO_LIMIT):
    """合并视频清单：搜索到的新视频优先，其次归档老视频（≤ old_limit），总数 ≤ max_videos。
    返回 [(video, is_old)]。"""
    chosen, seen = [], set()
    for v in searched:
        if v['aid'] not in seen and len(chosen) < max_videos:
            seen.add(v['aid'])
            chosen.append((v, False))
    olds = 0
    for v in archived:
        if v['aid'] in seen or olds >= old_limit or len(chosen) >= max_videos:
            continue
        seen.add(v['aid'])
        olds += 1
        chosen.append((v, True))
    return chosen


def fetch_bilibili_comments(cutoff=None, http=None, sleep=time.sleep, keywords=None,
                            archive_dir=None, now=None):
    """采集 B 站视频评论；风控降级返回已采部分，不抛错。"""
    now = now or datetime.now(UTC)
    if cutoff is None:
        hours = news_common.env_int('HOURS_LOOKBACK', 24)
        cutoff = now - timedelta(hours=hours)
    old_cutoff = min(cutoff, now - timedelta(days=OLD_COMMENT_DAYS))
    searched = search_videos(http=http, keywords=keywords, cutoff=cutoff)
    archived = archived_videos(archive_dir=archive_dir, today=now.date())
    plan = select_videos(searched, archived)
    client = BiliClient(http=http, sleep=sleep)
    stats, collected, failed = {}, {}, 0
    for video, is_old in plan:
        try:
            for it in fetch_video_comments(client, video, old_cutoff if is_old else cutoff, stats):
                collected[it['url']] = it
        except RiskControl as e:
            logger.warning('B 站评论：停止本轮（降级，保留已采 %d 条）：%s', len(collected), e)
            break
        except Exception as e:  # noqa: BLE001  单视频失败降级
            failed += 1
            logger.warning('B 站评论：视频 av%s 失败：%s', video['aid'], type(e).__name__)
    logger.info('B 站评论采集完成：%d 条（视频 %d：搜索 %d + 归档 %d；失败 %d；时窗外 %d；请求 %d 次）',
                len(collected), len(plan), sum(1 for _, o in plan if not o),
                sum(1 for _, o in plan if o), failed, stats.get('stale', 0), client.requests_made)
    return list(collected.values())
