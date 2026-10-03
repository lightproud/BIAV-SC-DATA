"""`yuqing unitize`（T12）：messages + msg_flags → DATA_ROOT/units/unit_ver=<v>/day_cn=<日>/。

- 聊天类按流切段：流 = 平台 × 频道 × 讨论串（Discord 讨论串的消息另成一条流，不和主频道混切）。
- `unit_ver` = 切段规则版本 + 切段参数 + clean_ver 的哈希；`unit_id` = 哈希(unit_ver + 首条 msg_id)。
  同参数重跑编号不变、已有的单元不重写；参数一改就是新的 unit_ver 目录，旧的不动。
- 封口：默认按当前时间，最后一条距今不足 seal_minutes 的段先不输出；`--replay` 回放历史时全部封口。
- 不送标的单元照样切出来，只打 `skip_reason`（守密人 2026-10-03）：
  `chitchat_channel`（config/channels.toml 里的纯闲聊频道）、
  `low_content`（聊天单元里最长一句都不到 low_content_max_chars 字）。默认只把 skip_reason 为空的送标。
"""

from __future__ import annotations

import argparse
import json
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa

from yuqing.config import ConfigError, load_config
from yuqing.normalize.clean import MESSAGES_DEDUP_SQL, current_clean_ver, flags_root
from yuqing.normalize.run import messages_root
from yuqing.normalize.store import StagedWriter, duckdb_connect, glob_of, parquet_files, short_hash
from yuqing.unitize.segment import ANNOTATE, Msg, Seg, n_authors, segment_chat, unit_lang

UNIT_RULE_VER = "q9-3"  # 2：连续发言并句、skip_reason；3：回复关系、碎片并回（2026-10-03）
CTX_TEXT_MAX = 200
CONTENT_PARAMS = (
    "gap_minutes", "max_msgs", "overlap_msgs", "ctx_inline_max", "turn_gap_seconds", "low_content_max_chars",
    "reply_window_minutes", "attach_minutes", "attach_max_turns",
)  # fmt: skip
# seal_minutes 只管时机，不进版本
SKIP_CHITCHAT, SKIP_LOW = "chitchat_channel", "low_content"
# 低内容判定用的「字母当量」长度：汉字、假名、谚文一个顶三个字母（一个汉字的信息量约合英文三四个字母）
TEXT_LEN_SQL = (
    "length(trim(text)) + 2 * length(regexp_replace(text, '[^\\p{Han}\\p{Hiragana}\\p{Katakana}\\p{Hangul}]', '', 'g'))"
    " AS text_len"
)

UNITS_SCHEMA = pa.schema(
    [
        ("unit_id", pa.string()),
        ("unit_ver", pa.string()),
        ("platform", pa.string()),
        ("kind", pa.string()),
        ("community", pa.string()),
        ("server", pa.string()),
        ("channel", pa.string()),
        ("lang", pa.string()),
        ("msg_ids", pa.list_(pa.string())),
        ("ctx_msg_ids", pa.list_(pa.string())),
        ("ctx_text", pa.string()),
        ("ts_start", pa.timestamp("us", tz="UTC")),
        ("ts_end", pa.timestamp("us", tz="UTC")),
        ("day_cn", pa.date32()),
        ("n_msgs", pa.int32()),
        ("n_authors", pa.int32()),
        ("clean_ver", pa.string()),
        ("turn_ids", pa.list_(pa.int32())),
        ("n_turns", pa.int32()),
        ("skip_reason", pa.string()),
    ]
)


def channels_path() -> Path:
    from yuqing.annotate.schema import config_dir
    from yuqing.config import project_root

    p = config_dir() / "channels.toml"
    return p if p.is_file() else project_root() / "config" / "channels.toml"


def load_excluded_channels(path: Path | None = None) -> frozenset[tuple[str, str]]:
    path = path or channels_path()
    if not Path(path).is_file():
        return frozenset()
    with Path(path).open("rb") as fh:
        doc = tomllib.load(fh)
    return frozenset((str(e["platform"]), str(e["channel"])) for e in doc.get("exclude", []))


def unit_ver_of(unitize_params: dict, clean_ver: str, excluded: frozenset[tuple[str, str]] = frozenset()) -> str:
    p = {k: unitize_params[k] for k in CONTENT_PARAMS}
    chans = short_hash(*sorted(f"{a}\x1f{b}" for a, b in excluded))
    return short_hash(UNIT_RULE_VER, json.dumps(p, sort_keys=True), clean_ver, chans, n=12)


def unit_id_of(unit_ver: str, first_msg_id: str) -> str:
    return short_hash("unit", unit_ver, first_msg_id)


def units_root(data_root: Path, unit_ver: str) -> Path:
    return Path(data_root) / "units" / f"unit_ver={unit_ver}"


def _pq(root: Path) -> str:
    return f"read_parquet('{glob_of(root)}', hive_partitioning=false, union_by_name=true)"


def _ts(t: float | None) -> datetime | None:
    return datetime.fromtimestamp(t, tz=UTC) if t is not None else None


@dataclass
class UnitizeResult:
    unit_ver: str
    clean_ver: str
    units: int = 0
    written: int = 0
    unsealed: int = 0
    chat_units: int = 0
    root: Path | None = None
    skipped: dict = field(default_factory=dict)


_COLS = (
    "msg_id, platform, community, server, channel, kind, parent_ref, parent_text, author_id, "
    "epoch(ts_utc) AS t, day_cn, lang, reply_to, route, text_len"
)


def _stream_rows(con, batch: int = 50_000) -> Iterator[dict]:
    reader = con.execute(
        f"""
        SELECT {_COLS},
               CASE WHEN kind = 'chat' AND starts_with(coalesce(parent_ref, ''), 'thread:')
                    THEN parent_ref END AS thread
        FROM j
        ORDER BY kind = 'chat' DESC, platform, channel NULLS FIRST, thread NULLS FIRST, t NULLS LAST, msg_id
        """
    ).to_arrow_reader(batch)
    for rb in reader:
        yield from rb.to_pylist()


def _skip_reason(seg: Seg, head: dict, excluded: frozenset[tuple[str, str]], low_max: int) -> str | None:
    if (head["platform"], head["channel"]) in excluded:
        return SKIP_CHITCHAT
    if head["kind"] == "chat" and low_max > 0:
        per_turn: dict[int, int] = {}
        for m, k in zip(seg.body, seg.turns or range(len(seg.body)), strict=True):
            per_turn[k] = per_turn.get(k, 0) + m.text_len
        if max(per_turn.values()) < low_max:
            return SKIP_LOW
    return None


def _unit_row(seg: Seg, head: dict, unit_ver: str, clean_ver: str, ctx_text: str | None, skip: str | None) -> dict:
    body = seg.body
    times = [m.t for m in body if m.t is not None]
    return {
        "unit_id": unit_id_of(unit_ver, body[0].msg_id),
        "unit_ver": unit_ver,
        "platform": head["platform"],
        "kind": head["kind"],
        "community": head["community"],
        "server": head["server"],
        "channel": head["channel"],
        "lang": unit_lang(body),
        "msg_ids": [m.msg_id for m in body],
        "ctx_msg_ids": list(seg.ctx),
        "ctx_text": ctx_text,
        "ts_start": _ts(min(times)) if times else None,
        "ts_end": _ts(max(times)) if times else None,
        "day_cn": body[0].day_cn,
        "n_msgs": len(body),
        "n_authors": n_authors(body),
        "clean_ver": clean_ver,
        "turn_ids": list(seg.turns) if seg.turns else list(range(len(body))),
        "n_turns": seg.n_turns,
        "skip_reason": skip,
    }


def _msg(r: dict) -> Msg:
    return Msg(
        r["msg_id"], r["t"], r["route"], r["author_id"], r["reply_to"], r["lang"], r["day_cn"], r["text_len"] or 0
    )


def unitize(
    data_root: Path,
    unitize_params: dict,
    clean_ver: str,
    now: datetime | None = None,
    replay: bool = False,
    excluded: frozenset[tuple[str, str]] | None = None,
) -> UnitizeResult:
    data_root = Path(data_root)
    mroot = messages_root(data_root)
    froot = flags_root(data_root, clean_ver)
    if not parquet_files(froot):
        raise ConfigError(f"找不到 clean_ver={clean_ver} 的 msg_flags：先跑 yuqing clean")
    p = {k: int(unitize_params[k]) for k in (*CONTENT_PARAMS, "seal_minutes")}
    excluded = load_excluded_channels() if excluded is None else excluded
    ver = unit_ver_of(p, clean_ver, excluded)
    res = UnitizeResult(ver, clean_ver, root=units_root(data_root, ver))
    gap_s, seal_s = p["gap_minutes"] * 60, p["seal_minutes"] * 60
    now_t = None if replay else (now or datetime.now(UTC)).timestamp()

    con = duckdb_connect(data_root)
    cols = (
        "msg_id, platform, community, server, channel, kind, parent_ref, parent_text, author_id, ts_utc, "
        "day_cn, lang, reply_to, raw_ref, " + TEXT_LEN_SQL
    )  # 切段不用正文，只读长度
    con.execute(f"CREATE TEMP TABLE m AS {MESSAGES_DEDUP_SQL.format(src=f'(SELECT {cols} FROM {_pq(mroot)})')}")
    con.execute(
        f"CREATE TEMP TABLE j AS SELECT m.*, f.route FROM m JOIN {_pq(froot)} f USING (msg_id) WHERE f.route <> 'skip'"
    )
    reply_ok = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT j.reply_to FROM j JOIN m ON j.reply_to = m.msg_id WHERE j.route = 'annotate'"
        ).fetchall()
    }
    have: set[str] = set()
    if parquet_files(res.root):
        have = {r[0] for r in con.execute(f"SELECT unit_id FROM {_pq(res.root)}").fetchall()}

    out = StagedWriter(res.root, UNITS_SCHEMA, key="unit_id", order="day_cn, ts_start NULLS LAST, unit_id")

    def emit(seg: Seg, head: dict, ctx_text: str | None = None) -> None:
        res.units += 1
        if now_t is not None and seg.end is not None and now_t - seg.end < seal_s:
            res.unsealed += 1
            return
        row = _unit_row(
            seg, head, ver, clean_ver, ctx_text, _skip_reason(seg, head, excluded, p["low_content_max_chars"])
        )
        if row["kind"] == "chat":
            res.chat_units += 1
        if row["skip_reason"]:
            res.skipped[row["skip_reason"]] = res.skipped.get(row["skip_reason"], 0) + 1
        if row["unit_id"] not in have:
            out.add(row)

    def flush_stream(rows: list[dict]) -> None:
        if not rows:
            return
        head = rows[0]
        msgs = [_msg(r) for r in rows]
        segs = segment_chat(
            msgs,
            gap_s,
            p["max_msgs"],
            p["overlap_msgs"],
            p["ctx_inline_max"],
            reply_ok,
            p["turn_gap_seconds"],
            reply_window_s=p["reply_window_minutes"] * 60,
            attach_s=p["attach_minutes"] * 60,
            attach_max_turns=p["attach_max_turns"],
        )
        for seg in segs:
            emit(seg, head)

    key = None
    buf: list[dict] = []
    for r in _stream_rows(con):
        if r["kind"] != "chat":
            if r["route"] != ANNOTATE:
                continue
            m = _msg(r)
            seg = Seg(body=[m], ctx=[m.reply_to] if m.reply_to in reply_ok else [], turns=[0])
            text = r["parent_text"]
            emit(seg, r, text[:CTX_TEXT_MAX] if text else None)
            continue
        k = (r["platform"], r["channel"], r["thread"])
        if k != key:
            flush_stream(buf)
            key, buf = k, []
        buf.append(r)
    flush_stream(buf)
    res.written = out.close(con, ver)
    con.close()
    return res


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--replay", action="store_true", help="回放历史：全部封口（不等 seal_minutes）")
    p.add_argument("--now", help="当前时间（ISO 8601，测试与复算用；默认取系统时间）")
    p.add_argument("--clean-ver", help="用哪一版 msg_flags（默认按当前配置算出的 clean_ver）")
    sub = p.add_subparsers(dest="unitize_cmd", metavar="<动作>")
    from yuqing.unitize.review import add_args as add_review_args

    add_review_args(sub.add_parser("review", help="试切检查包：抽 N 段出单文件 HTML（T13）"))


def run_cli(args: argparse.Namespace) -> int:
    if getattr(args, "unitize_cmd", None) == "review":
        from yuqing.unitize.review import run_cli as review_cli

        return review_cli(args)
    cfg = load_config()
    data_root = cfg.data_root()
    clean_ver = args.clean_ver or current_clean_ver(cfg)
    now = datetime.fromisoformat(args.now) if args.now else None
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    res = unitize(data_root, cfg.params["unitize"], clean_ver, now=now, replay=args.replay)
    print(
        f"切段完成（unit_ver {res.unit_ver}，clean_ver {res.clean_ver}）：共 {res.units:,} 段，"
        f"其中聊天 {res.chat_units:,} 段；本次新写 {res.written:,} 段，未封口留到下一轮 {res.unsealed:,} 段。"
    )
    if res.skipped:
        detail = "、".join(f"{k} {v:,}" for k, v in sorted(res.skipped.items()))
        print(f"不送标（单元照留）：{detail}；送标 {res.units - res.unsealed - sum(res.skipped.values()):,} 段。")
    print(f"产出：{res.root}")
    return 0
