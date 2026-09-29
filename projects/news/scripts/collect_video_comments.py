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

需 YOUTUBE_API_KEY（Google Data API，免费配额：commentThreads 1 单位/100 条，
每日 1 万单位足够）。无 key 时仍建目录并退出，便于工作流容错。

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


def _get(path, params):
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "silver-core/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def discover_videos(key):
    """搜索 + 复用归档的 youtube 视频 ID → {vid: (title, channel)}。"""
    vids = {}
    for q in SEARCH_Q:
        try:
            for it in _get("search", {"part": "snippet", "q": q, "type": "video",
                                      "order": "date", "maxResults": 25, "key": key}).get("items", []):
                vid = it.get("id", {}).get("videoId")
                if vid:
                    sn = it.get("snippet", {})
                    vids[vid] = (sn.get("title", ""), sn.get("channelTitle", ""))
        except Exception as e:
            print(f"  search '{q}' 失败: {type(e).__name__}")
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


def fetch_video_comments(key, vid, known_ids, max_pages):
    """按时间序分页拉评论；遇整页已知即停（增量）；返回 (new_rows, exhausted)。"""
    new_rows, page, token = [], 0, None
    while page < max_pages:
        params = {"part": "snippet", "videoId": vid, "order": "time",
                  "maxResults": 100, "textFormat": "plainText", "key": key}
        if token:
            params["pageToken"] = token
        try:
            data = _get("commentThreads", params)
        except urllib.error.HTTPError:
            return new_rows, True  # 评论关闭/不可用 → 视为已尽
        page += 1
        page_new = 0
        for t in data.get("items", []):
            cid = t.get("id")
            c = t.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            if not cid or cid in known_ids:
                continue
            known_ids.add(cid)
            page_new += 1
            new_rows.append({"id": cid, "video_id": vid,
                             "author": c.get("authorDisplayName", ""),
                             "text": (c.get("textDisplay") or "")[:1000],
                             "likes": c.get("likeCount", 0),
                             "published": c.get("publishedAt", ""),
                             "fetched_at": datetime.now(UTC).isoformat()})
        token = data.get("nextPageToken")
        if page_new == 0:        # 整页都已知 → 已追上最新
            return new_rows, True
        if not token:            # 没有下一页 → 该视频评论已尽
            return new_rows, True
    return new_rows, False       # 还有更多，下次续（回填未尽）


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
    if Path(store).is_file():
        with open(store, encoding="utf-8") as f:
            for line in f:
                try:
                    known.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError, TypeError):  # 坏行 / 缺 id
                    pass
    state = {}
    sp = f"{DEST}/state.json"
    if Path(sp).is_file():
        with open(sp, encoding="utf-8") as f:
            state = json.load(f)

    videos = discover_videos(key)
    print(f"候选视频 {len(videos)}；库内已有评论 {len(known)} 条")

    run_new = []
    with open(store, "a", encoding="utf-8") as f:
        for vid, (title, ch) in videos.items():
            rows, exhausted = fetch_video_comments(key, vid, known, a.max_pages)
            for r in rows:
                r["video_title"] = title
                r["channel"] = ch
                r["video_url"] = f"https://www.youtube.com/watch?v={vid}"
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                run_new.append(r)
            state[vid] = {"title": title, "channel": ch, "exhausted": exhausted,
                          "last_run": a.date}
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


if __name__ == "__main__":
    main()
