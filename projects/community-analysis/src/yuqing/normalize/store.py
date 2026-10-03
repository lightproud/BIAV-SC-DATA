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


def duckdb_connect(data_root: Path, memory_limit: str = "6GB"):
    """全量运行用的 DuckDB 连接：限内存、允许溢写到 DATA_ROOT/.tmp（不进 git）。"""
    import duckdb

    tmp = Path(data_root) / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(
        config={"memory_limit": memory_limit, "temp_directory": str(tmp), "preserve_insertion_order": False}
    )


class StagedWriter:
    """边算边分批写临时文件，最后由 DuckDB 按 day_cn 一次分区写出：内存只留一批，每天每次运行一个文件。

    文件名 `part-<标签>-<序号>.parquet`；标签由全部主键的摘要决定，同输入同标签；已有同标签文件就拒绝（只追加）。
    """

    def __init__(self, root: Path, schema: pa.Schema, key: str, order: str, chunk_rows: int = 500_000) -> None:
        self.root = Path(root)
        self.schema = schema
        self.key = key
        self.order = order
        self.chunk_rows = chunk_rows
        self.buf: list[dict] = []
        self.n = 0
        self._digest = hashlib.sha256()
        self.stage = self.root.parent / f".stage-{self.root.name}"
        self._chunks = 0

    def add(self, row: dict) -> None:
        self.buf.append(row)
        self._digest.update(str(row[self.key]).encode("utf-8") + b"\x1f")
        if len(self.buf) >= self.chunk_rows:
            self._flush()

    def _flush(self) -> None:
        if not self.buf:
            return
        self.stage.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(self.buf, schema=self.schema)
        pq.write_table(table, self.stage / f"chunk-{self._chunks:05d}.parquet")
        self._chunks += 1
        self.n += len(self.buf)
        self.buf = []

    def close(self, con, tag_prefix: str) -> int:
        """写出并清掉暂存；返回写出的行数。"""
        import shutil

        self._flush()
        if not self.n:
            return 0
        tag = short_hash(tag_prefix, self._digest.hexdigest())
        if any(self.root.glob(f"day_cn=*/part-{tag}-*.parquet")):
            raise FileExistsError(f"产出只追加，不覆盖已有文件：{self.root} 标签 {tag}")
        self.root.mkdir(parents=True, exist_ok=True)
        src = f"read_parquet('{self.stage}/*.parquet')"
        con.execute(
            f"COPY (SELECT * FROM {src} ORDER BY {self.order}) TO '{self.root}' (FORMAT parquet, "
            f"PARTITION_BY (day_cn), WRITE_PARTITION_COLUMNS true, FILENAME_PATTERN 'part-{tag}-{{i}}', "
            "OVERWRITE_OR_IGNORE true)"
        )
        shutil.rmtree(self.stage)
        return self.n
