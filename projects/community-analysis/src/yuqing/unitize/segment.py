"""切段算法本身（不碰文件）：一条聊天流 → 若干段；评论、帖子、评价一条一段。

- 正文只收 route = annotate 的消息；相邻两条正文间隔超过 gap，或正文已满 max_msgs，就开新段。
- 只读上下文（ctx_msg_ids）三个来源：同一流前一段正文的最后 overlap 条；正文回复的、不在本段正文里的那条；
  落在本段时间范围内（或紧贴段前段后、不超过 gap）的 context_only 消息（Q9），每段最多 inline_max 条。
  上下文不算正文、不占 max_msgs 名额，也不参与作者计数。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

ANNOTATE, CONTEXT = "annotate", "context_only"


@dataclass(frozen=True)
class Msg:
    msg_id: str
    t: float | None
    route: str
    author: str | None
    reply_to: str | None
    lang: str | None
    day_cn: object


@dataclass
class Seg:
    body: list[Msg] = field(default_factory=list)
    ctx: list[str] = field(default_factory=list)

    @property
    def start(self) -> float | None:
        return self.body[0].t

    @property
    def end(self) -> float | None:
        return self.body[-1].t


def _add_ctx(seg: Seg, ids: Iterable[str], body_ids: set[str]) -> None:
    seen = set(seg.ctx)
    for i in ids:
        if i not in body_ids and i not in seen:
            seg.ctx.append(i)
            seen.add(i)


def segment_chat(
    msgs: list[Msg],
    gap_s: float,
    max_msgs: int,
    overlap: int,
    inline_max: int,
    reply_ok: set[str] | frozenset[str] = frozenset(),
) -> list[Seg]:
    """msgs 须是同一条流、按 (t, msg_id) 排好；t 为空的正文各自成段（排在最后）。"""
    body = [m for m in msgs if m.route == ANNOTATE and m.t is not None]
    segs: list[Seg] = []
    for m in body:
        cur = segs[-1] if segs else None
        if cur is None or m.t - cur.end > gap_s or len(cur.body) >= max_msgs:
            segs.append(Seg())
        segs[-1].body.append(m)
    timed = len(segs)
    segs += [Seg(body=[m]) for m in msgs if m.route == ANNOTATE and m.t is None]

    # 段内（及紧贴段前段后）的 context_only 消息
    inline: list[list[str]] = [[] for _ in range(timed)]
    starts = [s.start for s in segs[:timed]]
    for m in msgs:
        if m.route != CONTEXT or m.t is None or not timed:
            continue
        i = bisect_right(starts, m.t) - 1
        if i >= 0 and m.t <= segs[i].end:
            k = i
        elif i + 1 < timed and starts[i + 1] - m.t <= gap_s:
            k = i + 1
        elif i >= 0 and m.t - segs[i].end <= gap_s:
            k = i
        else:
            continue
        if len(inline[k]) < inline_max:
            inline[k].append(m.msg_id)

    for k, seg in enumerate(segs):
        body_ids = {m.msg_id for m in seg.body}
        if 0 < k < timed and overlap > 0:
            _add_ctx(seg, (m.msg_id for m in segs[k - 1].body[-overlap:]), body_ids)
        _add_ctx(seg, (m.reply_to for m in seg.body if m.reply_to and m.reply_to in reply_ok), body_ids)
        if k < timed:
            _add_ctx(seg, inline[k], body_ids)
    return segs


def unit_lang(body: list[Msg]) -> str | None:
    c = Counter(m.lang for m in body if m.lang)
    if not c:
        return None
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def n_authors(body: list[Msg]) -> int:
    return len({m.author for m in body if m.author is not None})
