"""普查统计：流式读全量，按平台累计；每个平台另抽样做字段清单。

内存边界：记录逐条流过，只在一个目录（频道 / 来源）×一天内排序；
去重状态按目录持有，换目录即清。作者与文本只以 64 位摘要留在内存里，不进任何输出。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import groupby

from yuqing.census import fields as F
from yuqing.lake.layout import Discovery, LakeFile
from yuqing.lake.reader import LakeReader

CENSUS_VER = "1"
SHORT_MAX_CHARS = 60  # 「短句」上限，与 clean.long_repost_min_chars 对齐


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def quantiles(hist: Counter, qs: tuple[float, ...] = (0.5, 0.9, 0.99)) -> dict[str, int]:
    """直方图上的分位数（最近秩法）。"""
    total = sum(hist.values())
    out: dict[str, int] = {}
    if not total:
        return {f"p{round(q * 100)}": 0 for q in qs}
    keys = sorted(hist)
    for q in qs:
        rank = max(1, math.ceil(q * total - 1e-9))
        acc = 0
        for k in keys:
            acc += hist[k]
            if acc >= rank:
                out[f"p{round(q * 100)}"] = k
                break
    return out


def ratio(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


@dataclass
class ChannelStats:
    messages: int = 0
    days: set = field(default_factory=set)
    gaps: Counter = field(default_factory=Counter)
    last_ts: float | None = None


@dataclass
class PlatformStats:
    files: int = 0
    formats: Counter = field(default_factory=Counter)
    messages: int = 0
    bad_records: int = 0
    kind: Counter = field(default_factory=Counter)
    by_month: Counter = field(default_factory=Counter)
    ts_missing: int = 0
    len_hist: Counter = field(default_factory=Counter)
    tokens: int = 0
    authors: set = field(default_factory=set)
    author_missing: int = 0
    empty: int = 0
    emoji_only: int = 0
    image_only: int = 0
    with_text: int = 0
    dup_same_author: int = 0
    dup_cross_short: int = 0
    script: Counter = field(default_factory=Counter)
    special_filled: Counter = field(default_factory=Counter)
    drops: Counter = field(default_factory=Counter)
    kept: int = 0
    kept_chat: int = 0
    segments: int = 0
    channels: dict = field(default_factory=lambda: defaultdict(ChannelStats))
    sample: list = field(default_factory=list)
    seen_for_sample: int = 0


class Census:
    def __init__(self, reader: LakeReader, params: dict, sample_n: int = 1000, seed: int = 1) -> None:
        self.reader = reader
        self.sample_n = sample_n
        self.seed = seed
        self.gap_s = int(params["unitize"]["gap_minutes"]) * 60
        self.max_msgs = int(params["unitize"]["max_msgs"])
        self.window_s = int(params["clean"]["same_author_window_hours"]) * 3600
        self.long_min = int(params["clean"]["long_repost_min_chars"])
        self.params = params
        self.platforms: dict[str, PlatformStats] = {}
        self._rngs: dict[str, random.Random] = {}

    # ── 抽样：每平台蓄水池抽样，只留键名、类型、是否有值，不留原文 ──
    def _sample(self, platform: str, ps: PlatformStats, rec: dict) -> None:
        rng = self._rngs.setdefault(platform, random.Random(f"{self.seed}:{platform}"))
        ps.seen_for_sample += 1
        shape = {k: (F.json_type(v), F.is_filled(v) or v is False or v == 0) for k, v in rec.items()}
        if len(ps.sample) < self.sample_n:
            ps.sample.append(shape)
            return
        j = rng.randrange(ps.seen_for_sample)
        if j < self.sample_n:
            ps.sample[j] = shape

    def run(self, found: Discovery) -> None:
        for (platform, _scope), group in groupby(found.files, key=lambda f: (f.platform, f.scope)):
            ps = self.platforms.setdefault(platform, PlatformStats())
            self._scope(ps, platform, list(group))

    def _scope(self, ps: PlatformStats, platform: str, files: list[LakeFile]) -> None:
        last_by_author_text: dict[int, float] = {}
        short_first_author: dict[int, int] = {}
        long_seen: set[int] = set()
        prev_ts: float | None = None
        seg_len = 0
        for _, day_files in groupby(files, key=lambda f: f.day):
            day_files = list(day_files)
            batch: list[tuple[float, int, dict, LakeFile]] = []
            for f in day_files:
                ps.files += 1
                ps.formats[f.fmt + (".gz" if f.gz else "")] += 1
                for no, rec in self.reader.records(f):
                    if rec.get("__bad__"):
                        ps.bad_records += 1
                        continue
                    dt = F.parse_ts(rec)
                    ts = dt.timestamp() if dt else None
                    batch.append((ts if ts is not None else float("inf"), no, rec, f))
            batch.sort(key=lambda x: (x[0], x[3].rel, x[1]))
            for ts_key, _no, rec, f in batch:
                ts = None if ts_key == float("inf") else ts_key
                kept = self._record(ps, platform, f, rec, ts, last_by_author_text, short_first_author, long_seen)
                if f.kind != "chat" or ts is None:
                    continue
                ch = ps.channels[f.scope]
                ch.messages += 1
                ch.days.add(F.day_cn(datetime.fromtimestamp(ts, tz=UTC)))
                if ch.last_ts is not None:
                    ch.gaps[max(0, int(ts - ch.last_ts))] += 1
                ch.last_ts = ts
                if kept:  # 试切：只算清洗后留下的消息
                    if prev_ts is None or ts - prev_ts > self.gap_s or seg_len >= self.max_msgs:
                        ps.segments += 1
                        seg_len = 0
                    seg_len += 1
                    prev_ts = ts
                    ps.kept_chat += 1

    def _record(self, ps, platform, f, rec, ts, last_by_author_text, short_first_author, long_seen) -> bool:
        ps.messages += 1
        ps.kind[f.kind] += 1
        self._sample(platform, ps, rec)
        if ts is None:
            ps.ts_missing += 1
            month = f.day[:7]
        else:
            month = F.day_cn(datetime.fromtimestamp(ts, tz=UTC))[:7]
        ps.by_month[month] += 1

        text = F.get_text(rec)
        stripped = text.strip()
        ps.len_hist[len(text)] += 1
        label, tokens = F.script_profile(text)
        ps.tokens += tokens
        author = F.get_author(rec)
        a_h = _h(author) if author is not None else None
        if a_h is None:
            ps.author_missing += 1
        else:
            ps.authors.add(a_h)
        for key, cands in F.SPECIAL_KEYS.items():
            v = F.first_filled(rec, cands)
            if key == "bot":
                v = True if F.is_bot(rec) else None
            if v is not None:
                ps.special_filled[key] += 1

        attach = F.has_attachment(rec)
        emoji_only = F.is_emoji_only(text)
        if not stripped:
            if attach:
                ps.image_only += 1
            else:
                ps.empty += 1
        else:
            ps.with_text += 1
            ps.script[label] += 1
            if emoji_only:
                ps.emoji_only += 1

        # 完全重复（同目录内）：同作者 24 小时内相同文字；不同作者的相同短句
        dup_same = False
        if stripped and a_h is not None:
            t_h = _h(stripped)
            k = _h(f"{a_h}:{t_h}")
            prev = last_by_author_text.get(k)
            if prev is not None and (ts is None or prev == float("-inf") or ts - prev <= self.window_s):
                dup_same = True
                ps.dup_same_author += 1
            last_by_author_text[k] = ts if ts is not None else float("-inf")
            if len(stripped) < SHORT_MAX_CHARS:
                first = short_first_author.get(t_h)
                if first is None:
                    short_first_author[t_h] = a_h
                elif first != a_h:
                    ps.dup_cross_short += 1

        # 清洗试算：按 T11 的顺序取第一个命中的原因
        reason = None
        if F.is_bot(rec):
            reason = "bot"
        elif F.is_system(rec):
            reason = "system"
        elif not stripped and not attach:
            reason = "empty"
        elif emoji_only:
            reason = "emoji_only"
        elif dup_same:
            reason = "dup_same_author"
        elif len(stripped) >= self.long_min:
            t_h = _h(stripped)
            if t_h in long_seen:
                reason = "dup_long_repost"
            long_seen.add(t_h)
        if reason:
            ps.drops[reason] += 1
            return False
        if not stripped and attach:
            ps.drops["image_only（保留不送标）"] += 1
            return False
        ps.kept += 1
        return True

    # ── 汇总成可落盘的 dict（只含统计与字段名） ──
    def summary(self, found: Discovery) -> dict:
        platforms = {}
        tot = Counter()
        for name in sorted(self.platforms):
            ps = self.platforms[name]
            platforms[name] = self._platform_summary(name, ps, found)
            tot["messages"] += ps.messages
            tot["chat"] += ps.kind["chat"]
            tot["kept"] += ps.kept
            tot["kept_chat"] += ps.kept_chat
            tot["segments"] += ps.segments
            tot["units"] += ps.segments + (ps.kept - ps.kept_chat)
            tot["tokens"] += ps.tokens
            tot["with_text"] += ps.with_text
        scripts = Counter()
        for ps in self.platforms.values():
            scripts.update(ps.script)
        totals = {
            "messages": tot["messages"],
            "chat_share": ratio(tot["chat"], tot["messages"]),
            "kept_after_clean": tot["kept"],
            "retention": ratio(tot["kept"], tot["messages"]),
            "trial_segments": tot["segments"],
            "trial_avg_segment_len": round(tot["kept_chat"] / tot["segments"], 2) if tot["segments"] else 0.0,
            "trial_units_total": tot["units"],
            "tokens_est_total": tot["tokens"],
            "script": dict(sorted(scripts.items())),
        }
        assumptions = [
            {"item": "原文总条数", "assumed": "约 8,000,000", "actual": f"{tot['messages']:,}"},
            {"item": "语言数", "assumed": "12 种", "actual": f"文字脚本 {len(scripts)} 类（粗分，非语种）"},
            {"item": "聊天类占比", "assumed": "方案未给数", "actual": f"{totals['chat_share']:.2%}"},
            {"item": "清洗后留存率", "assumed": "方案未给数", "actual": f"{totals['retention']:.2%}"},
            {
                "item": "按默认参数试切的平均段长",
                "assumed": "方案未给数",
                "actual": f"{totals['trial_avg_segment_len']} 条/段",
            },
            {
                "item": "单元总数（聊天段 + 评论帖子各一）",
                "assumed": "方案未给数",
                "actual": f"{tot['units']:,}",
            },
        ]
        return {
            "census_ver": CENSUS_VER,
            "reader": self.reader.source,
            "params": {
                "sample_per_platform": self.sample_n,
                "seed": self.seed,
                "unitize.gap_minutes": self.gap_s // 60,
                "unitize.max_msgs": self.max_msgs,
                "clean.same_author_window_hours": self.window_s // 3600,
                "clean.long_repost_min_chars": self.long_min,
                "short_text_max_chars": SHORT_MAX_CHARS,
            },
            "lake": {
                "files": len(found.files),
                "skipped": dict(sorted(found.skipped.items())),
            },
            "totals": totals,
            "assumptions_vs_actual": assumptions,
            "platforms": platforms,
        }

    def _platform_summary(self, name: str, ps: PlatformStats, found: Discovery) -> dict:
        n = ps.messages
        sample_n = len(ps.sample)
        field_stats: dict[str, dict] = {}
        for shape in ps.sample:
            for k, (typ, filled) in shape.items():
                st = field_stats.setdefault(k, {"types": set(), "present": 0, "non_null": 0})
                st["types"].add(typ)
                st["present"] += 1
                st["non_null"] += int(filled)
        fields_out = {
            k: {
                "types": sorted(v["types"]),
                "present": ratio(v["present"], sample_n),
                "non_null": ratio(v["non_null"], sample_n),
            }
            for k, v in sorted(field_stats.items())
        }
        special = {}
        for key, cands in F.SPECIAL_KEYS.items():
            in_sample = [c for c in cands if c in field_stats]
            if key == "bot" and "author" in field_stats:
                in_sample.append("author.bot?")
            special[key] = {
                "label": F.SPECIAL_LABELS[key],
                "fields_in_sample": in_sample,
                "filled_share": ratio(ps.special_filled[key], n),
            }
        channels = {}
        for scope in sorted(ps.channels):
            ch = ps.channels[scope]
            channels[scope] = {
                "messages": ch.messages,
                "days": len(ch.days),
                "per_day_mean": round(ch.messages / len(ch.days), 2) if ch.days else 0.0,
                "gap_seconds": quantiles(ch.gaps),
            }
        lengths = quantiles(ps.len_hist)
        lengths["max"] = max(ps.len_hist) if ps.len_hist else 0
        units = ps.segments + (ps.kept - ps.kept_chat)
        return {
            "files": ps.files,
            "formats": dict(sorted(ps.formats.items())),
            "layouts": dict(sorted(found.layouts.get(name, Counter()).items())),
            "messages": n,
            "bad_records": ps.bad_records,
            "kind": dict(sorted(ps.kind.items())),
            "by_month": dict(sorted(ps.by_month.items())),
            "timestamp_missing": ps.ts_missing,
            "text_len_chars": lengths,
            "tokens_est": {"total": ps.tokens, "per_msg_mean": round(ps.tokens / n, 2) if n else 0.0},
            "authors_unique": len(ps.authors),
            "author_missing": ps.author_missing,
            "share": {
                "empty": ratio(ps.empty, n),
                "emoji_only": ratio(ps.emoji_only, n),
                "image_only": ratio(ps.image_only, n),
            },
            "dup": {
                "same_author": ratio(ps.dup_same_author, ps.with_text),
                "cross_author_short": ratio(ps.dup_cross_short, ps.with_text),
            },
            "script": dict(sorted(ps.script.items())),
            "fields": {"sample_n": sample_n, "fields": fields_out},
            "special": special,
            "chat_channels": channels,
            "clean_trial": {
                "drops": dict(sorted(ps.drops.items())),
                "kept": ps.kept,
                "retention": ratio(ps.kept, n),
            },
            "unitize_trial": {
                "segments": ps.segments,
                "avg_segment_len": round(ps.kept_chat / ps.segments, 2) if ps.segments else 0.0,
                "units_total": units,
            },
        }
