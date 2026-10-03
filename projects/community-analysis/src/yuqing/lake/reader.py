"""逐条读数据湖记录。优先复用数据仓的统一读取接口 archive_layout.open_archive_text。"""

from __future__ import annotations

import gzip
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from yuqing.lake.layout import LakeFile

_LAYOUT_REL = Path("projects/news/scripts/archive_layout.py")
_LAKE_SUFFIX = ("Record", "Community")
BUILTIN_READER = "内置读取器（裸文本与 .gz 透明双开）"
_CONTAINER_KEYS = ("items", "data", "records", "list", "reviews", "comments", "posts", "messages")


def _builtin_open(path: Path) -> TextIO:
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


class LakeReader:
    """开档器：找得到数据仓的 archive_layout 就用它，否则用内置的同等实现。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._open = _builtin_open
        self.source = BUILTIN_READER
        layout = self._find_layout()
        if layout is not None:
            mod = self._load(layout)
            if mod is not None and hasattr(mod, "open_archive_text"):
                self._open = mod.open_archive_text
                self.source = "数据仓 archive_layout.open_archive_text"

    def _find_layout(self) -> Path | None:
        """只认数据仓的标准布局：数据湖正是 <仓库>/Record/Community 时，复用该仓的接口。"""
        here = self.root.resolve()
        if here.parts[-2:] != _LAKE_SUFFIX:
            return None
        cand = here.parent.parent / _LAYOUT_REL
        return cand if cand.is_file() else None

    @staticmethod
    def _load(path: Path):
        try:
            spec = importlib.util.spec_from_file_location("_lake_archive_layout", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:  # 数据仓接口加载失败就退回内置实现，不中断普查
            return None
        return mod

    def records(self, f: LakeFile) -> Iterator[tuple[int, dict]]:
        """产出 (行号或序号, 记录)。jsonl 行号从 1 起；json 文档按列表下标。"""
        path = self.root / f.rel
        with self._open(path) as fh:
            if f.fmt == "jsonl":
                for no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        yield no, {"__bad__": True}
                        continue
                    if isinstance(rec, dict):
                        yield no, rec
                return
            try:
                doc = json.load(fh)
            except json.JSONDecodeError:
                yield 0, {"__bad__": True}
                return
        for i, rec in enumerate(extract_items(doc)):
            yield i, rec


def extract_items(doc: object) -> list[dict]:
    """JSON 文档里的条目：顶层列表，或字典里第一个「字典列表」值；都没有则把字典本身当一条。"""
    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)]
    if isinstance(doc, dict):
        for key in sorted(doc):
            val = doc[key]
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                return val
        for key in _CONTAINER_KEYS:
            if isinstance(doc.get(key), list):
                return []  # 容器键是空列表：当天无条目
        return [doc]
    return []
