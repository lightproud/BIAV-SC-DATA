"""`yuqing normalize`：数据湖原文 → DATA_ROOT/messages。

增量按文件记账（`messages/_ledger.jsonl`，只追加）：记下每个文件处理到第几行（json 文档按条目数）；
文件追加了新行就只读新行。记账键去掉 `.gz` 后缀，冷层压缩后不会重读。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from yuqing.config import Config, ConfigError, check_outside_repo, load_config, project_root
from yuqing.lake.layout import LakeFile, discover
from yuqing.lake.reader import LakeReader
from yuqing.normalize.messages import MESSAGES_SCHEMA, Communities, to_row
from yuqing.normalize.store import short_hash, write_partitioned

FLUSH_ROWS = 200_000


def messages_root(data_root: Path) -> Path:
    return Path(data_root) / "messages"


def _ledger_key(rel: str) -> str:
    return rel[:-3] if rel.endswith(".gz") else rel


def load_ledger(root: Path) -> dict[str, int]:
    path = root / "_ledger.jsonl"
    done: dict[str, int] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["key"]] = max(done.get(rec["key"], 0), int(rec["done"]))
    return done


@dataclass
class NormalizeResult:
    files_seen: int = 0
    files_read: int = 0
    rows: int = 0
    bad_records: int = 0
    parts: list[Path] = field(default_factory=list)


def select_files(
    files: Iterable[LakeFile],
    platforms: list[str] | None = None,
    prefixes: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[LakeFile]:
    out = []
    for f in files:
        if platforms and f.platform not in platforms:
            continue
        if prefixes and not any(f.rel.startswith(p.rstrip("/") + "/") or f.rel == p for p in prefixes):
            continue
        if since and f.day < since:
            continue
        if until and f.day > until:
            continue
        out.append(f)
    return out


def normalize(
    lake: Path,
    data_root: Path,
    salt: str,
    communities: Communities,
    files: list[LakeFile] | None = None,
) -> NormalizeResult:
    lake = Path(lake)
    root = messages_root(data_root)
    root.mkdir(parents=True, exist_ok=True)
    reader = LakeReader(lake)
    if files is None:
        files = discover(lake).files
    ledger = load_ledger(root)
    res = NormalizeResult()
    buf: list[dict] = []
    spans: list[str] = []
    ledger_lines: list[str] = []

    def flush() -> None:
        if not buf:
            return
        tag = short_hash(*spans, n=16)
        res.parts += write_partitioned(
            root, buf, MESSAGES_SCHEMA, tag, sort_key=lambda r: (r["ts_utc"] is None, r["ts_utc"] or 0, r["msg_id"])
        )
        with (root / "_ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.writelines(ledger_lines)
        buf.clear()
        spans.clear()
        ledger_lines.clear()

    for f in files:
        res.files_seen += 1
        key = _ledger_key(f.rel)
        done = ledger.get(key, 0)
        last = done
        added = 0
        for no, rec in reader.records(f):
            # jsonl 行号从 1 起，json 下标从 0 起：统一成「已处理条数」
            pos = no if f.fmt == "jsonl" else no + 1
            if pos <= done:
                continue
            last = max(last, pos)
            if rec.get("__bad__"):
                res.bad_records += 1
                continue
            buf.append(to_row(f, no, rec, salt, communities))
            added += 1
        if last > done:
            res.files_read += 1
            res.rows += added
            spans.append(f"{key}:{done}-{last}")
            ledger_lines.append(json.dumps({"key": key, "done": last}, ensure_ascii=False) + "\n")
            ledger[key] = last
        if len(buf) >= FLUSH_ROWS:
            flush()
    flush()
    return res


def resolve_raw_ref(lake: Path, raw_ref: str) -> dict:
    """按 raw_ref 取回原文那一条记录（冷层压缩前后都能取到）。"""
    lake = Path(lake)
    if ":L" in raw_ref:
        rel, no_s = raw_ref.rsplit(":L", 1)
        fmt = "jsonl"
    else:
        rel, no_s = raw_ref.rsplit("#", 1)
        fmt = "json"
    no = int(no_s)
    candidates = [rel, rel + ".gz"] if not rel.endswith(".gz") else [rel, rel[:-3]]
    reader = LakeReader(lake)
    for cand in candidates:
        if (lake / cand).is_file():
            parts = cand.split("/")
            f = LakeFile(cand, parts[0], "/".join(parts[:-1]), None, "", fmt, cand.endswith(".gz"), "")
            for n, rec in reader.records(f):
                if n == no:
                    return rec
    raise KeyError(f"raw_ref 取不回原文：{raw_ref}")


def default_lake(cfg: Config) -> Path:
    from yuqing.census.run import default_lake as repo_lake

    lake = cfg.env.get("LAKE_ROOT") or repo_lake()
    if lake is None:
        cfg.require("LAKE_ROOT")
    return Path(lake)


def communities_path() -> Path:
    from yuqing.annotate.schema import config_dir

    p = config_dir() / "communities.toml"
    return p if p.is_file() else project_root() / "config" / "communities.toml"


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lake", help="数据湖根目录（默认 LAKE_ROOT，未设则用本仓 Record/Community）")
    p.add_argument("--platform", action="append", help="只处理这些平台（可重复）")
    p.add_argument("--path", action="append", help="只处理这些相对路径前缀（可重复），如 discord/global/channels/123")
    p.add_argument("--since", help="文件日期下限 YYYY-MM-DD（含）")
    p.add_argument("--until", help="文件日期上限 YYYY-MM-DD（含）")


def run_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    salt = cfg.require("AUTHOR_SALT")["AUTHOR_SALT"]
    data_root = cfg.data_root()
    lake = Path(args.lake) if args.lake else default_lake(cfg)
    if not lake.is_dir():
        raise ConfigError(f"数据湖目录不存在：{lake}")
    check_outside_repo(data_root, "DATA_ROOT")
    files = select_files(discover(lake).files, args.platform, args.path, args.since, args.until)
    res = normalize(lake, data_root, salt, Communities.load(communities_path()), files)
    print(
        f"规范化完成：扫描 {res.files_seen:,} 个文件，读入新内容 {res.files_read:,} 个，"
        f"新增 {res.rows:,} 行，坏记录 {res.bad_records:,} 条，写出 {len(res.parts):,} 个分区文件。"
    )
    return 0
