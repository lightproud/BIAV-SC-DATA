#!/usr/bin/env python3
"""YouTube 视频评论采集器（累积归档版）。

守密人要求：每天采集忘却前夜相关视频的**所有新评论**，并**归档旧评论**（不丢历史）。
做法：维护一个去重累积库，每次运行增量补新 + 逐步回填旧。

存储（Public-Info-Pool/Record/Community/youtube_comments/，2026-07-02 对齐 BPT 4R
数据根——此前仍写迁移前旧路径 projects/news/data/platforms/，导致权威档案 6-20 后断更）：
  comments.jsonl   累积库，一行一条评论，按 comment id 去重、只增不删
  state.json       每视频分页状态 {video_id: {title, channel, exhausted, next_page}}
  {date}.json      **该日发布**的评论日档（供当日报告引用「PV 视频评论」）。守密人
                   2026-08-08 裁定改为按发布日分档，取代原「当轮快照」语义——原语义下
                   本档记的是「哪一轮采到的」而非「评论发布于哪天」，断更后补采时整段
                   积压会全挤进一个日期档、其余日期成空档。--date 自此降为**回落标签**，
                   只用于 published 缺失 / 不可解析的评论。

候选视频来源：YouTube 搜索（Morimens/忘却前夜…）+ 复用已归档的 youtube 视频 ID
（Record/Community/youtube/ 全布局递归，含区服/类型子目录）。每视频按 order=time 分页：
  - 增量：翻到整页都已在库即停（已追上最新）。
  - 回填：每视频每次最多翻 --max-pages 页，未尽则 state 标记 exhausted=false 下次续。

楼中楼（2026-10-03）：commentThreads 用 part=snippet,replies，每个线程随带最多 5 条回复；
totalReplyCount 超出已知回复数时用 comments.list?parentId= 分页补全。回复一条一行，
与一级评论同构，另带 parent_id、is_reply=true；去重键仍是 id（回复 id 形如 "<线程id>.<回复id>"，稳定）。

热门发现：除按日期搜索外，补 order=viewCount 的热门搜索；"近 REFRESH_DAYS 天有新评论的旧视频"
每轮多扫 SWEEP_PAGES 页（抓旧线程下的新回复）。

配额（Data API 每日 1 万单位）：search 100/次，commentThreads / comments.list 1/次。
每轮带预算上限 RUN_QUOTA_BUDGET，开跑前打印估算、收尾打印实际消耗；超预算时按
「新视频 > 近期活跃视频 > 其余」排序处理，且预算紧张（余量 < REPLY_RESERVE）时先停
comments.list 补全，把配额留给新视频与一级评论。

需 YOUTUBE_API_KEY（Google Data API，免费配额）。无 key 时仍建目录并退出，便于工作流容错。

用法：
  YOUTUBE_API_KEY=xxx python projects/news/scripts/collect_video_comments.py --date 2026-06-03
"""
import os
import sys
import json
import glob
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime, UTC

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_layout  # noqa: E402  归档布局单一真相源（分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认）
import news_common  # noqa: E402  原子 JSON 写单一真源（dump_json_atomic）

API = "https://www.googleapis.com/youtube/v3"
# 分仓桥接：youtube_comments 写根 + youtube 读 glob 均随 community_root() 换位（data 仓 / 在树默认）
DEST = str(archive_layout.community_root() / "youtube_comments")
YT_ARCHIVE_GLOB = str(archive_layout.community_root() / "youtube" / "**" / "*.json")
SEARCH_Q = ["Morimens", "忘却前夜", "Morimens Saya no Uta", "忘却前夜 沙耶"]
HOT_Q = ["Morimens", "忘却前夜"]       # 热门发现词（order=viewCount），只取主词省配额
SEARCH_MAX_RESULTS = 25

# ── 配额（Data API 每日 10,000 单位）──
DAILY_QUOTA = 10000
RUN_QUOTA_BUDGET = 8000       # 本轮上限：为同日手动补跑 / 其他用途留 20% 余量
SEARCH_COST = 100
LIST_COST = 1                 # commentThreads.list / comments.list
REPLY_RESERVE = 300           # 余量低于此值即停 comments.list 补全，配额留给新视频与一级评论
MAX_VIDEOS_PER_RUN = 700      # 每轮处理视频数上限（按优先级截断）
MAX_REPLY_PAGES = 5           # 单线程 comments.list 最多翻页（每页 100 条）
REFRESH_DAYS = 14             # 近 N 天有新评论即视为「近期活跃」
SWEEP_PAGES = 3               # 近期活跃视频至少翻的页数（抓旧线程下的新回复）


class BudgetExceeded(Exception):
    """本轮预算用尽或 API 返回 quotaExceeded。"""


class Quota:
    """本轮配额计量器。limit=None 表示不设限（仅计数）。"""

    def __init__(self, limit=RUN_QUOTA_BUDGET):
        self.limit = limit
        self.spent = 0
        self.hit_api_limit = False

    @property
    def remaining(self):
        return float("inf") if self.limit is None else self.limit - self.spent

    def charge(self, cost):
        if self.hit_api_limit or cost > self.remaining:
            raise BudgetExceeded()
        self.spent += cost

    def allow_extra(self):
        """comments.list 补全是否还允许（预算紧张时让位）。"""
        return not self.hit_api_limit and self.remaining > REPLY_RESERVE


def estimate_quota(n_search, n_videos, n_new, n_recent, max_pages):
    """本轮配额估算 -> (典型, 上界)，不含 comments.list 补全（其量取决于回复缺口）。

    典型：搜索 + 每视频 1 页；新视频按 max_pages；近期活跃按 SWEEP_PAGES。
    上界：每视频都翻满 max_pages。
    """
    base = n_search * SEARCH_COST
    n_old = max(n_videos - n_new - n_recent, 0)
    typical = base + (n_new * max_pages + n_recent * SWEEP_PAGES + n_old) * LIST_COST
    upper = base + n_videos * max_pages * LIST_COST
    return typical, upper


def _call(path, params, quota=None):
    """带计量的 API 调用；quotaExceeded 转 BudgetExceeded。"""
    if quota is not None:
        quota.charge(SEARCH_COST if path == "search" else LIST_COST)
    try:
        return _get(path, params)
    except urllib.error.HTTPError as e:
        if _is_quota_error(e):
            if quota is not None:
                quota.hit_api_limit = True
            raise BudgetExceeded() from e
        raise


def _is_quota_error(e):
    try:
        body = e.read().decode("utf-8", "replace") if getattr(e, "fp", None) else ""
    except Exception:
        body = ""
    return "quotaExceeded" in body or "dailyLimitExceeded" in body


def _get(path, params):
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "silver-core/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def discover_videos(key, quota=None):
    """搜索（按日期 + 热门）+ 复用归档的 youtube 视频 ID -> {vid: (title, channel)}。

    热门一路（order=viewCount）与按日期一路按 video id 去重（先到先得）。
    """
    vids = {}
    plans = [(q, "date") for q in SEARCH_Q] + [(q, "viewCount") for q in HOT_Q]
    for q, order in plans:
        try:
            for it in _call("search", {"part": "snippet", "q": q, "type": "video",
                                       "order": order, "maxResults": SEARCH_MAX_RESULTS,
                                       "key": key}, quota).get("items", []):
                vid = it.get("id", {}).get("videoId")
                if vid and vid not in vids:
                    sn = it.get("snippet", {})
                    vids[vid] = (sn.get("title", ""), sn.get("channelTitle", ""))
        except BudgetExceeded:
            print("  搜索阶段预算用尽，停止发现")
            break
        except Exception as e:
            print(f"  search '{q}'({order}) 失败: {type(e).__name__}")
    # 复用已归档 youtube 视频 URL 里的 video id（递归覆盖区服/类型分层）
    for fp in glob.glob(YT_ARCHIVE_GLOB, recursive=True):
        try:
            with open(fp, encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for it in (items if isinstance(items, list) else []):
            u = it.get("url", "")
            if "watch?v=" in u:
                vid = u.split("watch?v=")[1][:11]
                vids.setdefault(vid, (it.get("title", ""), it.get("author", "")))
    return vids


def _row(cid, vid, c, parent_id=None):
    row = {"id": cid, "video_id": vid,
           "author": c.get("authorDisplayName", ""),
           "text": (c.get("textDisplay") or "")[:1000],
           "likes": c.get("likeCount", 0),
           "published": c.get("publishedAt", ""),
           "fetched_at": datetime.now(UTC).isoformat()}
    if parent_id:
        row["parent_id"] = parent_id
        row["is_reply"] = True
    return row


def _fetch_replies(key, vid, parent_id, known_ids, quota):
    """comments.list?parentId= 分页补全一个线程的回复 -> 新回复行列表。"""
    out, token = [], None
    for _ in range(MAX_REPLY_PAGES):
        params = {"part": "snippet", "parentId": parent_id, "maxResults": 100,
                  "textFormat": "plainText", "key": key}
        if token:
            params["pageToken"] = token
        try:
            data = _call("comments", params, quota)
        except urllib.error.HTTPError:
            break  # 线程被删 / 不可用：放弃补全，不影响其他线程
        for rc in data.get("items", []):
            rid = rc.get("id")
            if rid and rid not in known_ids:
                known_ids.add(rid)
                out.append(_row(rid, vid, rc.get("snippet", {}), parent_id))
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def fetch_video_comments(key, vid, known_ids, max_pages, quota=None,
                         known_reply_counts=None, sweep_pages=1):
    """按时间序分页拉评论 + 楼中楼；返回 (new_rows, exhausted)。

    - 增量：翻满 sweep_pages 页后，遇整页无新内容即停（已追上最新）。
    - 楼中楼：线程 totalReplyCount > 已知回复数 -> 先收随带回复，仍不足则 comments.list 补全。
    - known_reply_counts: {thread_id: 已入库回复数}（调用方由 comments.jsonl 构建，本函数就地更新）。
    - 预算用尽 -> 返回已采到的行，exhausted=False（下次续）。
    """
    if known_reply_counts is None:
        known_reply_counts = {}
    new_rows, page, token = [], 0, None
    while page < max_pages:
        params = {"part": "snippet,replies", "videoId": vid, "order": "time",
                  "maxResults": 100, "textFormat": "plainText", "key": key}
        if token:
            params["pageToken"] = token
        try:
            data = _call("commentThreads", params, quota)
        except BudgetExceeded:
            return new_rows, False
        except urllib.error.HTTPError:
            return new_rows, True  # 评论关闭/不可用 -> 视为已尽
        page += 1
        page_new = 0
        for t in data.get("items", []):
            cid = t.get("id")
            if not cid:
                continue
            sn = t.get("snippet", {})
            if cid not in known_ids:
                known_ids.add(cid)
                page_new += 1
                new_rows.append(_row(cid, vid, sn.get("topLevelComment", {}).get("snippet", {})))
            # 随带回复（最多 5 条）
            have = known_reply_counts.get(cid, 0)
            for rc in (t.get("replies") or {}).get("comments", []):
                rid = rc.get("id")
                if rid and rid not in known_ids:
                    known_ids.add(rid)
                    have += 1
                    page_new += 1
                    new_rows.append(_row(rid, vid, rc.get("snippet", {}), cid))
            total = sn.get("totalReplyCount", 0) or 0
            if total > have and (quota is None or quota.allow_extra()):
                try:
                    extra = _fetch_replies(key, vid, cid, known_ids, quota)
                except BudgetExceeded:
                    extra = []
                have += len(extra)
                page_new += len(extra)
                new_rows.extend(extra)
            known_reply_counts[cid] = have
        token = data.get("nextPageToken")
        if not token:            # 没有下一页 -> 该视频评论已尽
            return new_rows, True
        if page_new == 0 and page >= sweep_pages:   # 追上最新
            return new_rows, True
    return new_rows, False       # 还有更多，下次续（回填未尽）


def _is_recent(st, today):
    """state 里 last_new 距 today 不超过 REFRESH_DAYS 天。"""
    try:
        d = datetime.fromisoformat(st.get("last_new", "")).date()
        t = datetime.fromisoformat(today).date()
    except (ValueError, TypeError, AttributeError):
        return False
    return 0 <= (t - d).days <= REFRESH_DAYS


def prioritize(videos, state, today):
    """排序：新视频 > 近期活跃 > 其余（last_run 旧者先）；截断到 MAX_VIDEOS_PER_RUN。"""
    def rank(item):
        st = state.get(item[0])
        if st is None:
            return (0, "")
        return (1 if _is_recent(st, today) else 2, st.get("last_run", ""))
    return sorted(videos.items(), key=rank)[:MAX_VIDEOS_PER_RUN]


def snapshot_date(row: dict, fallback: str) -> str:
    """这条评论该落哪个日期档：按 published 折算北京日期；缺失/不可解析回落 fallback。

    日期基准一律经 archive_layout.archive_date_str（归档布局 SSOT）——YouTube 的
    publishedAt 已带 Z 偏移，手写 `+ timedelta(hours=8)` 会把偏移算两遍（该坑的原案
    见 archive_layout 日期基准注释）。
    """
    raw = (row.get("published") or "").strip()
    if raw:
        try:
            return archive_layout.archive_date_str(datetime.fromisoformat(raw))
        except ValueError:      # 非 ISO8601（字段缺省 / 上游改格式）→ 回落标签，不丢条目
            pass
    return fallback


def main():
    ap = argparse.ArgumentParser(description="YouTube 评论累积采集器")
    ap.add_argument("--date", required=True,
                    help="回落日期标签 YYYY-MM-DD（仅用于 published 缺失/不可解析的评论）")
    ap.add_argument("--max-pages", type=int, default=8, help="每视频每次最多翻页数（回填节流）")
    a = ap.parse_args()
    os.makedirs(DEST, exist_ok=True)   # 始终建目录，便于工作流容错

    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        print("YOUTUBE_API_KEY 未设置——跳过（CI 须配此 Secret 方能采评论）")
        return

    # 载入累积库 + 已知 id
    store = f"{DEST}/comments.jsonl"
    known = set()
    reply_counts: dict[str, int] = {}
    if Path(store).is_file():
        with open(store, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    known.add(r["id"])
                    if r.get("parent_id"):
                        reply_counts[r["parent_id"]] = reply_counts.get(r["parent_id"], 0) + 1
                except (json.JSONDecodeError, KeyError, TypeError, AttributeError):  # 坏行 / 缺 id
                    pass
    state = {}
    sp = f"{DEST}/state.json"
    if Path(sp).is_file():
        with open(sp, encoding="utf-8") as f:
            state = json.load(f)

    quota = Quota(RUN_QUOTA_BUDGET)
    videos = discover_videos(key, quota)
    plan = prioritize(videos, state, a.date)
    n_new = sum(1 for v, _ in plan if v not in state)
    n_recent = sum(1 for v, _ in plan if v in state and _is_recent(state[v], a.date))
    typical, upper = estimate_quota(len(SEARCH_Q) + len(HOT_Q), len(plan), n_new, n_recent, a.max_pages)
    print(f"候选视频 {len(videos)}（本轮处理 {len(plan)}：新 {n_new} / 近期活跃 {n_recent}）；"
          f"库内已有评论 {len(known)} 条")
    print(f"配额估算：典型 {typical} / 上界 {upper}（不含回复补全）；本轮预算 {RUN_QUOTA_BUDGET}，"
          f"发现阶段已用 {quota.spent}，日上限 {DAILY_QUOTA}")

    run_new = []
    with open(store, "a", encoding="utf-8") as f:
        for vid, (title, ch) in plan:
            if quota.hit_api_limit or quota.remaining < LIST_COST:
                print("  配额用尽，余下视频留待下轮")
                break
            old = state.get(vid, {})
            sweep = SWEEP_PAGES if _is_recent(old, a.date) else 1
            rows, exhausted = fetch_video_comments(key, vid, known, a.max_pages, quota,
                                                   reply_counts, sweep)
            for r in rows:
                r["video_title"] = title
                r["channel"] = ch
                r["video_url"] = f"https://www.youtube.com/watch?v={vid}"
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                run_new.append(r)
            state[vid] = {"title": title, "channel": ch, "exhausted": exhausted,
                          "last_run": a.date,
                          "last_new": a.date if rows else old.get("last_new", "")}
    # 原子写：state.json 是增量续采的唯一断点依据，直写 open('w') 若在写一半被中断
    # 便留下半截 JSON，下一轮 json.load 抛异常 => 整份状态丢失、全部视频从头重翻页
    # （YouTube API 有每日配额，重翻即烧配额）。
    news_common.dump_json_atomic(sp, state, indent=1)

    # 日档（按 likes 排序，供报告引用），按**评论发布日**分组落各自的档。
    # 与同日既有日档并轨后再写：run_new 只含**本轮新采**的评论，而累积库 comments.jsonl
    # 是跨轮去重的——同一天第二次跑（CI 重跑失败作业 / workflow_dispatch 补同一日期）
    # 时全部评论都已在库，run_new 恒为空，直写就把当日日档清成 `[]`，当日「PV 视频评论」
    # 报告数据凭空蒸发（累积库还在，日档层已毁）。分组后**空组不落笔**，本轮没采到新
    # 评论的日期一律不碰，上述清空路径遂在结构上不可达。
    by_date: dict[str, list] = {}
    for r in run_new:
        by_date.setdefault(snapshot_date(r, a.date), []).append(r)
    for day, rows in sorted(by_date.items()):
        snap_path = Path(f"{DEST}/{day}.json")
        merged = []
        seen_ids = set()
        if snap_path.is_file():
            try:
                prev = json.loads(snap_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prev = []
            for r in prev if isinstance(prev, list) else []:
                if isinstance(r, dict) and r.get("id") not in seen_ids:
                    seen_ids.add(r.get("id"))
                    merged.append(r)
        for r in rows:
            if r.get("id") not in seen_ids:
                seen_ids.add(r.get("id"))
                merged.append(r)
        merged.sort(key=lambda x: -x.get("likes", 0))
        news_common.dump_json_atomic(str(snap_path), merged, indent=1)
    run_new.sort(key=lambda x: -x.get("likes", 0))
    print(f"本次新增 {len(run_new)} 条，落 {len(by_date)} 个日档"
          f"（{min(by_date) if by_date else '-'} … {max(by_date) if by_date else '-'}）；"
          f"累积库共 {len(known)} 条 → {store}")
    n_rep = sum(1 for r in run_new if r.get("is_reply"))
    print(f"其中楼中楼回复 {n_rep} 条；本轮实际配额消耗 {quota.spent} / 预算 {RUN_QUOTA_BUDGET}")


if __name__ == "__main__":
    main()
