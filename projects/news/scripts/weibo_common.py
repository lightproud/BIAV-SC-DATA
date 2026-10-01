"""Weibo measured counts; numeric engagement remains a legacy sorting score."""
import re


def parse_count(value):
    """Return None for absent/unrecognizable counts; a visible zero is real zero."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"(?:转发|轉發|评论|評論|赞|讚|点赞)?\s*(\d+(?:\.\d+)?)\s*([万萬亿億kKmM]?)\+?", text)
    if not match:
        return None
    scales = {"万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000,
              "k": 1000, "m": 1000000}
    return int(float(match[1]) * scales.get(match[2].lower(), 1))


def metrics(reposts=None, comments=None, likes=None):
    raw = {"reposts_count": reposts, "comments_count": comments, "likes_count": likes}
    counts = {key: parse_count(value) for key, value in raw.items()}
    complete = all(value is not None for value in counts.values())
    total = sum(counts.values()) if complete else None
    # Shared validation, dedup and archive sorting require a numeric engagement.
    # Keep that compatibility score separate from the nullable measured total.
    score = sum(value for value in counts.values() if value is not None)
    approximate = any(value is not None and re.search(r"[万萬亿億kKmM+]", str(value)) for value in raw.values())
    return score, {
        **counts,
        "engagement_total": total,
        "engagement_is_unknown": not complete,
        "engagement_basis": "measured_total" if complete else "known_counts_sort_fallback",
        "counts_are_approximate": approximate,
    }
