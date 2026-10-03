"""从各平台记录里认出「文本 / 作者 / 时间 / 回复 / 机器人 / 原生 ID / 附件」字段，并做文本粗分类。

只做发现，不做规范化（规范化在 T10）。候选键按优先序排列，取第一个有值的。
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

TEXT_KEYS = ("content", "text", "review", "body", "message", "comment", "description", "desc", "title")
AUTHOR_KEYS = (
    "author_id", "steamid", "user_id", "uid", "author", "user", "author_name",
    "username", "user_name", "screen_name", "nickname",
)  # fmt: skip
_AUTHOR_INNER = ("id", "steamid", "uid", "mid", "user_id", "name", "screen_name", "nickname")
TIME_KEYS = (
    "timestamp", "timestamp_created", "created_at", "published_at", "publishedAt",
    "time", "date", "created", "pubDate", "ts",
)  # fmt: skip
SPECIAL_KEYS: dict[str, tuple[str, ...]] = {
    "reply": (
        "reply_to", "referenced_message", "message_reference", "in_reply_to",
        "in_reply_to_status_id", "reply_to_id", "parent_id", "parent",
    ),
    "bot": ("author_bot", "bot", "is_bot"),
    "native_id": (
        "id", "message_id", "msg_id", "recommendationid", "review_id", "post_id",
        "comment_id", "tweet_id", "bvid", "rpid", "mid",
    ),
    "attachment": ("attachments", "images", "image", "pics", "media", "stickers", "sticker_items"),
}  # fmt: skip
SPECIAL_LABELS = {
    "reply": "回复指向",
    "bot": "机器人标记",
    "native_id": "平台原生消息 ID",
    "attachment": "附件",
}

# discord 紧凑 schema：type 0 = 普通消息，19 = 回复；其余为系统消息（入群、置顶等）
_DISCORD_NORMAL_TYPES = {0, 19}

_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>|:[\w+-]+:")


def is_filled(v: object) -> bool:
    return v not in (None, "", [], {}, False)


def first_filled(rec: dict, keys: tuple[str, ...]) -> object:
    for k in keys:
        v = rec.get(k)
        if is_filled(v):
            return v
    return None


def get_text(rec: dict) -> str:
    v = first_filled(rec, TEXT_KEYS)
    return v if isinstance(v, str) else ""


def get_author(rec: dict) -> str | None:
    """作者标识（只在内存里用来去重计数，不落盘不打印）。"""
    v = first_filled(rec, AUTHOR_KEYS)
    if isinstance(v, dict):
        v = first_filled(v, _AUTHOR_INNER)
    if v is None or isinstance(v, (dict, list)):
        return None
    return str(v)


def has_attachment(rec: dict) -> bool:
    return first_filled(rec, SPECIAL_KEYS["attachment"]) is not None


def is_bot(rec: dict) -> bool:
    if first_filled(rec, SPECIAL_KEYS["bot"]) is True:
        return True
    author = rec.get("author")
    return isinstance(author, dict) and author.get("bot") is True


def is_system(rec: dict) -> bool:
    t = rec.get("type")
    return isinstance(t, int) and not isinstance(t, bool) and t not in _DISCORD_NORMAL_TYPES


def parse_ts(rec: dict) -> datetime | None:
    """时间统一成 UTC。无时区的字符串按 UTC 理解（口径写进报告）。"""
    v = first_filled(rec, TIME_KEYS)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000 if v > 1e12 else v
        if secs < 1e9:
            return None
        return datetime.fromtimestamp(secs, tz=UTC)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return parse_ts({"ts": int(s)})
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return None


def day_cn(dt: datetime) -> str:
    return dt.astimezone(CN_TZ).date().isoformat()


def _char_class(ch: str) -> str | None:
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "kana"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
        return "han"
    if 0x0400 <= o <= 0x04FF:
        return "cyrillic"
    if 0x0E00 <= o <= 0x0E7F:
        return "thai"
    if 0x0600 <= o <= 0x06FF:
        return "arabic"
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or 0x00C0 <= o <= 0x024F:
        return "latin"
    return None


def script_profile(text: str) -> tuple[str, int]:
    """(按文字脚本粗分的语言, 粗估 token)。汉字、假名、谚文按 1，其余每 4 个字符按 1。"""
    counts: dict[str, int] = {}
    for ch in text:
        c = _char_class(ch)
        if c:
            counts[c] = counts.get(c, 0) + 1
    cjk = counts.get("han", 0) + counts.get("kana", 0) + counts.get("hangul", 0)
    tokens = cjk + math.ceil((len(text) - cjk) / 4)
    if not counts:
        return "none", tokens
    han_kana = counts.get("han", 0) + counts.get("kana", 0)
    ranked = {
        "ko": counts.get("hangul", 0),
        "ja" if counts.get("kana", 0) else "zh": han_kana,
        "latin": counts.get("latin", 0),
        "cyrillic": counts.get("cyrillic", 0),
        "thai": counts.get("thai", 0),
        "arabic": counts.get("arabic", 0),
    }
    label = max(ranked, key=lambda k: (ranked[k], k))
    return label, tokens


def is_emoji_only(text: str) -> bool:
    """非空，且去掉表情、贴纸代码、标点、符号、空白后什么都不剩。"""
    if not text.strip():
        return False
    rest = _CUSTOM_EMOJI_RE.sub("", text)
    for ch in rest:
        cat = unicodedata.category(ch)
        if cat[0] in "PSZ" or cat in ("Mn", "Me", "Cf", "Cc"):
            continue
        return False
    return True


def json_type(v: object) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__
