#!/usr/bin/env python3
"""
TapTap 社区采集器 - Playwright 无头浏览器方案

TapTap 已废弃 webapiv2 全部端点（均返回404），页面使用 Nuxt 客户端渲染。
本模块通过 headless Chromium 拦截网络响应或提取渲染后 DOM 来获取社区帖子和玩家评价。

目标页面:
  帖子: https://www.taptap.cn/app/364992/topic?type=official
  评价: https://www.taptap.cn/app/364992/review?type=new
"""

import asyncio
import json
import logging
import math
import re
import sys
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # 时间归一单一真源（H4）

logger = logging.getLogger("collector.taptap")

# Morimens official page is taptap.cn/app/364992 (verified 2026-06-09;
# the previous 233553 pointed at a different game)
APP_ID = "364992"
TOPIC_URL = f"https://www.taptap.cn/app/{APP_ID}/topic?type=official"
REVIEW_URL = f"https://www.taptap.cn/app/{APP_ID}/review?type=new"

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "data" / "state.json"
DATA_DIR = BASE_DIR / "data"

# taptap.cn 是国服站点：DOM 上的裸墙钟一律北京时（UTC+8），与归档分桶基准同源。
_BEIJING_TZ = timezone(timedelta(hours=8))


# ─── 增量状态 ─────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── 数字解析 ─────────────────────────────────────────────────

def _parse_num(s: Any) -> int:
    """从字符串或数字中提取整数（处理 '1.2万' 等格式）。"""
    if isinstance(s, (int, float)):
        return int(s)
    if not s:
        return 0
    text = str(s).replace(",", "")
    # 处理 '万' 单位
    m = re.search(r"([\d.]+)\s*万", text)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"[\d]+", text)
    return int(m.group()) if m else 0


def _parse_taptap_dom_time(time_str):
    """Parse TapTap DOM time strings into ISO datetime.

    解析委托 news_common.parse_relative_time（H4 收敛，覆盖 ISO/中英相对时间/
    "刚刚"/"昨天"/"前天"/绝对日期）。Returns ISO datetime string.
    Falls back to now if unparseable.
    """
    return news_common.parse_relative_time(time_str)[0]


def _parse_taptap_dom_score(raw: Any) -> int:
    """从 DOM 星级块的文本里取 1–5 分，取不到返回 0（不入标题、不写 score 字段）。

    评分块可能渲染成 "4.5"、"4 分"、"★★★★☆" 几种形态；API 路径有结构化 score，
    DOM 兜底只能从文本猜，所以拿不准就返回 0，让下游知道「这条没有评分」，而不是
    编一个出来。
    """
    if raw is None:
        return 0
    text = str(raw).strip()
    if not text:
        return 0
    filled = text.count('★')
    if filled:
        return min(filled, 5)
    m = re.search(r'\d+(?:\.\d+)?', text)
    if not m:
        return 0
    try:
        # 四舍五入而非 round()：内建 round 是银行家舍入，round(4.5)==4，
        # 对"4.5 星"这种半档评分会系统性地往下压一档。
        value = math.floor(float(m.group()) + 0.5)
    except ValueError:
        return 0
    return value if 1 <= value <= 5 else 0


# ─── 滚动深采（懒加载分页）─────────────────────────────────────

async def _autoscroll_collect(page, parse_fn, captured, max_scrolls: int, cutoff) -> list[dict]:
    """滚动页面触发懒加载，累积并合并所有捕获 API 响应中的条目（按 item_id/url 去重）。

    TapTap 评价/帖子页是 Nuxt 客户端渲染、滚动到底懒加载下一批。原逻辑只取首屏
    第一个有效响应（约十几条），此函数下滑 max_scrolls 次、把每次新触发的 XHR
    响应一并解析合并，直到：连续两轮无新增（到底）或已捕获条目最早时间早于 cutoff
    （回溯深度足够）。返回去重后的 raw 条目列表。
    """
    merged: dict[str, dict] = {}

    def _merge() -> None:
        # captured 由 page.on("response") 持续追加，每轮重新全量合并即可
        for _url, body in list(captured):
            for it in parse_fn(body):
                key = it.get("item_id") or it.get("url") or it.get("title", "")[:60]
                if key and key not in merged:
                    merged[key] = it

    def _reached_cutoff() -> bool:
        if not cutoff or not merged:
            return False
        try:
            earliest = min(
                datetime.fromisoformat(it["created"])
                for it in merged.values()
                if it.get("created")
            )
            return earliest < cutoff
        except Exception:
            return False

    _merge()  # 首屏已捕获的先并入
    stale = 0
    for _ in range(max(0, max_scrolls)):
        if _reached_cutoff():
            break
        prev = len(merged)
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(1800)  # 等待懒加载 XHR 返回
        _merge()
        if len(merged) == prev:
            stale += 1
            if stale >= 2:  # 连续两轮无新增 = 已到底
                break
        else:
            stale = 0

    return list(merged.values())


# ─── 帖子页 ───────────────────────────────────────────────────

def _parse_topic_api_body(body: dict) -> list[dict]:
    """从各种可能的 API 响应结构中提取帖子数据。"""
    data = body.get("data") or body

    # 尝试常见列表字段
    topic_list = None
    if isinstance(data, list):
        topic_list = data
    elif isinstance(data, dict):
        for key in ("list", "topics", "moments", "items", "records"):
            if isinstance(data.get(key), list):
                topic_list = data[key]
                break
        if not topic_list:
            # 深一层搜索
            for v in data.values():
                if isinstance(v, dict):
                    for key in ("list", "topics", "moments", "items"):
                        if isinstance(v.get(key), list) and v[key]:
                            topic_list = v[key]
                            break
                if topic_list:
                    break

    if not isinstance(topic_list, list) or not topic_list:
        return []

    results = []
    for item in topic_list:
        if not isinstance(item, dict):
            continue

        title = item.get("title") or item.get("summary", "")[:100]
        if not title:
            continue

        # 时间戳（秒级或毫秒级）
        ts = item.get("created_time") or item.get("created_at") or item.get("publish_time") or 0
        if isinstance(ts, str):
            ts = int(ts) if ts.isdigit() else 0
        if ts > 1e12:  # 毫秒转秒
            ts = ts // 1000
        created = datetime.fromtimestamp(ts, tz=UTC) if ts else datetime.now(UTC)

        like_count = _parse_num(item.get("like_count") or item.get("likes") or item.get("vote_count") or 0)
        comment_count = _parse_num(item.get("comment_count") or item.get("comments") or item.get("reply_count") or 0)

        item_id = str(item.get("id") or item.get("moment_id") or item.get("topic_id") or "")
        url = (
            item.get("share_url")
            or item.get("url")
            or (f"https://www.taptap.cn/moment/{item_id}" if item_id else "")
        )

        user = item.get("user") or item.get("author") or {}
        author = user.get("name") or user.get("username") or "" if isinstance(user, dict) else ""

        results.append({
            "title": str(title).strip(),
            "summary": str(item.get("summary") or item.get("intro") or "").strip(),
            "like_count": like_count,
            "comment_count": comment_count,
            "created": created.isoformat(),
            "url": url,
            "author": str(author),
            "item_id": item_id,
        })

    return results


async def _extract_topics(page, max_scrolls: int = 5, cutoff=None) -> list[dict]:
    """访问帖子页，滚动累积拦截的网络响应，fallback 到 DOM 提取。"""
    captured: list[tuple[str, dict]] = []

    async def handle_response(response):
        url = response.url
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        # 匹配 TapTap 内部 API（topic/moment/feed 等路径）
        if not re.search(r"/(topic|moment|feed|community|post)", url):
            return
        try:
            body = await response.json()
            captured.append((url, body))
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        await page.goto(TOPIC_URL, wait_until="networkidle", timeout=60000)
    except Exception as e:
        logger.warning(f"TapTap topic page load warning: {e}")

    # 额外等待 JS 异步渲染
    await page.wait_for_timeout(3000)

    # 1. 滚动累积所有拦截到的 API 响应（合并去重）
    items = await _autoscroll_collect(page, _parse_topic_api_body, captured, max_scrolls, cutoff)
    if items:
        logger.info(f"TapTap topics: {len(items)} after {max_scrolls}-scroll API merge")
        return items

    # 2. Fallback: DOM 提取（此时页面已滚动到底，DOM 含更多条目）
    logger.info("TapTap: API interception yielded no topics, falling back to DOM")
    return await _extract_topics_dom(page)


async def _extract_topics_dom(page) -> list[dict]:
    """从渲染后的 DOM 中提取帖子（保存 HTML 用于调试）。"""
    content = await page.content()
    debug_path = DATA_DIR / "debug_taptap_topic.html"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        debug_path.write_text(content, encoding="utf-8")
        logger.info(f"TapTap: saved topic DOM ({len(content)} chars) to {debug_path}")
    except Exception:
        pass

    result = await page.evaluate("""() => {
        // 常见 TapTap 帖子容器选择器（按优先级排列）
        const candidateSelectors = [
            '[class*="post-item"]',
            '[class*="topic-item"]',
            '[class*="moment-item"]',
            '[class*="community-post"]',
            '[class*="feed-item"]',
            '[class*="list-item"]',
            'article',
        ];

        let postElements = [];
        for (const sel of candidateSelectors) {
            const els = document.querySelectorAll(sel);
            if (els.length >= 3) {  // 至少3条才认为找到了
                postElements = Array.from(els).slice(0, 200);
                break;
            }
        }

        return postElements.map(el => {
            const titleEl = el.querySelector(
                'h1, h2, h3, h4, [class*="title"], [class*="heading"], [class*="name"]'
            );
            const timeEl = el.querySelector('time, [class*="time"], [class*="date"]');
            const likeEl = el.querySelector('[class*="like"], [class*="thumb"], [class*="vote"]');
            const commentEl = el.querySelector('[class*="comment"], [class*="reply"]');
            const linkEl = el.querySelector('a[href*="/moment/"], a[href*="/topic/"], a[href*="/post/"]')
                        || el.querySelector('a[href]');
            const authorEl = el.querySelector(
                '[class*="username"], [class*="user-name"], [class*="author"], [class*="nickname"]'
            );

            return {
                title: titleEl ? ((titleEl.innerText || titleEl.textContent || '').trim()) : '',
                time_str: timeEl
                    ? (timeEl.getAttribute('datetime') || ((timeEl.innerText || timeEl.textContent || '').trim()))
                    : '',
                likes: likeEl ? ((likeEl.innerText || likeEl.textContent || '').trim()) : '0',
                comments: commentEl ? ((commentEl.innerText || commentEl.textContent || '').trim()) : '0',
                url: linkEl ? linkEl.href : '',
                author: authorEl ? ((authorEl.innerText || authorEl.textContent || '').trim()) : '',
            };
        });
    }""")

    items = []
    for r in result or []:
        if not r.get("title"):
            continue
        created_str = _parse_taptap_dom_time(r.get("time_str", ""))
        item = {
            "title": r["title"],
            "summary": "",
            "like_count": _parse_num(r.get("likes")),
            "comment_count": _parse_num(r.get("comments")),
            "created": created_str,
            "url": r.get("url", ""),
            "author": r.get("author", ""),
            "item_id": "",
        }
        if not r.get("time_str"):
            item["time_is_approximate"] = True
        items.append(item)

    if not items:
        logger.warning("TapTap: DOM extraction found no topic elements")
    return items


# ─── 评价页 ───────────────────────────────────────────────────

def _parse_review_api_body(body: dict) -> list[dict]:
    """从 API 响应中提取评价数据。"""
    data = body.get("data") or body

    review_list = None
    if isinstance(data, list):
        review_list = data
    elif isinstance(data, dict):
        for key in ("list", "reviews", "rating_list", "items", "records", "comments"):
            if isinstance(data.get(key), list):
                review_list = data[key]
                break
        if not review_list:
            for v in data.values():
                if isinstance(v, dict):
                    for key in ("list", "reviews", "rating_list"):
                        if isinstance(v.get(key), list) and v[key]:
                            review_list = v[key]
                            break
                if review_list:
                    break

    if not isinstance(review_list, list) or not review_list:
        return []

    results = []
    for item in review_list:
        if not isinstance(item, dict):
            continue

        content = (
            item.get("comment")
            or item.get("content")
            or item.get("text")
            or item.get("body")
            or ""
        )
        if not content:
            continue

        score = item.get("score") or item.get("rating") or item.get("stars") or 0

        ts = item.get("created_time") or item.get("created_at") or 0
        if isinstance(ts, str):
            ts = int(ts) if ts.isdigit() else 0
        if ts > 1e12:
            ts = ts // 1000
        created = datetime.fromtimestamp(ts, tz=UTC) if ts else datetime.now(UTC)

        like_count = _parse_num(item.get("like_count") or item.get("likes") or 0)

        item_id = str(item.get("id") or item.get("review_id") or "")
        url = item.get("share_url") or (
            f"https://www.taptap.cn/review/{item_id}" if item_id else ""
        )

        user = item.get("user") or item.get("author") or {}
        author = user.get("name") or user.get("username") or "" if isinstance(user, dict) else ""

        star_str = f"{'★' * int(score)}{'☆' * max(0, 5 - int(score))} " if score else ""
        title = f"{star_str}{str(content)[:60]}".strip()

        results.append({
            "title": title,
            "summary": str(content).strip(),
            "like_count": like_count,
            "comment_count": 0,
            "created": created.isoformat(),
            "url": url,
            "author": str(author),
            "item_id": item_id,
        })

    return results


async def _extract_reviews(page, max_scrolls: int = 5, cutoff=None) -> list[dict]:
    """访问评价页，滚动累积拦截的网络响应，fallback 到 DOM 提取。"""
    captured: list[tuple[str, dict]] = []

    async def handle_response(response):
        url = response.url
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        if not re.search(r"/(review|rating|comment|score)", url):
            return
        try:
            body = await response.json()
            captured.append((url, body))
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        await page.goto(REVIEW_URL, wait_until="networkidle", timeout=60000)
    except Exception as e:
        logger.warning(f"TapTap review page load warning: {e}")

    await page.wait_for_timeout(3000)

    # 滚动累积所有拦截到的评价 API 响应（合并去重）
    items = await _autoscroll_collect(page, _parse_review_api_body, captured, max_scrolls, cutoff)
    if items:
        logger.info(f"TapTap reviews: {len(items)} after {max_scrolls}-scroll API merge")
        return items

    logger.info("TapTap: API interception yielded no reviews, falling back to DOM")
    return await _extract_reviews_dom(page)


async def _extract_reviews_dom(page) -> list[dict]:
    """从渲染后的 DOM 中提取评价（保存 HTML 用于调试）。"""
    content = await page.content()
    debug_path = DATA_DIR / "debug_taptap_review.html"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        debug_path.write_text(content, encoding="utf-8")
        logger.info(f"TapTap: saved review DOM ({len(content)} chars) to {debug_path}")
    except Exception:
        pass

    result = await page.evaluate("""() => {
        const candidateSelectors = [
            '[class*="review-item"]',
            '[class*="rating-item"]',
            '[class*="comment-item"]',
            '[class*="review-card"]',
            '[class*="comment-card"]',
        ];

        let els = [];
        for (const sel of candidateSelectors) {
            const found = document.querySelectorAll(sel);
            if (found.length >= 3) {
                els = Array.from(found).slice(0, 200);
                break;
            }
        }

        return els.map(el => {
            const timeEl = el.querySelector('time, [class*="time"], [class*="date"]');
            const likeEl = el.querySelector('[class*="like"], [class*="thumb"]');
            const authorEl = el.querySelector(
                '[class*="username"], [class*="user-name"], [class*="author"], [class*="nickname"]'
            );
            const starEl = el.querySelector('[class*="star"], [class*="score"], [class*="rating"]');
            const linkEl = el.querySelector('a[href*="/review/"]') || el.querySelector('a[href]');

            // 正文选择：原实现用 querySelector 取**文档序第一个**命中
            // '[class*="content"], [class*="text"], …' 的元素。TapTap 的卡片把用户名
            // 也包在带 content/text 类名的头部块里，且它排在正文之前 —— 于是抓回来的
            // 「正文」就是用户名，落档成 title=summary=author=用户名、评分全空
            // （实测两周 1,185 条评论无一条有正文）。
            // 改成：候选里挑**文本最长**的那个，并排除作者块本身及其祖先容器。
            const contentCandidates = Array.from(el.querySelectorAll(
                '[class*="content"], [class*="text"], [class*="body"], p'
            )).filter(c => !authorEl || !(c === authorEl || c.contains(authorEl)));
            let contentEl = null;
            let bestLen = 0;
            for (const c of contentCandidates) {
                const t = (c.innerText || c.textContent || '').trim();
                if (t.length > bestLen) { bestLen = t.length; contentEl = c; }
            }

            return {
                content: contentEl ? ((contentEl.innerText || contentEl.textContent || '').trim()) : '',
                time_str: timeEl
                    ? (timeEl.getAttribute('datetime') || ((timeEl.innerText || timeEl.textContent || '').trim()))
                    : '',
                likes: likeEl ? ((likeEl.innerText || likeEl.textContent || '').trim()) : '0',
                author: authorEl ? ((authorEl.innerText || authorEl.textContent || '').trim()) : '',
                score: starEl ? ((starEl.innerText || starEl.textContent || '').trim()) : '',
                url: linkEl ? linkEl.href : '',
            };
        });
    }""")

    items = []
    seen_urls = set()
    dropped_author_echo = 0
    for r in result or []:
        content_text = r.get("content", "").strip()
        if not content_text:
            continue
        author = r.get("author", "").strip()
        # 正文与用户名雷同 = 选择器又抓到了头部块，不是评论正文。宁可这轮空手
        # 触发响亮失败，也不要把一堆用户名当评论灌进归档（那批数据无法回溯识别，
        # 只能靠人肉发现「1,185 条评论全是用户名」）。
        if author and content_text == author:
            dropped_author_echo += 1
            continue
        url = r.get("url", "")
        # 同一条评论在 DOM 里可能被多个卡片容器命中；按评论链接收敛。
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        created_str = _parse_taptap_dom_time(r.get("time_str", ""))
        score = _parse_taptap_dom_score(r.get("score", ""))
        star_str = f"{'★' * score}{'☆' * (5 - score)} " if score else ""
        item = {
            "title": f"{star_str}{content_text[:60]}".strip(),
            "summary": content_text,
            "like_count": _parse_num(r.get("likes")),
            "comment_count": 0,
            "created": created_str,
            "url": url,
            "author": author,
            "item_id": "",
        }
        if score:
            item["score"] = score
        if not r.get("time_str"):
            item["time_is_approximate"] = True
        items.append(item)

    if dropped_author_echo:
        logger.warning(
            f"TapTap DOM: {dropped_author_echo} 条正文与用户名雷同已丢弃"
            f"（正文选择器疑似又抓到卡片头部块，请复查 debug_taptap_review.html）"
        )

    if not items:
        logger.warning("TapTap: DOM extraction found no review elements")
    return items


# ─── 增量过滤（纯逻辑，可独立测试）─────────────────────────────

def _raw_to_item(raw: dict, source: str, cutoff: datetime) -> dict | None:
    """把一条 raw 条目转为 aggregator 兼容字典；早于 cutoff 返回 None。

    从原 collect() 内部闭包提升为模块级纯函数，便于单测（对外行为不变）。
    """
    try:
        created = datetime.fromisoformat(raw["created"])
    except Exception:
        created = datetime.now(UTC)
    # DOM 兜底路径（API 拦截落空时才走）拿到的是页面 <time datetime="..."> 原文，
    # 可以是**不带时区**的 "2026-07-28 12:00:00"；朴素 datetime 与带时区的 cutoff
    # 相比会抛 TypeError，而该异常在 collect() 外层被 except 吞成一条 warning ——
    # 整批帖子/评价一条不剩，且对外表现为「今天 TapTap 没内容」。
    # 缺时区按**北京墙钟**（UTC+8）解释：taptap.cn 是国服站点，页面给的就是北京时间。
    # 原按 UTC 解释等于把时间戳整体推后 8 小时——北京 16:00–23:59 的帖会被推进次日，
    # 归档按北京日分桶（archive_layout）时整批落错一天的桶（每天错 8 小时之久）。
    # API 路径的 created 由 epoch 造出、自带 +00:00，不受本分支影响。
    time_str = raw["created"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=_BEIJING_TZ)
        # 补齐时区后必须把带偏移的形态**发下去**：下游 archive_platforms.item_date_utc8
        # 对无时区串一律按 UTC 折算北京日，光在本函数内补时区只修了 cutoff 比较，
        # 归档桶仍然错。仅在原串确实无时区时替换，坏时间戳照旧原样落校验层拒绝。
        time_str = created.isoformat()
    if created < cutoff:
        return None

    like_count = raw.get("like_count", 0)
    comment_count = raw.get("comment_count", 0)
    engagement = like_count + comment_count

    return {
        "title": raw.get("title", ""),
        "summary": raw.get("summary", ""),
        "source": source,
        "platform_region": "cn",
        "lang": "zh",
        "time": time_str,
        "url": raw.get("url", ""),
        "engagement": engagement,
        "is_hot": like_count > 50,
        "author": raw.get("author", ""),
        "tags": [],
        "content_type": "text",
        "media_url": "",
    }


def _filter_incremental(
    raw_items: list[dict],
    source: str,
    cutoff: datetime,
    last_id: str,
    backfill: bool,
) -> tuple[list[dict], str]:
    """增量过滤一批 raw 条目，返回 (转换后条目列表, 本次最新 item_id)。

    - 非回溯模式遇到 last_id 即短路停止（已处理到上次位置）。
    - 回溯模式不短路，仅靠 cutoff 控深度。
    - new_last_id 取**本批**第一条带 id 的条目（即本轮见到的最新一条）；本批一条
      带 id 的都没有（含首条即命中 last_id 而短路）时才沿用旧 last_id。
      原实现把 new_last_id 初始化为 last_id 后只在其为空时赋值 —— 游标一旦写过
      就永远冻在首轮那条上：轻则每轮把它之后的条目全部重采一遍，重则该锚点若是
      长期置顶帖（topic?type=official 常有），此后每轮在第 0 条就 break，帖子/评价
      恒为 0 条且不报任何错。
    """
    items: list[dict] = []
    batch_first_id = ""
    for raw in raw_items:
        item_id = raw.get("item_id", "")
        if not backfill and item_id and item_id == last_id:
            break
        if item_id and not batch_first_id:
            batch_first_id = item_id
        item = _raw_to_item(raw, source, cutoff)
        if item:
            items.append(item)
    return items, (batch_first_id or last_id)


# ─── 主入口 ───────────────────────────────────────────────────

async def collect(
    cutoff: datetime | None = None,
    max_scrolls: int = 5,
    backfill: bool = False,
) -> tuple[list[dict], list[dict]]:
    """
    启动 headless Chromium，采集帖子和评价。

    参数:
      cutoff:      只保留晚于此时刻的条目；None 时默认近 24 小时（日常增量）。
      max_scrolls: 每页下滑触发懒加载的最大轮次；回溯时调大以抓取更深历史。
      backfill:    回溯模式。绕过「遇到上次最新 ID 即停」的增量短路，
                   仅用 cutoff 控制时间深度，便于一次性补齐历史。

    返回 (topic_items, review_items)，每条均为 collector._make_item() 兼容的字典。
    """
    from playwright.async_api import async_playwright

    if cutoff is None:
        cutoff = datetime.now(UTC) - timedelta(hours=24)

    topic_raw: list[dict] = []
    review_raw: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )

        try:
            # 帖子页
            page = await context.new_page()
            try:
                topic_raw = await _extract_topics(page, max_scrolls, cutoff)
            except Exception as e:
                logger.warning(f"TapTap topics extraction failed: {e}")
            finally:
                await page.close()

            # 评价页
            page2 = await context.new_page()
            try:
                review_raw = await _extract_reviews(page2, max_scrolls, cutoff)
            except Exception as e:
                logger.warning(f"TapTap reviews extraction failed: {e}")
            finally:
                await page2.close()

        finally:
            await context.close()
            await browser.close()

    # ── 增量过滤 ──────────────────────────────────────────────
    state = _load_state()
    taptap_state = state.get("taptap", {})
    last_post_id = taptap_state.get("last_post_id", "")
    last_review_id = taptap_state.get("last_review_id", "")

    topic_items, new_last_post_id = _filter_incremental(
        topic_raw, "taptap_post", cutoff, last_post_id, backfill
    )
    review_items, new_last_review_id = _filter_incremental(
        review_raw, "taptap_review", cutoff, last_review_id, backfill
    )

    # ── 持久化状态 ────────────────────────────────────────────
    taptap_state.update({
        "last_post_id": new_last_post_id,
        "last_review_id": new_last_review_id,
        "last_run": datetime.now(UTC).isoformat(),
    })
    state["taptap"] = taptap_state
    _save_state(state)

    logger.info(
        f"TapTap collect done: {len(topic_items)} posts, {len(review_items)} reviews"
    )
    return topic_items, review_items


# ─── 独立运行入口 ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    async def main():
        topics, reviews = await collect()
        all_items = topics + reviews

        output_path = DATA_DIR / "test_output_taptap.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n{len(topics)} posts + {len(reviews)} reviews → {output_path}")
        if all_items:
            print("\n--- 示例（前3条）---")
            for item in all_items[:3]:
                print(json.dumps(item, ensure_ascii=False, indent=2))
        return all_items

    result = asyncio.run(main())
    sys.exit(0 if result else 1)
