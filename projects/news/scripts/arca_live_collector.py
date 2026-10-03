"""arca.live 忘却前夜频道（/b/forgettingeve）采集。

守密人 2026-10-03 裁定重启（推翻 2026-09-30 删除裁定）；旧采集器源码不可恢复，本模块重写。
容器机房 IP 实测：带桌面浏览器 UA 的 GET 返回 200，不带 UA 返回 403，故必须带 UA。
两步：① 列表页取帖（标题 / 链接 / 时间 / 作者 / 浏览 / 推荐 / 评论）；
② 对时窗内非公告帖按链接抓首帖正文写 summary（去 HTML、截 1000 字）。

稳健：请求间隔、每轮补抓上限、连续失败熔断、挑战页识别降级。永不因正文失败而丢列表结果；
列表页失败 / 挑战页返回空列表（一个源失败不拖垮整轮）。
去重：url 规范为 https://arca.live/b/forgettingeve/<id>（去 ?p= 等会变的查询参数），
collect_global.dedup_key 与 archive_platforms.item_key 都是 URL 优先，故键稳定。
"""
import argparse
import html as _html
import json
import logging
import re
import time
from datetime import datetime, timedelta, UTC
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

import requests
import news_common

logger = logging.getLogger(__name__)

ARCA_BOARD = "forgettingeve"
ARCA_BASE = "https://arca.live"
ARCA_LIST_URL = f"{ARCA_BASE}/b/{ARCA_BOARD}"
ARCA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ko,en;q=0.8",
}
ARCA_REQUEST_TIMEOUT = 15
ARCA_BODY_MAX_PER_RUN = 15      # 每轮最多补抓帖数
ARCA_BODY_DELAY_S = 1.5         # 请求间隔（秒）
ARCA_BODY_MAX_CHARS = 1000      # summary 截断长度
ARCA_BODY_MAX_CONSEC_FAIL = 3   # 连续失败达此数即停止补抓（熔断）
ARCA_FALLBACK_HOURS = 24

_POST_HREF = re.compile(r"/b/" + ARCA_BOARD + r"/(\d+)")
# 真挑战页特征（页面里的 recaptcha / turnstile 站点配置是常态，不算）
_CHALLENGE_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "attention required",
                      "enable javascript and cookies", "cf-browser-verification",
                      "checking your browser")


class ArcaChallenge(Exception):
    """收到挑战页 / 拦截页（非预期内容），调用方据此降级。"""


def canonical_url(post_id):
    return f"{ARCA_BASE}/b/{ARCA_BOARD}/{post_id}"


def is_challenge_page(page_html, kind="list"):
    """判别挑战页：缺少应有内容标记（列表 vrow 行 / 帖子 article-content）且命中挑战特征或页面很短。"""
    text = page_html or ""
    marker = 'class="vrow' if kind == "list" else "article-content"
    if marker in text:
        return False
    low = text.lower()
    return len(text) < 20000 or any(m in low for m in _CHALLENGE_MARKERS)


def _fetch_page(url, kind, session=None):
    getter = session.get if session else requests.get
    resp = getter(url, headers=ARCA_HEADERS, timeout=ARCA_REQUEST_TIMEOUT)
    if resp.status_code in (403, 429, 503):
        raise ArcaChallenge(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    if is_challenge_page(resp.text, kind):
        raise ArcaChallenge("challenge page")
    return resp.text


def _to_int(s):
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else 0


def _text(fragment):
    return re.sub(r"\s+", " ", _html.unescape(news_common.strip_html(fragment or ""))).strip()


def parse_list(page_html):
    """解析列表页，返回 dict 列表；公告行（notice）标 is_notice。"""
    rows = []
    for m in re.finditer(r'<a class="vrow([^"]*)" href="([^"]+)"[^>]*>(.*?)(?=<a class="vrow|\Z)',
                         page_html or "", re.DOTALL):
        classes, href, body = m.group(1), m.group(2), m.group(3)
        if "head" in classes.split():
            continue
        pm = _POST_HREF.search(href)
        if not pm:
            continue
        t = re.search(r'<span class="title">(.*?)</span>\s*<span class="info">', body, re.DOTALL) \
            or re.search(r'<span class="title">(.*?)</span>', body, re.DOTALL)
        if t:
            title = _text(re.sub(r'<span class="media-icon[^"]*"></span>', "", t.group(1)))
        else:  # 公告行标题在 <b> 内
            tb = re.search(r'col-title">\s*<b>(.*?)</b>', body, re.DOTALL)
            title = _text(tb.group(1)) if tb else ""
        if not title:
            continue
        badge = re.search(r'<span class="badge[^"]*">([^<]*)</span>', body)
        author = re.search(r'<span class="user-info[^"]*">.*?data-filter="([^"]*)"', body, re.DOTALL)
        time_m = re.search(r'<time datetime="([^"]+)"', body)
        cc = re.search(r'comment-count">\s*\[?(\d+)', body)
        view = re.search(r'col-view">\s*([\d,]+)', body)
        rate = re.search(r'col-rate">\s*(-?[\d,]+)', body)
        rows.append({
            "id": pm.group(1),
            "url": canonical_url(pm.group(1)),
            "title": title,
            "tag": _text(badge.group(1)) if badge else "",
            "author_raw": _html.unescape(author.group(1)) if author else "",
            "time": time_m.group(1) if time_m else "",
            "comments": int(cc.group(1)) if cc else 0,
            "views": _to_int(view.group(1)) if view else 0,
            "rate": _to_int(rate.group(1)) if rate else 0,
            "is_notice": "notice" in classes.split(),
        })
    return rows


def split_author(raw):
    """列表页 data-filter：固定昵称为 "昵称"，流动用户为 "昵称#数字"。返回 (显示昵称, 流动尾号)。"""
    if "#" in raw:
        nick, _, tail = raw.rpartition("#")
        return nick, tail
    return raw, ""


class _ArticleExtractor(HTMLParser):
    """取帖子页首个 article-content 的纯文本，并记录 member-info 区作者链接。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.done = False
        self.parts = []
        self._skip = 0
        self.author_href = ""
        self._in_member = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if not self.depth and not self.done:
            if tag == "div" and "member-info" in classes:
                self._in_member = 1
                return
            if self._in_member:
                if tag == "div":
                    self._in_member += 1
                if tag == "a" and not self.author_href and (a.get("href") or "").startswith("/u/"):
                    self.author_href = a["href"]
            if tag == "div" and "article-content" in classes:
                self.depth = 1
            return
        if self.done or not self.depth:
            return
        if tag == "div":
            self.depth += 1
            self.parts.append("\n")
        elif tag in ("br", "p"):
            self.parts.append("\n")
        elif tag in ("script", "style"):
            self._skip += 1

    def handle_startendtag(self, tag, attrs):
        if self.depth and not self.done and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._in_member and not self.depth and tag == "div":
            self._in_member -= 1
        if self.done or not self.depth:
            return
        if tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.done = True
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self.depth and not self.done and not self._skip:
            self.parts.append(data)


def parse_article(page_html):
    """帖子页 -> (正文纯文本<=1000 字, 稳定作者 ID)。无正文 / 无固定账号返回空串。

    作者 ID：固定昵称用户在 member-info 里是 `/u/@<昵称>` 链接，取解码后的昵称；
    流动用户无链接，返回 ""（调用方回落列表页的 `昵称#尾号`）。"""
    ex = _ArticleExtractor()
    ex.feed(page_html or "")
    ex.close()
    lines = [re.sub(r"[ \t　\xa0]+", " ", ln).strip() for ln in "".join(ex.parts).split("\n")]
    body = "\n".join(ln for ln in lines if ln)[:ARCA_BODY_MAX_CHARS]
    author_id = unquote(ex.author_href[len("/u/"):]) if ex.author_href else ""
    return body, author_id


def _enrich_bodies(items, session=None):
    """就地给 items 补首帖正文与作者 ID；失败降级（summary 空 + metadata.body_fetch_failed）。永不抛错。"""
    fetched = consec_fail = 0
    for item in items:
        if fetched >= ARCA_BODY_MAX_PER_RUN or consec_fail >= ARCA_BODY_MAX_CONSEC_FAIL:
            break
        if item["metadata"].get("is_notice"):
            continue
        if fetched:
            time.sleep(ARCA_BODY_DELAY_S)
        fetched += 1
        try:
            body, author_id = parse_article(_fetch_page(item["url"], "article", session))
            item["summary"] = body
            if author_id:
                item["metadata"]["author_id"] = author_id
            consec_fail = 0
        except ArcaChallenge as e:
            consec_fail += 1
            item["metadata"]["body_fetch_failed"] = True
            item["metadata"]["challenge"] = True
            logger.debug(f"arca_live body challenge {item['url']}: {e}")
        except Exception as e:
            consec_fail += 1
            item["metadata"]["body_fetch_failed"] = True
            logger.debug(f"arca_live body fetch failed {item['url']}: {e}")
    logger.info(f"arca_live bodies: requested {fetched}")


def build_items(rows, cutoff):
    """列表行 -> 标准 item（时窗过滤；公告标注 is_notice 且不进补抓）。"""
    items = []
    seen = set()
    for r in rows:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        try:
            created = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        except ValueError:
            continue  # 无法解析发布时间的行丢弃，不用 now() 冒充
        if created < cutoff:
            continue
        nick, tail = split_author(r["author_raw"])
        md = {"views": r["views"], "likes": r["rate"], "comments": r["comments"]}
        if r["is_notice"]:
            md["is_notice"] = True
        if tail:
            md["author_id"] = r["author_raw"]  # 流动用户：昵称#尾号（帖子页有固定账号时会被覆盖）
        item = news_common.make_item(
            title=r["title"], summary="", source="arca_live", platform_region="kr",
            time_str=created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            url=r["url"], engagement=r["views"] + r["rate"] + r["comments"],
            is_hot=r["views"] >= 1000, author=nick,
            tags=[r["tag"]] if r["tag"] else [], lang="ko",
        )
        item["metadata"] = md
        items.append(item)
    return items


def fetch_arca_live(cutoff=None, session=None):
    """采集 arca.live 忘却前夜频道。列表失败 / 挑战页返回 []，正文失败降级，永不抛错。"""
    if cutoff is None:
        try:
            import global_collectors
            cutoff = global_collectors.CUTOFF
        except Exception:
            cutoff = datetime.now(UTC) - timedelta(hours=ARCA_FALLBACK_HOURS)
    try:
        rows = parse_list(_fetch_page(ARCA_LIST_URL, "list", session))
    except ArcaChallenge as e:
        logger.warning(f"arca_live 列表页被拦或为挑战页，本轮降级为空：{e}")
        return []
    except Exception as e:
        logger.warning(f"arca_live 列表页失败：{e}")
        return []
    items = build_items(rows, cutoff)
    logger.info(f"arca_live: {len(rows)} rows, {len(items)} in window")
    try:
        _enrich_bodies(items, session)
    except Exception as e:  # 双保险
        logger.warning(f"arca_live body enrichment aborted: {e}")
    return items


# ── 收件箱通道 CLI（守密人 2026-10-03「方案 A」，T111）────────────────────────
# GitHub Actions 机房 IP 被 arca.live 拦（403），Claude 云会话容器带浏览器 UA 可访问：
# 每日云会话例程在容器里跑本 CLI，把结果 JSON 提交到 inbox/arca 分支，
# 由 arca-inbox-ingest 工作流经 ingest_inbox.py 入湖。CLI 只写 --out，不碰 Record/。
DEFAULT_HOURS = 36


def build_payload(items, collected_at, blocked=False):
    """收件 JSON 结构：{source, collected_at, items[, blocked]}；被拦时 items 为空并带 blocked 标记。"""
    payload = {"source": "arca_live", "collected_at": collected_at, "items": items}
    if blocked:
        payload["blocked"] = True
    return payload


def run_cli(hours, out, session=None):
    """采集并写 out；被拦 / 失败 / 零条也写文件（被拦带 blocked 标记）。返回 payload。"""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    blocked = False
    try:
        # 与 fetch_arca_live 同一套步骤（列表 -> build_items -> 补正文）；
        # 区别只在被拦时记 blocked 标记，以区分「窗内确实零条」。
        rows = parse_list(_fetch_page(ARCA_LIST_URL, "list", session))
        items = build_items(rows, cutoff)
        try:
            _enrich_bodies(items, session)
        except Exception as e:  # 双保险
            logger.warning(f"arca_live body enrichment aborted: {e}")
    except Exception as e:  # ArcaChallenge / 网络错误：降级为空并标记
        logger.warning(f"arca_live 列表页被拦或失败，写空收件文件：{e}")
        items, blocked = [], True
    payload = build_payload(items, now.strftime("%Y-%m-%dT%H:%M:%SZ"), blocked)
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def main(argv=None):
    ap = argparse.ArgumentParser(description="arca.live 采集 CLI：结果写收件 JSON（不写 Record/）")
    ap.add_argument("--hours", type=float, default=DEFAULT_HOURS, help="回看小时数（默认 36）")
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    payload = run_cli(args.hours, args.out)
    print(f"arca_live: {len(payload['items'])} 条"
          f"{'（被拦，已写空文件）' if payload.get('blocked') else ''} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
