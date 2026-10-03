"""Reddit 评论采集（官方 OAuth API）。

背景：现有 fetch_reddit（RSS / 公开 JSON）只采帖子，评论一条没有；机房 IP 下评论 RSS 返回 429、
`.json` 返回 403，只剩 Reddit 官方 OAuth API 一条路（守密人注册 script 类型应用，见
projects/news/NEWS_SOURCES.md「Reddit 应用怎么注册」）。

凭据（环境变量，绝不进日志 / 异常消息 / 归档）：
  REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET   必需；缺任一 → 返回 [] 并 info「未配置，跳过」，不算失败源
  REDDIT_USERNAME / REDDIT_PASSWORD         可选；两者齐全走 password grant，否则 client_credentials
  REDDIT_USER_AGENT                         可选；写完整 UA（含 "(by /u/"）或只写 Reddit 用户名

取数策略（两个入口合用）：
  1. 子版块新帖 /r/<sub>/new：时窗内的新帖（综合版块按标题关键词过滤，与 fetch_reddit 同一套 ALL_KEYWORDS）。
  2. 子版块最新评论流 /r/<sub>/comments：作为「近期有新评论的帖子」发现入口（老帖被回复也能发现，
     综合版块按 link_title 过滤关键词），评论流本身也兜底入库——帖子数超上限时，没拉评论树的帖子
     仍保有评论流里的那几条（此时无 depth）。
  3. 对每个候选帖拉评论树 /r/<sub>/comments/<post_id>.json?sort=new&limit=500&depth=10，
     展开成扁平评论；遇 `more` 节点调 /api/morechildren（每帖次数有上限）。
  只保留 time >= cutoff 的评论（更老的已在先前轮次入湖）。

落点：source=reddit_comment，经 archive_layout.FOLDED_SOURCE_LAYOUT 折叠到宿主平台 reddit，
  归档 reddit/global/comment/YYYY-MM-DD.json（与 taptap_review → taptap/cn/review 同一机制）；
  reddit 帖子保持平铺 reddit/YYYY-MM-DD.json，宿主遍历靠 CLAIMED_SUBTYPES 避开 comment/ 防双计。
  去重键 = 评论永久链接 URL（collect_global.dedup_key 与 archive_platforms.item_key 都是 URL 优先，稳定）。

限速：OAuth 额度 100 请求 / 分钟。读 X-Ratelimit-Remaining / Reset 自适应：余量见底就睡到窗口重置（等待上限
  MAX_WAIT_SECONDS，超限则收工返回已采部分）；另有固定请求间隔、每轮请求总数上限、每子版块帖子数上限、
  单帖 / 单子版块失败降级（只告警，不拖垮整轮；全部子版块发现都失败才抛错交编排层记失败）。
"""
import logging
import os
import time
from datetime import datetime, timedelta, UTC

import requests

import news_common

logger = logging.getLogger(__name__)

TOKEN_URL = 'https://www.reddit.com/api/v1/access_token'
API_BASE = 'https://oauth.reddit.com'
WEB_BASE = 'https://www.reddit.com'

# 与 global_collectors.fetch_reddit 的默认子版块一致；前两个是专属版块（不做关键词过滤）
DEFAULT_SUBREDDITS = ('Morimens', 'MorimensGame', 'gachagaming')
DEDICATED_SUBREDDITS = {'morimens', 'morimensgame'}

DEFAULT_UA = 'python:biav-sc-news:1.0 (by /u/biav_sc_bot)'
DELETED_BODIES = {'[deleted]', '[removed]'}

REQUEST_DELAY = 1.0            # 请求间隔（秒）；100/min 额度下留余量
MAX_REQUESTS_PER_RUN = 90      # 每轮 API 请求总数上限（不含取 token）
MAX_POSTS_PER_SUB = 15         # 每子版块每轮最多拉评论树的帖子数
MAX_MORE_CALLS_PER_POST = 3    # 每帖 /api/morechildren 调用上限
MORE_CHUNK = 100               # morechildren 单次 children 上限（API 限制 100）
LISTING_LIMIT = 100
RESERVE_REMAINING = 2          # X-Ratelimit-Remaining 低于等于此值即等待窗口重置
MAX_WAIT_SECONDS = 90          # 单次限速等待上限；超过则放弃本轮剩余请求


class RateLimitExhausted(Exception):
    """额度见底且重置时间超过 MAX_WAIT_SECONDS，或请求预算用尽：本轮收工。"""


def credentials_from_env(env=None):
    """读凭据；缺 id / secret 返回 None。"""
    env = os.environ if env is None else env
    cid = (env.get('REDDIT_CLIENT_ID') or '').strip()
    secret = (env.get('REDDIT_CLIENT_SECRET') or '').strip()
    if not cid or not secret:
        return None
    return {
        'client_id': cid,
        'client_secret': secret,
        'username': (env.get('REDDIT_USERNAME') or '').strip(),
        'password': env.get('REDDIT_PASSWORD') or '',
    }


def build_user_agent(raw=None):
    """REDDIT_USER_AGENT：含 "(by /u/" 视为完整 UA；否则当作 Reddit 用户名嵌入规范模板。"""
    raw = (raw or '').strip()
    if not raw:
        return DEFAULT_UA
    if '(by /u/' in raw:
        return raw
    name = raw.lstrip('/').removeprefix('u/').strip()
    return f'python:biav-sc-news:1.0 (by /u/{name})' if name else DEFAULT_UA


class RedditClient:
    """极简 OAuth 客户端：取 token、限速自适应、请求预算。凭据只存在于此对象内。"""

    def __init__(self, creds, user_agent, http=None, sleep=time.sleep, clock=time.time,
                 max_requests=MAX_REQUESTS_PER_RUN, delay=REQUEST_DELAY):
        self._creds = creds
        self._ua = user_agent
        self._http = http or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._token = None
        self._token_expiry = 0.0
        self.max_requests = max_requests
        self.delay = delay
        self.requests_made = 0
        self.stopped = False  # 额度耗尽 / 预算用尽后置位，后续调用直接抛 RateLimitExhausted

    def _redact(self, text):
        text = str(text)
        for secret in (self._creds.get('client_secret'), self._creds.get('password'),
                       self._creds.get('client_id'), self._token):
            if secret:
                text = text.replace(secret, '***')
        return text

    def describe_error(self, exc):
        """日志用：异常类型 + 已脱敏消息（绝不含凭据 / 请求头）。"""
        return f'{type(exc).__name__}: {self._redact(exc)}'[:200]

    # ── token ──
    def authenticate(self):
        c = self._creds
        if c['username'] and c['password']:
            data = {'grant_type': 'password', 'username': c['username'], 'password': c['password']}
            mode = 'password'
        else:
            data = {'grant_type': 'client_credentials', 'device_id': 'DO_NOT_TRACK_THIS_DEVICE'}
            mode = 'client_credentials'
        resp = self._http.post(TOKEN_URL, data=data, auth=(c['client_id'], c['client_secret']),
                               headers={'User-Agent': self._ua}, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        token = body.get('access_token') if isinstance(body, dict) else None
        if not token:
            raise RuntimeError('Reddit token 响应缺 access_token（凭据无效或应用类型不符）')
        self._token = token
        self._token_expiry = self._clock() + max(int(body.get('expires_in') or 3600) - 60, 60)
        logger.info('Reddit OAuth token 获取成功（%s）', mode)

    # ── 限速 ──
    def _absorb_rate_headers(self, resp):
        """读 X-Ratelimit-Remaining / Reset；余量见底则睡到重置（过久则标记收工）。"""
        h = resp.headers or {}
        try:
            remaining = float(h.get('X-Ratelimit-Remaining'))
            reset = float(h.get('X-Ratelimit-Reset'))
        except (TypeError, ValueError):
            return
        if remaining <= RESERVE_REMAINING:
            if reset > MAX_WAIT_SECONDS:
                self.stopped = True
                raise RateLimitExhausted(f'额度见底，{reset:.0f}s 后重置（上限 {MAX_WAIT_SECONDS}s）')
            logger.info('Reddit 额度余 %.0f，等待 %.0fs 至窗口重置', remaining, reset + 1)
            self._sleep(reset + 1)

    def get_json(self, path, params=None):
        """GET oauth.reddit.com<path>，返回解析后的 JSON。401 刷新 token 重试一次；429 按头等待重试一次。"""
        retried_auth = retried_429 = False
        while True:
            if self.stopped or self.requests_made >= self.max_requests:
                self.stopped = True
                raise RateLimitExhausted('本轮请求预算用尽或已被限流')
            if self._token is None or self._clock() >= self._token_expiry:
                self.authenticate()
            if self.requests_made:
                self._sleep(self.delay)
            self.requests_made += 1
            resp = self._http.get(
                API_BASE + path, params={**(params or {}), 'raw_json': 1},
                headers={'Authorization': f'bearer {self._token}', 'User-Agent': self._ua},
                timeout=20)
            if resp.status_code == 401 and not retried_auth:
                retried_auth = True
                self._token = None
                continue
            if resp.status_code == 429:
                h = resp.headers or {}
                try:
                    wait = float(h.get('Retry-After') or h.get('X-Ratelimit-Reset') or 60)
                except (TypeError, ValueError):
                    wait = 60.0
                if retried_429 or wait > MAX_WAIT_SECONDS:
                    self.stopped = True
                    raise RateLimitExhausted(f'429 限流（建议等待 {wait:.0f}s）')
                retried_429 = True
                logger.info('Reddit 429，等待 %.0fs 后重试一次', wait + 1)
                self._sleep(wait + 1)
                continue
            resp.raise_for_status()
            self._absorb_rate_headers(resp)
            return resp.json()


# ── 解析 ──

def _iso(ts):
    return datetime.fromtimestamp(float(ts), UTC)


def _relevant(sub, title, keywords):
    """专属版块全收；综合版块按标题含关键词（与 fetch_reddit 同逻辑）。"""
    if sub.lower() in DEDICATED_SUBREDDITS:
        return True
    low = (title or '').lower()
    return any(kw.lower() in low for kw in keywords)


def parse_comment(data, sub, post_title, stats, cutoff=None):
    """t1 → 标准 item；已删除 / 被移除 / 空正文 / 时窗外 / 缺关键字段返回 None（并在 stats 计数）。"""
    if not isinstance(data, dict):
        return None
    body = (data.get('body') or '').strip()
    author = data.get('author') or ''
    if body in DELETED_BODIES or author == '[deleted]' or not body:
        stats['deleted'] = stats.get('deleted', 0) + 1
        return None
    permalink = data.get('permalink') or ''
    created = data.get('created_utc')
    if not permalink or created is None:
        stats['malformed'] = stats.get('malformed', 0) + 1
        return None
    when = _iso(created)
    if cutoff is not None and when < cutoff:
        stats['stale'] = stats.get('stale', 0) + 1
        return None
    score = data.get('score')
    score = score if isinstance(score, int) else 0
    sub_name = data.get('subreddit') or sub
    item = news_common.make_item(
        title=f'[r/{sub_name}] 评论 · {post_title or data.get("link_title") or ""}'[:120],
        summary=body,
        source='reddit_comment',
        platform_region='global',
        time_str=when.isoformat(),
        url=WEB_BASE + permalink,
        engagement=score,
        is_hot=score >= 50,
        author=author,
        tags=[],
        lang='en',
        region='global',
        archive_subtype='comment',
    )
    metadata = {
        'post_id': str(data.get('link_id') or '').removeprefix('t3_'),
        'parent_id': data.get('parent_id') or '',
        'score': score,
        'subreddit': sub_name,
    }
    if data.get('depth') is not None:
        metadata['depth'] = int(data['depth'])
    if data.get('author_fullname'):
        metadata['author_id'] = data['author_fullname']
    item['metadata'] = metadata
    return item


def _walk(children, sub, post_title, stats, cutoff, items, more_ids):
    """递归展开评论树：t1 入 items，更深的 replies 继续递归，more 节点的 children 收进 more_ids。"""
    for node in children or []:
        if not isinstance(node, dict):
            continue
        kind, data = node.get('kind'), node.get('data') or {}
        if kind == 't1':
            item = parse_comment(data, sub, post_title, stats, cutoff)
            if item is not None:
                items.append(item)
            replies = data.get('replies')
            if isinstance(replies, dict):
                _walk((replies.get('data') or {}).get('children'), sub, post_title,
                      stats, cutoff, items, more_ids)
        elif kind == 'more':
            for cid in data.get('children') or []:
                more_ids.append(cid)


def fetch_post_comments(client, sub, post_id, post_title, cutoff, stats):
    """拉一个帖子的评论树并展开；more 节点最多调 MAX_MORE_CALLS_PER_POST 次 morechildren。"""
    data = client.get_json(f'/r/{sub}/comments/{post_id}.json',
                           {'sort': 'new', 'limit': 500, 'depth': 10})
    if not isinstance(data, list) or len(data) < 2:
        raise ValueError('评论树响应形状异常')
    items, more_ids = [], []
    _walk(((data[1] or {}).get('data') or {}).get('children'), sub, post_title,
          stats, cutoff, items, more_ids)
    calls = 0
    while more_ids and calls < MAX_MORE_CALLS_PER_POST:
        chunk, more_ids = more_ids[:MORE_CHUNK], more_ids[MORE_CHUNK:]
        calls += 1
        resp = client.get_json('/api/morechildren', {
            'api_type': 'json', 'link_id': f't3_{post_id}', 'children': ','.join(chunk),
            'sort': 'new', 'limit_children': 'false'})
        things = ((resp.get('json') or {}).get('data') or {}).get('things') or []
        for thing in things:
            kind, d = thing.get('kind'), thing.get('data') or {}
            if kind == 't1':
                item = parse_comment(d, sub, post_title, stats, cutoff)
                if item is not None:
                    items.append(item)
            elif kind == 'more':
                more_ids.extend(d.get('children') or [])
    if more_ids:
        stats['more_skipped'] = stats.get('more_skipped', 0) + len(more_ids)
    return items


def _discover_posts(client, sub, cutoff, keywords, stream_items, stats):
    """发现候选帖 → {post_id: {'title', 'active'}}；同时把评论流里的时窗内评论放进 stream_items。"""
    posts = {}
    new = client.get_json(f'/r/{sub}/new', {'limit': LISTING_LIMIT})
    for ch in ((new.get('data') or {}).get('children') or []):
        d = ch.get('data') or {}
        pid, created = d.get('id'), d.get('created_utc')
        if not pid or created is None or _iso(created) < cutoff:
            continue
        if not d.get('num_comments'):
            continue  # 新帖暂无评论，没有可拉的
        if not _relevant(sub, d.get('title'), keywords):
            continue
        posts[pid] = {'title': d.get('title') or '', 'active': float(created)}
    stream = client.get_json(f'/r/{sub}/comments', {'limit': LISTING_LIMIT})
    for ch in ((stream.get('data') or {}).get('children') or []):
        d = ch.get('data') or {}
        pid = str(d.get('link_id') or '').removeprefix('t3_')
        if not pid or d.get('created_utc') is None or _iso(d['created_utc']) < cutoff:
            continue
        title = d.get('link_title') or ''
        if not _relevant(sub, title, keywords):
            continue
        entry = posts.setdefault(pid, {'title': title, 'active': 0.0})
        entry['title'] = entry['title'] or title
        entry['active'] = max(entry['active'], float(d['created_utc']))
        item = parse_comment(d, sub, entry['title'], stats, cutoff)
        if item is not None:
            stream_items[item['url']] = item
    return posts


def fetch_reddit_comments(cutoff=None, env=None, http=None, sleep=time.sleep, clock=time.time,
                          subreddits=None, keywords=None):
    """采集时窗内的 Reddit 评论；凭据缺失返回 []（不算失败）。"""
    creds = credentials_from_env(env)
    if creds is None:
        logger.info('Reddit 评论采集：REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET 未配置，跳过')
        return []
    if cutoff is None:
        hours = news_common.env_int('HOURS_LOOKBACK', 24)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
    if keywords is None:
        import global_collectors  # 延迟导入：global_collectors 顶层已导入本模块，避免循环
        keywords = global_collectors.ALL_KEYWORDS
    subreddits = subreddits or DEFAULT_SUBREDDITS
    ua = build_user_agent((env if env is not None else os.environ).get('REDDIT_USER_AGENT'))
    client = RedditClient(creds, ua, http=http, sleep=sleep, clock=clock)

    try:
        client.authenticate()
    except Exception as e:  # noqa: BLE001  已配置却取不到 token = 真故障，脱敏后上抛
        raise RuntimeError(f'Reddit OAuth 取 token 失败：{client.describe_error(e)}') from None

    stats, collected, discovery_failures = {}, {}, 0
    for sub in subreddits:
        stream_items, fetched_posts = {}, set()
        try:
            posts = _discover_posts(client, sub, cutoff, keywords, stream_items, stats)
        except RateLimitExhausted as e:
            logger.warning('Reddit r/%s 发现阶段停止（限流收工，非源故障）：%s', sub, e)
            break
        except Exception as e:  # noqa: BLE001  单子版块失败降级
            discovery_failures += 1
            logger.warning('Reddit r/%s 发现候选帖失败：%s', sub, client.describe_error(e))
            continue
        ranked = sorted(posts.items(), key=lambda kv: -kv[1]['active'])[:MAX_POSTS_PER_SUB]
        for pid, meta in ranked:
            try:
                for item in fetch_post_comments(client, sub, pid, meta['title'], cutoff, stats):
                    collected[item['url']] = item
                fetched_posts.add(pid)
            except RateLimitExhausted as e:
                logger.warning('Reddit 评论树停止：%s', e)
                break
            except Exception as e:  # noqa: BLE001  单帖失败降级
                logger.warning('Reddit r/%s 帖 %s 评论树失败：%s', sub, pid, client.describe_error(e))
        # 评论流兜底：没拉到评论树的帖子，保留评论流里的那几条；树里已有的以树为准（带 depth）
        for url, item in stream_items.items():
            if url not in collected:
                collected[url] = item
        logger.info('Reddit r/%s：候选帖 %d，拉树 %d，累计评论 %d', sub, len(posts),
                    len(fetched_posts), len(collected))
        if client.stopped:
            break

    if discovery_failures and discovery_failures >= len(subreddits) and not collected:
        raise RuntimeError('Reddit 评论：全部子版块发现阶段失败')
    logger.info('Reddit 评论采集完成：%d 条（跳过已删除/移除 %d，时窗外 %d，more 未展开 %d；请求 %d 次）',
                len(collected), stats.get('deleted', 0), stats.get('stale', 0),
                stats.get('more_skipped', 0), client.requests_made)
    return list(collected.values())
