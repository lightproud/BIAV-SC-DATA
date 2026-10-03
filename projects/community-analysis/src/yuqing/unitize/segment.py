"""切段算法本身（不碰文件）：一条聊天流 → 若干段；评论、帖子、评价一条一段。

- 正文只收 route = annotate 的消息；相邻两条正文间隔超过 gap，或正文已满 max_msgs，就开新段。
- 只读上下文（ctx_msg_ids）三个来源：同一流前一段正文的最后 overlap 条；正文回复的、不在本段正文里的那条；
  落在本段时间范围内（或紧贴段前段后、不超过 gap）的 context_only 消息（Q9），每段最多 inline_max 条。
  上下文不算正文、不占 max_msgs 名额，也不参与作者计数。
- 连续发言（守密人 2026-10-03）：同一人连发、中间没有别人的正文、相邻不超过 turn_gap 秒，算同一「句」；
  max_msgs 按句计，一句不会被切到两段。turn_gap 为 0 时不合并，一条一句。
- 冷门频道防碎（守密人 2026-10-03）：
  回复关系——一条正文回复的是当前段里的消息，reply_window 秒内都不切开；
  碎片并回——一段只有 attach_max_turns 句以内、离前一段不超过 attach 秒，就并进前一段（前一段并后不超过 max_msgs 句）。
  两项为 0 时关闭，同旧行为。
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
    text_len: int = 0


@dataclass
class Seg:
    body: list[Msg] = field(default_factory=list)
    ctx: list[str] = field(default_factory=list)
    turns: list[int] = field(default_factory=list)  # 与 body 等长：每条正文属于段内第几句

    @property
    def n_turns(self) -> int:
        return (self.turns[-1] + 1) if self.turns else len(self.body)

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
    turn_gap_s: float = 0,
    reply_window_s: float = 0,
    attach_s: float = 0,
    attach_max_turns: int = 2,
) -> list[Seg]:
    """msgs 须是同一条流、按 (t, msg_id) 排好；t 为空的正文各自成段（排在最后）。"""
    body = [m for m in msgs if m.route == ANNOTATE and m.t is not None]
    segs: list[Seg] = []
    cur_ids: set[str] = set()

    def absorb_fragment() -> None:
        """当前最后一段若是碎片、离前一段够近，就并进前一段。"""
        if attach_s <= 0 or len(segs) < 2:
            return
        prev, frag = segs[-2], segs[-1]
        if frag.n_turns > attach_max_turns or frag.start - prev.end > attach_s:
            return
        if prev.n_turns + frag.n_turns > max_msgs:
            return
        base = prev.n_turns
        prev.body += frag.body
        prev.turns += [base + k for k in frag.turns]
        segs.pop()

    for m in body:
        cur = segs[-1] if segs else None
        near = cur is not None and m.t - cur.end <= gap_s
        replying = (
            cur is not None
            and reply_window_s > 0
            and m.reply_to is not None
            and m.reply_to in cur_ids
            and m.t - cur.end <= reply_window_s
        )
        if near or replying:
            last = cur.body[-1]
            same_turn = (
                turn_gap_s > 0 and m.author is not None and m.author == last.author and m.t - last.t <= turn_gap_s
            )
            if same_turn:
                cur.body.append(m)
                cur.turns.append(cur.turns[-1])
                cur_ids.add(m.msg_id)
                continue
            if cur.n_turns < max_msgs:
                cur.body.append(m)
                cur.turns.append(cur.turns[-1] + 1)
                cur_ids.add(m.msg_id)
                continue
        absorb_fragment()
        segs.append(Seg(body=[m], turns=[0]))
        cur_ids = {m.msg_id}
    absorb_fragment()
    timed = len(segs)
    segs += [Seg(body=[m], turns=[0]) for m in msgs if m.route == ANNOTATE and m.t is None]

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
