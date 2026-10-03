#!/usr/bin/env python3
"""行为数据日快照（behavior-snapshot）：非发言类热度指标的每日时间序列。

2026-10-03 守密人批准。供舆情系统做「言行对照」——玩家「说了什么」之外，
还要看「做了什么」：社群人数、在线人数、评分与评价量、预约 / 粉丝、视频播放量、百科活跃度。
与 store_patrol.py 不重复：巡检盯的是 Steam 店面价格与评测总计的「变更签名」（变了才记），
本脚本记的是每天一个点的**数值时间序列**。

输出（本仓 Record/Behavior/，新目录）：
  Record/Behavior/<source>/<YYYY>.jsonl
每行一个 JSON：
  {"date": "<UTC+8 日期>", "ts": "<UTC ISO>", "source": ..., "target": ..., "metrics": {...}}
幂等：同一 (date, source, target) 重跑覆盖当天那一行，不重复追加；
steamcharts 回填行按 (source, target, ts) 去重（同一天有多个小时点）。

源（每源一个函数、互相隔离；一个失败只记 error，其余照常落盘；有失败则最后退出码 1）：
  discord / steam / taptap / appstore / google_play / youtube / fandom
一次性回填：--backfill-steamcharts 把 steamcharts chart-data.json 全部历史写入
  Record/Behavior/steamcharts/（按点的时间分年，粒度 month / day / hour 原样保留并标 granularity）。

每个请求带浏览器 UA、超时、失败重试一次、请求间隔 >= 1 秒（steamcharts 为第三方站，间隔 >= 3 秒）。

用法：
  python projects/news/scripts/behavior_snapshot.py                       # 正式一轮，写 Record/Behavior/
  python projects/news/scripts/behavior_snapshot.py --out /tmp/bh         # 写到别处（试跑）
  python projects/news/scripts/behavior_snapshot.py --dry-run             # 只拉取并打印，不写文件
  python projects/news/scripts/behavior_snapshot.py --only discord,fandom # 只跑指定源
  python projects/news/scripts/behavior_snapshot.py --backfill-steamcharts
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
DEFAULT_OUT = REPO_ROOT / "Record" / "Behavior"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_S = 25
MAX_ATTEMPTS = 2
RETRY_BASE_S = 2.0
MIN_INTERVAL_S = 1.0
STEAMCHARTS_INTERVAL_S = 3.0
UTC8 = timezone(timedelta(hours=8))

# ------------------------------------------------------------ 目标常量 ----
DISCORD_INVITES = ["morimens", "2hsGcAPcs9"]  # 全球服官方 / 日本版公式（YouTube 简介所载）
STEAM_APPS = {  # 区服 -> (appid, 评测语言列表)
    "global": ("3052450", ["schinese", "japanese", "english", "all"]),
    "jp": ("4226130", ["japanese", "schinese", "english", "all"]),
}
TAPTAP_APP_ID = "364992"
APPSTORE_APPS = {"global": ("6447354150", "us"), "jp": ("6743462069", "jp")}
GOOGLE_PLAY_APPS = {
    "global": ("com.qookkagames.z1.gp.hk", "en", "US"),
    "jp": ("jp.co.altplus.boukyakuzenya", "ja", "JP"),
}
YOUTUBE_CHANNEL_ID = "UCF6iFnr28T4KjmVvPakmU3g"  # 忘却前夜【日本版公式】
FANDOM_API = "https://morimens.fandom.com/api.php"


class SourceError(Exception):
    """某个源整体失败（无任何可用指标）。"""


# ---------------------------------------------------------------- 工具 ----
_last_request_at = 0.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _monotonic() -> float:
    return time.monotonic()


def fetch_text(url: str, min_interval: float = MIN_INTERVAL_S, lang: str = "en") -> str:
    """GET 文本：浏览器 UA + 超时 + 间隔限频 + 失败重试一次。测试里 monkeypatch 掉本函数。"""
    global _last_request_at
    last_err: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        wait = min_interval - (_monotonic() - _last_request_at)
        if wait > 0:
            _sleep(wait)
        _last_request_at = _monotonic()
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept-Language": lang}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_err = exc
            if attempt + 1 < MAX_ATTEMPTS:
                _sleep(RETRY_BASE_S)
    raise SourceError(f"GET {url} 失败：{last_err}")


def fetch_json(url: str, **kw: Any) -> Any:
    try:
        return json.loads(fetch_text(url, **kw))
    except json.JSONDecodeError as exc:
        raise SourceError(f"GET {url} 返回非 JSON：{exc}") from exc


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def date_utc8(moment: datetime) -> str:
    """快照日期一律取北京时间（UTC+8）。"""
    return moment.astimezone(UTC8).strftime("%Y-%m-%d")


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value: Any) -> int | float | None:
    """尽力取数字（字符串 / 数字都收）；取不到返回 None，绝不猜。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            f = float(value.strip().replace(",", ""))
        except ValueError:
            return None
        return int(f) if f.is_integer() and "." not in value else f
    return None


def _round_rating(value: Any) -> float | None:
    n = _num(value)
    return round(float(n), 4) if n is not None else None


Record = dict  # {"source", "target", "metrics", "warnings"?, "date"?, "ts"?, "key_ts"?}


def _rec(source: str, target: str, metrics: dict, warnings: list[str] | None = None) -> Record:
    return {"source": source, "target": target, "metrics": metrics, "warnings": warnings or []}


# ------------------------------------------------------------ 1. Discord ----
def collect_discord() -> list[Record]:
    out: list[Record] = []
    errors: list[str] = []
    for code in DISCORD_INVITES:
        try:
            d = fetch_json(f"https://discord.com/api/v10/invites/{code}?with_counts=true")
            members = _num(d.get("approximate_member_count"))
            presence = _num(d.get("approximate_presence_count"))
            if members is None and presence is None:
                raise SourceError("邀请响应缺 approximate_*_count")
            m: dict = {}
            if members is not None:
                m["member_count"] = members
            if presence is not None:
                m["presence_count"] = presence
            guild = d.get("guild") if isinstance(d.get("guild"), dict) else {}
            if guild.get("id"):
                m["guild_id"] = str(guild["id"])
            out.append(_rec("discord", code, m))
        except SourceError as exc:
            errors.append(f"{code}: {exc}")
    if not out:
        raise SourceError("; ".join(errors) or "discord: 无邀请码")
    if errors:
        out[0]["warnings"].extend(errors)
    return out


# --------------------------------------------------------------- 2. Steam ----
def _steam_history_yesterday(app_id: str, today_utc8: str) -> dict:
    d = fetch_json(f"https://store.steampowered.com/appreviewhistogram/{app_id}?l=english")
    recent = (d.get("results") or {}).get("recent") if isinstance(d, dict) else None
    if not isinstance(recent, list):
        raise SourceError("histogram 无 recent[]")
    yesterday = (datetime.strptime(today_utc8, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    for row in recent:
        if not isinstance(row, dict) or not isinstance(row.get("date"), int):
            continue
        # histogram 的日桶是 UTC 日（date 为 UTC 0 点）；取标签 == 北京时间昨日者，近似对齐。
        label = datetime.fromtimestamp(row["date"], timezone.utc).strftime("%Y-%m-%d")
        if label == yesterday:
            return {
                "yesterday_date": yesterday,
                "yesterday_up": _num(row.get("recommendations_up")),
                "yesterday_down": _num(row.get("recommendations_down")),
            }
    raise SourceError(f"histogram recent[] 无 {yesterday}")


def steamcharts_peak_24h(app_id: str, now: datetime) -> int | None:
    """steamcharts 最近 24 小时在线峰值；无页面 / 无点返回 None。"""
    try:
        data = json.loads(fetch_text(
            f"https://steamcharts.com/app/{app_id}/chart-data.json",
            min_interval=STEAMCHARTS_INTERVAL_S))
    except (SourceError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    cutoff = (now - timedelta(hours=24)).timestamp() * 1000
    vals = [p[1] for p in data
            if isinstance(p, list) and len(p) == 2 and isinstance(p[0], (int, float))
            and p[0] >= cutoff and _num(p[1]) is not None]
    return int(max(vals)) if vals else None


def collect_steam(now: datetime | None = None) -> list[Record]:
    now = now or now_utc()
    today = date_utc8(now)
    out: list[Record] = []
    for region, (app_id, langs) in STEAM_APPS.items():
        m: dict = {"app_id": app_id}
        warns: list[str] = []
        try:
            d = fetch_json(
                f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={app_id}")
            pc = _num(((d or {}).get("response") or {}).get("player_count"))
            if pc is None:
                raise SourceError("无 player_count")
            m["current_players"] = pc
        except SourceError as exc:
            warns.append(f"current_players: {exc}")
        peak = steamcharts_peak_24h(app_id, now)
        if peak is None:
            warns.append("peak_24h: steamcharts 无数据，跳过")
        else:
            m["peak_24h_steamcharts"] = peak
        try:
            m.update(_steam_history_yesterday(app_id, today))
        except SourceError as exc:
            warns.append(f"histogram: {exc}")
        reviews: dict = {}
        for lang in langs:
            try:
                # 注意：不带 filter=all&purchase_type=all 时 Steam 对 num_per_page=0 返回全 0。
                d = fetch_json(
                    f"https://store.steampowered.com/appreviews/{app_id}?json=1&num_per_page=0"
                    f"&language={lang}&filter=all&purchase_type=all")
                s = d.get("query_summary") if isinstance(d, dict) else None
                if not isinstance(s, dict) or _num(s.get("total_positive")) is None:
                    raise SourceError("无 query_summary")
                reviews[lang] = {
                    "total_positive": _num(s.get("total_positive")),
                    "total_negative": _num(s.get("total_negative")),
                }
            except SourceError as exc:
                warns.append(f"reviews[{lang}]: {exc}")
        if reviews:
            m["reviews"] = reviews
        if len(m) == 1:  # 只剩 app_id
            warns.append("steam 全部指标失败")
            raise SourceError(f"steam/{region}: " + "; ".join(warns))
        out.append(_rec("steam", region, m, warns))
    return out


# -------------------------------------------------------------- 3. TapTap ----
def _ld_json_blocks(html: str) -> list[Any]:
    blocks = []
    for m in re.finditer(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            blocks.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return blocks


def parse_ld_rating(html: str) -> tuple[Any, Any] | None:
    """从 JSON-LD 找第一个带 aggregateRating 的块，返回 (ratingValue, ratingCount)。"""
    for b in _ld_json_blocks(html):
        items = b if isinstance(b, list) else [b]
        for it in items:
            ar = it.get("aggregateRating") if isinstance(it, dict) else None
            if isinstance(ar, dict) and "ratingValue" in ar:
                return ar.get("ratingValue"), ar.get("ratingCount")
    return None


def parse_nuxt_stats(html: str) -> dict | None:
    """解 Nuxt devalue 序列化（__NUXT_DATA__ 平铺数组，对象值为下标）里含 fans_count 的统计对象。"""
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list):
        return None
    hits = [v for v in arr if isinstance(v, dict) and "fans_count" in v and "reserve_count" in v]
    if len(hits) != 1:  # 0 个或多个都算「不能可靠定位」
        return None
    out = {}
    for k in ("reserve_count", "fans_count", "wish_count", "review_count"):
        idx = hits[0].get(k)
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(arr):
            n = _num(arr[idx])
            if n is not None:
                out[k] = n
    return out


def collect_taptap() -> list[Record]:
    html = fetch_text(f"https://www.taptap.cn/app/{TAPTAP_APP_ID}", lang="zh-CN")
    warns: list[str] = []
    m: dict = {}
    rating = parse_ld_rating(html)
    if rating is None:
        warns.append("JSON-LD aggregateRating 缺失")
    else:
        rv, rc = _num(rating[0]), _num(rating[1])
        if rv is None:
            warns.append("ratingValue 缺失")
        else:
            m["rating_value"] = rv
        if rc is None:
            warns.append("ratingCount 缺失")
        else:
            m["rating_count"] = rc
    stats = parse_nuxt_stats(html)
    if stats is None:
        warns.append("Nuxt 统计对象（预约 / 粉丝）无法唯一定位，跳过")
    elif "rating_count" in m and stats.get("review_count") != m["rating_count"]:
        # 用 JSON-LD 的评价数交叉校验，防止取到别的对象。
        warns.append("Nuxt 统计对象 review_count 与 JSON-LD ratingCount 不符，预约 / 粉丝跳过")
    else:
        for src_key, dst in (("reserve_count", "reserve_count"), ("fans_count", "fans_count"),
                             ("wish_count", "wish_count")):
            if src_key in stats:
                m[dst] = stats[src_key]
            else:
                warns.append(f"Nuxt 缺 {src_key}")
    if not m:
        raise SourceError("taptap: " + "; ".join(warns))
    return [_rec("taptap", TAPTAP_APP_ID, m, warns)]


# ----------------------------------------------------------- 4. App Store ----
def collect_appstore() -> list[Record]:
    out: list[Record] = []
    errors: list[str] = []
    for region, (app_id, cc) in APPSTORE_APPS.items():
        try:
            d = fetch_json(f"https://itunes.apple.com/lookup?id={app_id}&country={cc}")
            res = (d.get("results") or []) if isinstance(d, dict) else []
            if not res or not isinstance(res[0], dict):
                raise SourceError("lookup 无结果")
            r = res[0]
            m = {"app_id": app_id, "country": cc}
            for src_key, dst in (("averageUserRating", "average_rating"),
                                 ("userRatingCount", "rating_count")):
                n = _num(r.get(src_key))
                if n is not None:
                    m[dst] = round(float(n), 4) if dst == "average_rating" else n
            for src_key, dst in (("version", "version"),
                                 ("currentVersionReleaseDate", "current_version_release_date")):
                if r.get(src_key):
                    m[dst] = r[src_key]
            if len(m) == 2:
                raise SourceError("lookup 无评分 / 版本字段")
            out.append(_rec("appstore", region, m))
        except SourceError as exc:
            errors.append(f"{region}: {exc}")
    if not out:
        raise SourceError("; ".join(errors))
    if errors:
        out[0]["warnings"].extend(errors)
    return out


# --------------------------------------------------------- 5. Google Play ----
def parse_google_play(html: str) -> dict:
    m: dict = {}
    rating = parse_ld_rating(html)
    if rating:
        rv, rc = _num(rating[0]), _num(rating[1])
        if rv is not None:
            m["rating_value"] = round(float(rv), 4)
        if rc is not None:
            m["rating_count"] = rc
    inst = re.search(r'class="ClM7O">([^<]+)</div><div class="g1rdde">[^<]*<', html)
    if inst:
        m["installs_tier"] = inst.group(1).strip()
    return m


def collect_google_play() -> list[Record]:
    out: list[Record] = []
    errors: list[str] = []
    for region, (pkg, hl, gl) in GOOGLE_PLAY_APPS.items():
        try:
            html = fetch_text(
                f"https://play.google.com/store/apps/details?id={pkg}&hl={hl}&gl={gl}", lang=hl)
            m = parse_google_play(html)
            warns = []
            if "rating_value" not in m:
                warns.append(f"{region}: JSON-LD 评分缺失")
            if "installs_tier" not in m:
                warns.append(f"{region}: 安装档位未找到")
            if not m:
                raise SourceError("页面无评分也无安装档位")
            m["package"] = pkg
            out.append(_rec("google_play", region, m, warns))
        except SourceError as exc:
            errors.append(f"{region}: {exc}")
    if not out:
        raise SourceError("; ".join(errors))
    if errors:
        out[0]["warnings"].extend(errors)
    return out


# ------------------------------------------------------------ 6. YouTube ----
_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def parse_youtube_feed(xml_text: str, limit: int = 15) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"RSS 解析失败：{exc}") from exc
    videos = []
    for e in root.findall("a:entry", _NS)[:limit]:
        vid = e.findtext("yt:videoId", default="", namespaces=_NS)
        if not vid:
            continue
        stats = e.find("media:group/media:community/media:statistics", _NS)
        star = e.find("media:group/media:community/media:starRating", _NS)
        videos.append({
            "video_id": vid,
            "published": e.findtext("a:published", default=None, namespaces=_NS),
            "views": _num(stats.get("views")) if stats is not None else None,
            "star_count": _num(star.get("count")) if star is not None else None,
        })
    return videos


def collect_youtube() -> list[Record]:
    xml_text = fetch_text(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}")
    videos = parse_youtube_feed(xml_text)
    if not videos:
        raise SourceError("RSS 无视频条目")
    return [_rec("youtube", YOUTUBE_CHANNEL_ID, {"videos": videos})]


# ------------------------------------------------------------- 7. Fandom ----
def collect_fandom() -> list[Record]:
    d = fetch_json(f"{FANDOM_API}?action=query&meta=siteinfo&siprop=statistics&format=json")
    st = ((d.get("query") or {}).get("statistics") or {}) if isinstance(d, dict) else {}
    m = {k: _num(st.get(k)) for k in ("pages", "articles", "edits", "activeusers")}
    m = {k: v for k, v in m.items() if v is not None}
    if not m:
        raise SourceError("siteinfo 无 statistics")
    return [_rec("fandom", "morimens", m)]


SOURCES: dict[str, Callable[[], list[Record]]] = {
    "discord": collect_discord,
    "steam": collect_steam,
    "taptap": collect_taptap,
    "appstore": collect_appstore,
    "google_play": collect_google_play,
    "youtube": collect_youtube,
    "fandom": collect_fandom,
}


# ---------------------------------------------------------------- 落盘 ----
def _year_path(out_dir: Path, source: str, year: str) -> Path:
    return out_dir / source / f"{year}.jsonl"


def _row_key(row: dict) -> tuple:
    """幂等键：日快照 = (date, source, target)；steamcharts 点 = (ts, source, target)。"""
    if row.get("source") == "steamcharts":
        return (row.get("ts"), row.get("source"), row.get("target"))
    return (row.get("date"), row.get("source"), row.get("target"))


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 坏行不卡住后续每一天
    return rows


def upsert_rows(out_dir: Path, rows: list[dict]) -> int:
    """按幂等键覆盖写入；同一键已有则替换原位置，没有则追加。返回写入的行数。"""
    by_file: dict[Path, list[dict]] = {}
    for r in rows:
        year = r["date"][:4]  # 分年以（UTC+8）date 为准，steamcharts 点同理
        by_file.setdefault(_year_path(out_dir, r["source"], year), []).append(r)
    written = 0
    for path, new_rows in by_file.items():
        existing = _read_rows(path)
        index = {_row_key(r): i for i, r in enumerate(existing)}
        for nr in new_rows:
            k = _row_key(nr)
            if k in index:
                existing[index[k]] = nr
            else:
                index[k] = len(existing)
                existing.append(nr)
            written += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in existing),
            encoding="utf-8")
        os.replace(tmp, path)
    return written


def run_snapshot(out_dir: Path, only: list[str] | None = None, dry_run: bool = False,
                 now: datetime | None = None) -> dict:
    """一轮快照：每源隔离。返回 {"rows": n, "errors": {...}, "warnings": [...]}。"""
    now = now or now_utc()
    date, ts = date_utc8(now), iso_utc(now)
    names = [n for n in SOURCES if not only or n in only]
    rows: list[dict] = []
    errors: dict[str, str] = {}
    warnings: list[str] = []
    for name in names:
        try:
            recs = SOURCES[name]()
        except Exception as exc:  # 隔离：任何异常都只记 error
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        for r in recs:
            warnings.extend(f"{name}/{r['target']}: {w}" for w in r.get("warnings", []))
            rows.append({"date": date, "ts": ts, "source": r["source"],
                         "target": r["target"], "metrics": r["metrics"]})
    if rows and not dry_run:
        upsert_rows(out_dir, rows)
    return {"rows": rows, "errors": errors, "warnings": warnings}


# -------------------------------------------------------------- 回填 ----
def _granularity(gap_hours: float) -> str:
    if gap_hours <= 3:
        return "hour"
    if gap_hours <= 36:
        return "day"
    return "month"


def steamcharts_rows(app_label: str, app_id: str, data: list) -> list[dict]:
    pts = [p for p in data if isinstance(p, list) and len(p) == 2
           and isinstance(p[0], (int, float)) and _num(p[1]) is not None]
    pts.sort(key=lambda p: p[0])
    rows = []
    for i, (t_ms, val) in enumerate(pts):
        gaps = []
        if i > 0:
            gaps.append((t_ms - pts[i - 1][0]) / 3600000)
        if i + 1 < len(pts):
            gaps.append((pts[i + 1][0] - t_ms) / 3600000)
        gran = _granularity(min(gaps)) if gaps else "hour"
        moment = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
        rows.append({
            "date": date_utc8(moment),
            "ts": iso_utc(moment),
            "source": "steamcharts",
            "target": app_label,
            "metrics": {"players": int(val), "granularity": gran, "app_id": app_id},
        })
    return rows


def backfill_steamcharts(out_dir: Path, dry_run: bool = False) -> dict:
    total = 0
    errors: dict[str, str] = {}
    for region, (app_id, _) in STEAM_APPS.items():
        try:
            data = json.loads(fetch_text(
                f"https://steamcharts.com/app/{app_id}/chart-data.json",
                min_interval=STEAMCHARTS_INTERVAL_S))
            if not isinstance(data, list):
                raise SourceError("chart-data 不是数组")
            rows = steamcharts_rows(region, app_id, data)
        except Exception as exc:
            errors[f"steamcharts/{region}"] = f"{type(exc).__name__}: {exc}"
            continue
        total += len(rows)
        if not dry_run:
            upsert_rows(out_dir, rows)
    return {"rows": total, "errors": errors}


# ---------------------------------------------------------------- 入口 ----
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出根目录（默认 Record/Behavior）")
    ap.add_argument("--dry-run", action="store_true", help="只拉取并打印，不写文件")
    ap.add_argument("--only", default="", help="逗号分隔的源名子集")
    ap.add_argument("--backfill-steamcharts", action="store_true",
                    help="一次性回填 steamcharts 全部历史（幂等）")
    args = ap.parse_args(argv)

    if args.backfill_steamcharts:
        res = backfill_steamcharts(args.out, dry_run=args.dry_run)
        print(f"steamcharts 回填：{res['rows']} 个点")
        for k, v in res["errors"].items():
            print(f"ERROR {k}: {v}", file=sys.stderr)
        return 1 if res["errors"] else 0

    only = [s for s in args.only.split(",") if s] or None
    unknown = set(only or []) - set(SOURCES)
    if unknown:
        print(f"未知源：{sorted(unknown)}；可选：{sorted(SOURCES)}", file=sys.stderr)
        return 2
    res = run_snapshot(args.out, only=only, dry_run=args.dry_run)
    for r in res["rows"]:
        print(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
    for w in res["warnings"]:
        print(f"WARNING {w}", file=sys.stderr)
    for k, v in res["errors"].items():
        print(f"ERROR {k}: {v}", file=sys.stderr)
    print(f"完成：{len(res['rows'])} 行，{len(res['errors'])} 个源失败", file=sys.stderr)
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
