"""产出表的读写：按 day_cn 分区的 Parquet，只追加。

目录形如 `<表根>/day_cn=YYYY-MM-DD/part-<标签>.parquet`；分区目录只是存放方式，
day_cn 同时写在文件里，读的时候不靠目录名推断。已有的文件不覆盖、不改写。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def short_hash(*parts: str, n: int = 16) -> str:
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return h[:n]


def parquet_files(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.parquet") if not p.name.startswith("."))


def write_partitioned(root: Path, rows: list[dict], schema: pa.Schema, tag: str, sort_key=None) -> list[Path]:
    """按 day_cn 分区写一批行；每个分区一个新文件，文件已存在就拒绝（只追加）。"""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r["day_cn"])].append(r)
    written: list[Path] = []
    for day in sorted(by_day):
        part = by_day[day]
        if sort_key is not None:
            part.sort(key=sort_key)
        table = pa.Table.from_pylist(part, schema=schema)
        d = Path(root) / f"day_cn={day}"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"part-{tag}.parquet"
        if path.exists():
            raise FileExistsError(f"产出只追加，不覆盖已有文件：{path}")
        tmp = d / f".part-{tag}.parquet.tmp"
        pq.write_table(table, tmp)
        tmp.replace(path)
        written.append(path)
    return written


def read_rows(root: Path, columns: Iterable[str] | None = None) -> list[dict]:
    files = parquet_files(root)
    if not files:
        return []
    cols = list(columns) if columns is not None else None
    tables = [pq.read_table(f, columns=cols) for f in files]
    return pa.concat_tables(tables, promote_options="default").to_pylist()


def glob_of(root: Path) -> str:
    """DuckDB read_parquet 用的通配路径。"""
    return str(Path(root) / "**" / "*.parquet")
