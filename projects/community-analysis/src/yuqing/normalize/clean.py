"""`yuqing clean`（T11）：给每条消息打全部命中的标记，再分三档路由；一条都不删。

按疑问清单 Q9（守密人 2026-10-03）施工，工单原文「drop_reason 取第一个命中」作废：
- `msg_flags` 一行一条消息：`flags` 记全部命中标记，`route` 定送不送标——
  `annotate` 送标；`context_only` 只进切段的只读上下文、不占正文名额；`skip` 不进单元。
- 路由：机器人、系统、空、仅链接、仅 @、机器人指令、重复类 → skip；纯表情、有图无字、极短、接龙 → context_only；
  其余（含只作属性的长贴、连发）→ annotate。极短、连发、接龙只对聊天类判。
- 不同作者发的相同短句不去重（那是声量）；只有 3 人以上 2 分钟内齐发同一短句才记「接龙」，且只降为上下文。
- `noise_daily`：频道 × 日（UTC+8）× 标记的条数与独立发言人数，只做群体统计、不按作者排行；
  独立发言人不足 privacy.min_authors_cell 的格子只给条数（规矩第 7 条）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from yuqing.census import fields as F
from yuqing.config import Config, load_config, project_root
from yuqing.normalize.run import messages_root
from yuqing.normalize.store import glob_of, parquet_files, short_hash, write_partitioned

CLEAN_RULE_VER = "q9-1"

ROUTE_ANNOTATE, ROUTE_CONTEXT, ROUTE_SKIP = "annotate", "context_only", "skip"
ROUTES = (ROUTE_ANNOTATE, ROUTE_CONTEXT, ROUTE_SKIP)
SKIP_FLAGS = frozenset(
    {
        "bot", "system", "empty", "link_only", "mention_only", "bot_command",
        "dup_same_author", "dup_near_same_author", "dup_long_repost",
    }
)  # fmt: skip
CONTEXT_FLAGS = frozenset({"emoji_only", "image_only", "short", "chorus"})
ATTR_FLAGS = frozenset({"long_post", "burst"})  # 只标不拦
ALL_FLAGS = tuple(sorted(SKIP_FLAGS | CONTEXT_FLAGS | ATTR_FLAGS))

# discord 消息类型：0 普通、19 回复、21 讨论串首帖；20 是斜杠指令的回执；其余为入群、置顶等系统消息
_NORMAL_TYPES = frozenset({0, 19, 21})
_COMMAND_TYPE = 20

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>|@everyone|@here")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>|:[\w+-]+:")
_COMMAND_RE = re.compile(r"^[!/$.;~][A-Za-z][\w-]{0,31}(?:\s|$)")
_COMMAND_MAX_CHARS = 200

FLAGS_SCHEMA = pa.schema(
    [
        ("msg_id", pa.string()),
        ("day_cn", pa.date32()),
        ("flags", pa.list_(pa.string())),
        ("route", pa.string()),
        ("image_only", pa.bool_()),
        ("clean_ver", pa.string()),
    ]
)
NOISE_SCHEMA = pa.schema(
    [
        ("platform", pa.string()),
        ("channel", pa.string()),
        ("day_cn", pa.date32()),
        ("flag", pa.string()),
        ("n_msgs", pa.int64()),
        ("n_authors", pa.int64()),
        ("clean_ver", pa.string()),
    ]
)


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def _is_filler(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat[0] in "PZSC" or cat in ("Mn", "Me")


def _nothing_left(text: str) -> bool:
    return all(_is_filler(ch) for ch in _CUSTOM_EMOJI_RE.sub("", text))


def norm_text(text: str) -> str:
    """近似重复与接龙用的归一：去标点、空白、控制符，统一大小写。表情等符号保留。"""
    return "".join(ch for ch in text.casefold() if unicodedata.category(ch)[0] not in "PZC")


def content_flags(text: str, kind: str, has_image: bool | None, params: dict) -> set[str]:
    """只看这一条本身就能判的标记。"""
    flags: set[str] = set()
    stripped = text.strip()
    if not stripped:
        flags.add("image_only" if has_image else "empty")
        return flags
    if F.is_emoji_only(text):
        flags.add("emoji_only")
    if _URL_RE.search(text) and _nothing_left(_MENTION_RE.sub("", _URL_RE.sub("", text))):
        flags.add("link_only")
    elif _MENTION_RE.search(text) and _nothing_left(_MENTION_RE.sub("", text)):
        flags.add("mention_only")
    if len(stripped) <= _COMMAND_MAX_CHARS and _COMMAND_RE.match(stripped):
        flags.add("bot_command")
    if len(stripped) >= params["long_post_min_chars"]:
        flags.add("long_post")
    if kind == "chat" and "emoji_only" not in flags:
        core = re.sub(r"\s+", "", _CUSTOM_EMOJI_RE.sub("", stripped))
        if 0 < len(core) <= params["short_max_chars"]:
            flags.add("short")
    return flags


def route_of(flags: set[str]) -> str:
    if flags & SKIP_FLAGS:
        return ROUTE_SKIP
    if flags & CONTEXT_FLAGS:
        return ROUTE_CONTEXT
    return ROUTE_ANNOTATE


@dataclass
class _Entry:
    msg_id: str
    day_cn: object
    t: float | None
    flags: set[str]


@dataclass
class _Stream:
    """一个频道（平台 × channel）里按时间走的状态。"""

    last_exact: dict[int, float] = field(default_factory=dict)
    last_near: dict[int, float] = field(default_factory=dict)
    by_author: dict[str, deque] = field(default_factory=dict)
    chorus: dict[int, deque] = field(default_factory=dict)
    pending: deque = field(default_factory=deque)
    seen: int = 0


class Cleaner:
    def __init__(
        self, params: dict, bots: frozenset[tuple[str, str]] = frozenset(), long_dups: frozenset[str] = frozenset()
    ):
        p = {k: int(v) for k, v in params.items()}
        self.p = p
        self.window = p["same_author_window_hours"] * 3600
        self.long_min = p["long_repost_min_chars"]
        self.hold = max(p["burst_window_s"], p["chorus_window_s"])
        self.bots = bots
        self.long_dups = long_dups

    def run(self, rows: Iterator[dict]) -> Iterator[_Entry]:
        """rows 须按 (platform, channel, t, msg_id) 排好；t 为空的排在频道末尾。"""
        key = None
        st = _Stream()
        for r in rows:
            k = (r["platform"], r["channel"])
            if k != key:
                yield from st.pending
                key, st = k, _Stream()
            yield from self._one(st, r)
        yield from st.pending

    def _one(self, st: _Stream, r: dict) -> Iterator[_Entry]:
        text = r["text"] or ""
        t = r["t"]
        flags = content_flags(text, r["kind"], r["has_image"], self.p)
        mtype = r["msg_type"]
        if mtype is not None and mtype not in _NORMAL_TYPES:
            flags.add("bot_command" if mtype == _COMMAND_TYPE else "system")
        author = r["author_id"]
        if r["bot_flag"] or (author is not None and (r["platform"], author) in self.bots):
            flags.add("bot")
        if r["msg_id"] in self.long_dups:
            flags.add("dup_long_repost")
        e = _Entry(r["msg_id"], r["day_cn"], t, flags)
        if t is not None:
            while st.pending and st.pending[0].t is not None and st.pending[0].t < t - self.hold:
                yield st.pending.popleft()
            self._time_rules(st, e, text, author, r["kind"] == "chat")
        st.pending.append(e)
        st.seen += 1
        if st.seen % 50_000 == 0 and t is not None:
            self._prune(st, t)

    def _time_rules(self, st: _Stream, e: _Entry, text: str, author: str | None, chat: bool) -> None:
        t = e.t
        stripped = text.strip()
        normed = norm_text(text) if stripped else ""
        if author is not None and stripped:
            ke = _h64(f"{author}\x1f{text}")
            prev = st.last_exact.get(ke)
            if prev is not None and t - prev <= self.window:
                e.flags.add("dup_same_author")
            st.last_exact[ke] = t
            if normed:
                kn = _h64(f"{author}\x1f{normed}")
                prev = st.last_near.get(kn)
                if prev is not None and t - prev <= self.window and "dup_same_author" not in e.flags:
                    e.flags.add("dup_near_same_author")
                st.last_near[kn] = t
        if not chat:
            return
        if author is not None:
            dq = st.by_author.setdefault(author, deque())
            while dq and t - dq[0].t > self.p["burst_window_s"]:
                dq.popleft()
            dq.append(e)
            if len(dq) >= self.p["burst_min_msgs"]:
                for x in dq:
                    x.flags.add("burst")
        if normed and len(stripped) < self.long_min:
            kc = _h64(normed)
            dq = st.chorus.setdefault(kc, deque())
            while dq and t - dq[0][0].t > self.p["chorus_window_s"]:
                dq.popleft()
            dq.append((e, author))
            if len({a for _, a in dq if a is not None}) >= self.p["chorus_min_authors"]:
                for x, _ in dq:
                    x.flags.add("chorus")

    def _prune(self, st: _Stream, t: float) -> None:
        for d in (st.last_exact, st.last_near):
            for k in [k for k, v in d.items() if t - v > self.window]:
                del d[k]
        for k in [k for k, dq in st.by_author.items() if not dq or t - dq[-1].t > self.hold]:
            del st.by_author[k]
        for k in [k for k, dq in st.chorus.items() if not dq or t - dq[-1][0].t > self.hold]:
            del st.chorus[k]


# ── 版本、名单、存取 ──


def bots_path() -> Path:
    from yuqing.annotate.schema import config_dir

    p = config_dir() / "bots.txt"
    return p if p.is_file() else project_root() / "config" / "bots.txt"


def load_bots(path: Path) -> list[tuple[str, str]]:
    out = []
    if not Path(path).is_file():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out.append((parts[0], parts[1].strip()))
    return out


def bot_ids(bots: list[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    return frozenset(bots)


def clean_ver_of(params: dict, bots: list[tuple[str, str]]) -> str:
    """规则版本 + 清洗参数 + 名单内容；名单只以摘要参与，不落盘。"""
    digest = short_hash(*sorted(f"{p}\x1f{i}" for p, i in bots))
    return short_hash(CLEAN_RULE_VER, json.dumps(params, sort_keys=True), digest, n=12)


def flags_root(data_root: Path, clean_ver: str) -> Path:
    return Path(data_root) / "msg_flags" / f"clean_ver={clean_ver}"


def noise_root(data_root: Path, clean_ver: str) -> Path:
    return Path(data_root) / "noise_daily" / f"clean_ver={clean_ver}"


def report_path(data_root: Path, clean_ver: str) -> Path:
    return Path(data_root) / "reports" / "clean" / clean_ver / "clean_report.json"


def _pq(root: Path) -> str:
    return f"read_parquet('{glob_of(root)}', hive_partitioning=false, union_by_name=true)"


# 同一条被重复采到时 messages 里有多行（一行不删）；往下各层按 msg_id 只取最早采到的那一行
MESSAGES_DEDUP_SQL = """
SELECT * FROM {src}
QUALIFY row_number() OVER (PARTITION BY msg_id ORDER BY ts_utc NULLS LAST, raw_ref) = 1
"""


def _existing_ids(con, root: Path) -> set[str]:
    if not parquet_files(root):
        return set()
    return {r[0] for r in con.execute(f"SELECT msg_id FROM {_pq(root)}").fetchall()}


def _long_dups(con, long_min: int) -> frozenset[str]:
    rows = con.execute(
        f"""
        SELECT msg_id FROM (
          SELECT msg_id, row_number() OVER (PARTITION BY text ORDER BY ts_utc NULLS LAST, msg_id) AS rn
          FROM m WHERE length(text) >= {int(long_min)}
        ) WHERE rn > 1
        """
    ).fetchall()
    return frozenset(r[0] for r in rows)


def _iter_messages(con, batch: int = 50_000) -> Iterator[dict]:
    rel = con.execute(
        """
        SELECT msg_id, platform, channel, kind, author_id, epoch(ts_utc) AS t, day_cn, text,
               has_image, bot_flag, msg_type
        FROM m ORDER BY platform, channel NULLS FIRST, t NULLS LAST, msg_id
        """
    )
    reader = rel.to_arrow_reader(batch)
    for rb in reader:
        yield from rb.to_pylist()


@dataclass
class CleanResult:
    clean_ver: str
    total: int = 0
    written: int = 0
    duplicate_ids: int = 0
    report: dict = field(default_factory=dict)
    report_path: Path | None = None


def clean(data_root: Path, cfg_params: dict, bots: list[tuple[str, str]] | None = None) -> CleanResult:
    params = dict(cfg_params["clean"])
    bots = bots or []
    ver = clean_ver_of(params, bots)
    data_root = Path(data_root)
    mroot = messages_root(data_root)
    froot = flags_root(data_root, ver)
    res = CleanResult(ver)
    if not parquet_files(mroot):
        return res
    con = duckdb.connect()
    con.execute(f"CREATE TEMP TABLE m AS {MESSAGES_DEDUP_SQL.format(src=_pq(mroot))}")
    raw_n = con.execute(f"SELECT count(*) FROM {_pq(mroot)}").fetchone()[0]
    res.total = con.execute("SELECT count(*) FROM m").fetchone()[0]
    res.duplicate_ids = raw_n - res.total
    have = _existing_ids(con, froot)
    cleaner = Cleaner(params, bot_ids(bots), _long_dups(con, int(params["long_repost_min_chars"])))
    out = []
    for e in cleaner.run(_iter_messages(con)):
        if e.msg_id in have:
            continue
        flags = sorted(e.flags)
        out.append(
            {
                "msg_id": e.msg_id,
                "day_cn": e.day_cn,
                "flags": flags,
                "route": route_of(e.flags),
                "image_only": "image_only" in e.flags,
                "clean_ver": ver,
            }
        )
    if out:
        tag = short_hash(ver, *sorted(e["msg_id"] for e in out))
        write_partitioned(froot, out, FLAGS_SCHEMA, tag, sort_key=lambda r: r["msg_id"])
        res.written = len(out)
    min_cell = int(cfg_params.get("privacy", {}).get("min_authors_cell", 5))
    write_noise_daily(con, data_root, ver, min_cell)
    res.report = build_report(con, froot, ver, res.duplicate_ids)
    path = report_path(data_root, ver)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res.report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    res.report_path = path
    con.close()
    return res


def write_noise_daily(con, data_root: Path, ver: str, min_cell: int) -> Path | None:
    """频道 × 日 × 标记的群体汇总。另加 `_all`（全部消息）与 `route:<档>` 两类伪标记作分母。"""
    froot = flags_root(data_root, ver)
    files = parquet_files(froot)
    if not files:
        return None
    tag = short_hash(ver, *(p.relative_to(froot).as_posix() for p in files))
    nroot = noise_root(data_root, ver)
    path = nroot / f"noise_daily-{tag}.parquet"
    if path.exists():
        return path
    rows = con.execute(
        f"""
        WITH f AS (
          SELECT m.platform, m.channel, m.day_cn, m.author_id, f.flags, f.route
          FROM {_pq(froot)} f JOIN m USING (msg_id)
        ),
        x AS (
          SELECT platform, channel, day_cn, author_id, unnest(flags) AS flag FROM f
          UNION ALL SELECT platform, channel, day_cn, author_id, '_all' FROM f
          UNION ALL SELECT platform, channel, day_cn, author_id, 'route:' || route FROM f
        )
        SELECT platform, channel, day_cn, flag, count(*) AS n_msgs, count(DISTINCT author_id) AS n_authors
        FROM x GROUP BY ALL ORDER BY platform, channel NULLS FIRST, day_cn, flag
        """
    ).fetchall()
    out = [
        {
            "platform": p,
            "channel": c,
            "day_cn": d,
            "flag": fl,
            "n_msgs": n,
            "n_authors": a if a >= min_cell else None,
            "clean_ver": ver,
        }
        for p, c, d, fl, n, a in rows
    ]
    nroot.mkdir(parents=True, exist_ok=True)
    tmp = nroot / f".noise_daily-{tag}.parquet.tmp"
    pq.write_table(pa.Table.from_pylist(out, schema=NOISE_SCHEMA), tmp)
    tmp.replace(path)
    return path


def build_report(con, froot: Path, ver: str, duplicate_ids: int) -> dict:
    rows = con.execute(f"SELECT f.flags, f.route, m.platform FROM {_pq(froot)} f JOIN m USING (msg_id)").fetchall()
    n = len(rows)
    flags: Counter = Counter()
    routes: Counter = Counter()
    by_platform: dict[str, Counter] = {}
    for fl, route, platform in rows:
        flags.update(fl)
        routes[route] += 1
        by_platform.setdefault(platform, Counter())[route] += 1

    def share(c: Counter, keys) -> dict:
        return {k: {"n": c.get(k, 0), "share": round(c.get(k, 0) / n, 4) if n else 0.0} for k in keys}

    return {
        "clean_ver": ver,
        "rule_ver": CLEAN_RULE_VER,
        "total": n,
        "routes": share(routes, ROUTES),
        "flags": share(flags, ALL_FLAGS),
        "image_only": share(flags, ["image_only"])["image_only"],
        "by_platform": {
            p: {"total": sum(c.values()), **{r: c.get(r, 0) for r in ROUTES}} for p, c in sorted(by_platform.items())
        },
        "duplicate_msg_ids": duplicate_ids,
        "note": "多标签：一条可命中多个标记，flags 各项占比之和可大于 1；routes 三档之和为 1。"
        "duplicate_msg_ids 是重复采到的行数（同一 msg_id 的额外行），只计数、不重复打标。",
    }


def current_clean_ver(cfg: Config) -> str:
    return clean_ver_of(dict(cfg.params["clean"]), load_bots(bots_path()))


def run_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    data_root = cfg.data_root()
    res = clean(data_root, cfg.params, load_bots(bots_path()))
    if not res.total:
        print("messages 表为空：先跑 yuqing normalize。")
        return 1
    r = res.report["routes"]
    print(
        f"清洗打标完成（clean_ver {res.clean_ver}）：共 {res.total:,} 条，本次新写 {res.written:,} 条；"
        f"送标 {r['annotate']['share']:.2%}、只作上下文 {r['context_only']['share']:.2%}、"
        f"不进单元 {r['skip']['share']:.2%}。"
    )
    print(f"报告：{res.report_path}")
    return 0
