"""`yuqing unitize`（T12）：messages + msg_flags → DATA_ROOT/units/unit_ver=<v>/day_cn=<日>/。

- 聊天类按流切段：流 = 平台 × 频道 × 讨论串（Discord 讨论串的消息另成一条流，不和主频道混切）。
- `unit_ver` = 切段规则版本 + 切段参数 + clean_ver 的哈希；`unit_id` = 哈希(unit_ver + 首条 msg_id)。
  同参数重跑编号不变、已有的单元不重写；参数一改就是新的 unit_ver 目录，旧的不动。
- 封口：默认按当前时间，最后一条距今不足 seal_minutes 的段先不输出；`--replay` 回放历史时全部封口。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from yuqing.config import ConfigError, load_config
from yuqing.normalize.clean import MESSAGES_DEDUP_SQL, current_clean_ver, flags_root
from yuqing.normalize.run import messages_root
from yuqing.normalize.store import glob_of, parquet_files, short_hash, write_partitioned
from yuqing.unitize.segment import ANNOTATE, Msg, Seg, n_authors, segment_chat, unit_lang

UNIT_RULE_VER = "q9-1"
CTX_TEXT_MAX = 200
CONTENT_PARAMS = ("gap_minutes", "max_msgs", "overlap_msgs", "ctx_inline_max")  # seal_minutes 只管时机，不进版本

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
    ]
)


def unit_ver_of(unitize_params: dict, clean_ver: str) -> str:
    p = {k: unitize_params[k] for k in CONTENT_PARAMS}
    return short_hash(UNIT_RULE_VER, json.dumps(p, sort_keys=True), clean_ver, n=12)


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


_COLS = (
    "msg_id, platform, community, server, channel, kind, parent_ref, parent_text, author_id, "
    "epoch(ts_utc) AS t, day_cn, lang, reply_to, route"
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


def _unit_row(seg: Seg, head: dict, unit_ver: str, clean_ver: str, ctx_text: str | None) -> dict:
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
    }


def _msg(r: dict) -> Msg:
    return Msg(r["msg_id"], r["t"], r["route"], r["author_id"], r["reply_to"], r["lang"], r["day_cn"])


def unitize(
    data_root: Path,
    unitize_params: dict,
    clean_ver: str,
    now: datetime | None = None,
    replay: bool = False,
) -> UnitizeResult:
    data_root = Path(data_root)
    mroot = messages_root(data_root)
    froot = flags_root(data_root, clean_ver)
    if not parquet_files(froot):
        raise ConfigError(f"找不到 clean_ver={clean_ver} 的 msg_flags：先跑 yuqing clean")
    p = {k: int(unitize_params[k]) for k in (*CONTENT_PARAMS, "seal_minutes")}
    ver = unit_ver_of(p, clean_ver)
    res = UnitizeResult(ver, clean_ver, root=units_root(data_root, ver))
    gap_s, seal_s = p["gap_minutes"] * 60, p["seal_minutes"] * 60
    now_t = None if replay else (now or datetime.now(UTC)).timestamp()

    con = duckdb.connect()
    con.execute(f"CREATE TEMP TABLE m AS {MESSAGES_DEDUP_SQL.format(src=_pq(mroot))}")
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

    out: list[dict] = []

    def emit(seg: Seg, head: dict, ctx_text: str | None = None) -> None:
        res.units += 1
        if now_t is not None and seg.end is not None and now_t - seg.end < seal_s:
            res.unsealed += 1
            return
        row = _unit_row(seg, head, ver, clean_ver, ctx_text)
        if row["kind"] == "chat":
            res.chat_units += 1
        if row["unit_id"] not in have:
            out.append(row)

    def flush_stream(rows: list[dict]) -> None:
        if not rows:
            return
        head = rows[0]
        msgs = [_msg(r) for r in rows]
        for seg in segment_chat(msgs, gap_s, p["max_msgs"], p["overlap_msgs"], p["ctx_inline_max"], reply_ok):
            emit(seg, head)

    key = None
    buf: list[dict] = []
    for r in _stream_rows(con):
        if r["kind"] != "chat":
            if r["route"] != ANNOTATE:
                continue
            m = _msg(r)
            seg = Seg(body=[m], ctx=[m.reply_to] if m.reply_to in reply_ok else [])
            text = r["parent_text"]
            emit(seg, r, text[:CTX_TEXT_MAX] if text else None)
            continue
        k = (r["platform"], r["channel"], r["thread"])
        if k != key:
            flush_stream(buf)
            key, buf = k, []
        buf.append(r)
    flush_stream(buf)
    con.close()

    if out:
        tag = short_hash(ver, *sorted(u["unit_id"] for u in out))
        write_partitioned(res.root, out, UNITS_SCHEMA, tag, sort_key=lambda u: (u["ts_start"] is None, u["unit_id"]))
        res.written = len(out)
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
    print(f"产出：{res.root}")
    return 0
