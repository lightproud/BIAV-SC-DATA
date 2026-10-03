"""一条原文记录 → messages 表的一行。

字段按数据契约 `messages`；另加四个后面几层要用、原文里又只有这时能取到的列：
`parent_text`（父帖标题或本帖与正文不同的标题，T12 填 ctx_text）、`bot_flag`、`msg_type`（T11 判机器人与系统消息）、
`norm_ver`。作者名只在这里读一次，立刻换成 HMAC 哈希，不进任何一列。
"""

from __future__ import annotations

import hashlib
import hmac
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa

from yuqing.census import fields as F
from yuqing.lake.layout import LakeFile
from yuqing.normalize.store import short_hash

NORM_VER = "2"  # 2：没有原生 ID 的条目改用内容键（2026-10-03）

MESSAGES_SCHEMA = pa.schema(
    [
        ("msg_id", pa.string()),
        ("platform", pa.string()),
        ("community", pa.string()),
        ("server", pa.string()),
        ("channel", pa.string()),
        ("kind", pa.string()),
        ("author_hash", pa.string()),
        ("ts_utc", pa.timestamp("us", tz="UTC")),
        ("day_cn", pa.date32()),
        ("text", pa.string()),
        ("lang", pa.string()),
        ("has_image", pa.bool_()),
        ("reply_to", pa.string()),
        ("parent_ref", pa.string()),
        ("raw_ref", pa.string()),
        ("parent_text", pa.string()),
        ("bot_flag", pa.bool_()),
        ("msg_type", pa.int32()),
        ("norm_ver", pa.string()),
    ]
)

# Q8：先认 author_id（含 metadata.author_id），再认 author；其余候选键沿用普查的发现顺序
_AUTHOR_FIRST = ("author_id",)
_SCRIPT_LANG = {"zh": "zh", "ja": "ja", "ko": "ko", "cyrillic": "ru", "thai": "th", "arabic": "ar"}
# 有附件字段的平台：字段缺省即「没有附件」（紧凑存法），能判断；其余平台判断不了，留空
_ATTACHMENT_PLATFORMS = frozenset({"discord"})
_STICKER_KEYS = ("stickers", "sticker_items")


def author_hash(salt: str, platform: str, ident: str) -> str:
    mac = hmac.new(salt.encode("utf-8"), f"{platform}\x1f{ident}".encode(), hashlib.sha256)
    return mac.hexdigest()[:16]


def msg_id_of(platform: str, native_id: str) -> str:
    return short_hash("msg", platform, native_id)


def msg_id_of_ref(raw_ref: str) -> str:
    return short_hash("ref", raw_ref)


# 没有 URL、也没有作者时，正文至少这么长才用内容键（短句不同人发的可能撞上）
CONTENT_KEY_MIN_CHARS = 10


def content_key(rec: dict, text: str, ident: str | None) -> str | None:
    """没有原生 ID 的条目用「URL + 作者 + 正文」认同一条，不含时间。

    新闻体例的平台会把同一帖跨天反复采进来（微博的 time 还是采集时间），
    而同一个 URL 下可能挂着多条不同的评价（商店页、TapTap），所以只用 URL 或只用路径都不对。
    认不准的（正文与 URL 都空；没 URL 没作者且正文太短）返回 None，退回 raw_ref。
    """
    url = rec.get("url")
    url = url.strip() if isinstance(url, str) else ""
    body = text.strip()
    if not body and not url:
        return None
    if not url and ident is None and len(body) < CONTENT_KEY_MIN_CHARS:
        return None
    return "\x1f".join((url, ident or "", body))


def author_ident(rec: dict) -> str | None:
    """作者标识（只在内存里用来算哈希）。"""
    for k in _AUTHOR_FIRST:
        v = rec.get(k)
        if F.is_filled(v) and not isinstance(v, (dict, list)):
            return str(v)
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        if meta.get("author_is_unknown") is True:
            return None
        v = meta.get("author_id")
        if F.is_filled(v) and not isinstance(v, (dict, list)):
            return str(v)
    return F.get_author(rec)


def native_id(rec: dict) -> str | None:
    v = F.first_filled(rec, F.SPECIAL_KEYS["native_id"])
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    return str(v)


def reply_native(rec: dict) -> str | None:
    v = F.first_filled(rec, F.SPECIAL_KEYS["reply"])
    if isinstance(v, dict):
        v = v.get("message_id") or v.get("id")
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    return str(v)


def raw_ref_of(f: LakeFile, no: int) -> str:
    """jsonl 记行号（从 1 起），json 文档记条目下标。"""
    return f"{f.rel}:L{no}" if f.fmt == "jsonl" else f"{f.rel}#{no}"


@dataclass(frozen=True)
class CommunityRule:
    platform: str
    region: str | None
    community: str
    server: str | None


class Communities:
    def __init__(self, rules: list[CommunityRule]) -> None:
        self.rules = rules

    @classmethod
    def load(cls, path: Path) -> Communities:
        with Path(path).open("rb") as fh:
            doc = tomllib.load(fh)
        rules = [
            CommunityRule(r["platform"], r.get("region"), r["community"], r.get("server") or None)
            for r in doc.get("rule", [])
        ]
        return cls(rules)

    def lookup(self, f: LakeFile) -> tuple[str, str | None]:
        parts = f.rel.split("/")
        region = parts[1] if len(parts) > 2 else None
        for r in self.rules:
            if r.platform == f.platform and (r.region is None or r.region == region):
                return r.community, r.server
        return "unknown", None


def _channel(f: LakeFile) -> str | None:
    if f.kind == "chat":
        return f.channel
    rest = f.scope.split("/")[1:]
    return "/".join(rest) or None


def _lang(rec: dict, text: str) -> str | None:
    for k in ("lang", "language"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    label, _ = F.script_profile(text)
    return _SCRIPT_LANG.get(label)


def _has_image(platform: str, rec: dict) -> bool | None:
    if platform not in _ATTACHMENT_PLATFORMS:
        return None
    return F.has_attachment(rec) or any(F.is_filled(rec.get(k)) for k in _STICKER_KEYS)


def _parent(rec: dict, text: str) -> tuple[str | None, str | None]:
    """(parent_ref, parent_text)。只认原文里写明的父级，不猜。"""
    if F.is_filled(rec.get("thread_id")):
        title = rec.get("thread_title")
        return f"thread:{rec['thread_id']}", title if isinstance(title, str) and title.strip() else None
    if F.is_filled(rec.get("video_id")):
        title = rec.get("video_title")
        return f"video:{rec['video_id']}", title if isinstance(title, str) and title.strip() else None
    title = rec.get("title")
    if isinstance(title, str) and title.strip() and title.strip() != text.strip():
        stem = title.strip().rstrip(".…").strip()
        if stem and not text.strip().startswith(stem):
            return None, title.strip()
    return None, None


def to_row(f: LakeFile, no: int, rec: dict, salt: str, communities: Communities) -> dict:
    platform = f.platform
    ref = raw_ref_of(f, no)
    nid = native_id(rec)
    text = F.get_text(rec)
    dt: datetime | None = F.parse_ts(rec)
    day = F.day_cn(dt) if dt else f.day
    ident = author_ident(rec)
    if nid is not None:
        msg_id = msg_id_of(platform, nid)
    else:
        key = content_key(rec, text, ident)
        msg_id = short_hash("content", platform, _channel(f) or "", key) if key is not None else msg_id_of_ref(ref)
    community, server = communities.lookup(f)
    rep = reply_native(rec)
    parent_ref, parent_text = _parent(rec, text)
    mtype = rec.get("type")
    return {
        "msg_id": msg_id,
        "platform": platform,
        "community": community,
        "server": server,
        "channel": _channel(f),
        "kind": f.kind,
        "author_hash": author_hash(salt, platform, ident) if ident is not None else None,
        "ts_utc": dt,
        "day_cn": datetime.strptime(day, "%Y-%m-%d").date(),  # noqa: DTZ007 只取日期
        "text": text,
        "lang": _lang(rec, text),
        "has_image": _has_image(platform, rec),
        "reply_to": msg_id_of(platform, rep) if rep is not None else None,
        "parent_ref": parent_ref,
        "raw_ref": ref,
        "parent_text": parent_text,
        "bot_flag": True if F.is_bot(rec) else None,
        "msg_type": mtype if isinstance(mtype, int) and not isinstance(mtype, bool) else None,
        "norm_ver": NORM_VER,
    }
