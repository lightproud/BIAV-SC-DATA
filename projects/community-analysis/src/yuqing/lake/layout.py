"""发现数据湖布局：遍历 LAKE_ROOT，列出平台、目录层级、文件格式。只读。

已知布局（数据仓 archive_layout 为准，这里只做发现、不硬编码平台表）：
- discord：`discord/{区服}/channels/{频道}/{YYYY-MM-DD}.jsonl[.gz]`，每行一条
- 其他平台：`{平台}[/{区服}][/{类型}]/{YYYY-MM-DD}.json[.gz]`，每档一个 JSON 文档
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(jsonl|json)(\.gz)?$")
_ID_SEG_RE = re.compile(r"^\d{3,}$")
# 非消息档的目录（统计快照等），整棵跳过
META_DIRS = frozenset({"activity_daily"})
# 类型层 → 消息种类；无类型层的非聊天档按 post 计
KIND_BY_SUBTYPE = {"review": "review", "comments": "comment", "comment": "comment"}


@dataclass(frozen=True)
class LakeFile:
    rel: str  # 相对 LAKE_ROOT 的 posix 路径
    platform: str
    scope: str  # 所在目录（聊天类即频道目录）
    channel: str | None
    kind: str  # chat / comment / post / review
    fmt: str  # jsonl / json
    gz: bool
    day: str  # 文件名上的日期


@dataclass
class Discovery:
    files: list[LakeFile] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # 原因 → 文件数
    layouts: dict[str, Counter] = field(default_factory=dict)  # 平台 → 抽象路径模式 → 文件数


def _pattern(parts: list[str], fmt: str, gz: bool) -> str:
    segs = ["{id}" if _ID_SEG_RE.match(p) else p for p in parts[1:-1]]
    return "/".join([*segs, f"{{date}}.{fmt}{'.gz' if gz else ''}"])


def classify(rel_parts: list[str]) -> tuple[str, str | None]:
    """(种类, 频道)。路径里有 channels 层即聊天类，下一层是频道。"""
    if "channels" in rel_parts[:-1]:
        i = rel_parts.index("channels")
        channel = rel_parts[i + 1] if i + 1 < len(rel_parts) - 1 else None
        return "chat", channel
    for seg in rel_parts[1:-1]:
        if seg in KIND_BY_SUBTYPE:
            return KIND_BY_SUBTYPE[seg], None
    return "post", None


def discover(root: Path) -> Discovery:
    root = Path(root)
    found = Discovery()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root)
        skipped_meta = [d for d in dirnames if d in META_DIRS]
        for d in skipped_meta:
            n = sum(len(fs) for _, _, fs in os.walk(Path(dirpath) / d))
            found.skipped[f"统计快照目录 {d}/"] += n
        dirnames[:] = [d for d in dirnames if d not in META_DIRS]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            parts = [*rel_dir.parts, name]
            m = DATE_FILE_RE.match(name)
            if len(parts) < 2:
                found.skipped["根目录散档"] += 1
                continue
            if not m:
                found.skipped["非日期档（索引 / 状态等）"] += 1
                continue
            day, fmt, gz = m.group(1), m.group(2), bool(m.group(3))
            kind, channel = classify(parts)
            platform = parts[0]
            found.files.append(
                LakeFile(
                    rel="/".join(parts),
                    platform=platform,
                    scope="/".join(parts[:-1]),
                    channel=channel,
                    kind=kind,
                    fmt=fmt,
                    gz=gz,
                    day=day,
                )
            )
            found.layouts.setdefault(platform, Counter())[_pattern(parts, fmt, gz)] += 1
    found.files.sort(key=lambda f: (f.platform, f.scope, f.day, f.rel))
    return found
