"""T12 切段：间隔、条数上限、重叠、回复拉入、封口；context_only 只进上下文（Q9）；
性质测试（每条送标的聊天消息恰好属于一个单元正文）；同参数重跑编号稳定，改参数产生新 unit_ver 且旧分区不动。

样本是测试里现造的合成消息，不含真实玩家原文。
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from yuqing import cli
from yuqing.config import load_config
from yuqing.normalize.clean import clean
from yuqing.normalize.messages import MESSAGES_SCHEMA
from yuqing.normalize.run import messages_root
from yuqing.normalize.store import parquet_files, read_rows, write_partitioned
from yuqing.unitize.run import unit_id_of, unit_ver_of, unitize, units_root
from yuqing.unitize.segment import ANNOTATE, CONTEXT, Msg, segment_chat

T0 = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
PARAMS = load_config(environ={}).params
UP = PARAMS["unitize"]
GAP = UP["gap_minutes"] * 60


def mk(i: int, t: float | None, route: str = ANNOTATE, author: str = "a", reply_to: str | None = None) -> Msg:
    return Msg(f"m{i:04d}", t, route, author, reply_to, None, date(2026, 9, 1))


def ids(seg) -> list[str]:
    return [m.msg_id for m in seg.body]


# ── 算法层 ──


def test_gap_rule():
    msgs = [mk(0, 0), mk(1, GAP), mk(2, 2 * GAP + 1)]  # 间隔恰等于 gap 不切，超过才切
    segs = segment_chat(msgs, GAP, 30, 5, 30)
    assert [ids(s) for s in segs] == [["m0000", "m0001"], ["m0002"]]


def test_max_msgs():
    msgs = [mk(i, i) for i in range(7)]
    segs = segment_chat(msgs, GAP, 3, 0, 30)
    assert [len(s.body) for s in segs] == [3, 3, 1]


def test_overlap_goes_to_ctx_not_body():
    msgs = [mk(i, i) for i in range(5)] + [mk(9, 10_000)]
    segs = segment_chat(msgs, GAP, 30, 2, 30)
    assert segs[1].ctx == ["m0003", "m0004"]
    assert ids(segs[1]) == ["m0009"] and segs[0].ctx == []


def test_reply_pulled_into_ctx_only_if_outside_body_and_exists():
    msgs = [mk(0, 0), mk(1, 10_000, reply_to="m0000"), mk(2, 10_010, reply_to="m0001"), mk(3, 10_020, reply_to="x")]
    segs = segment_chat(msgs, GAP, 30, 0, 30, reply_ok={"m0000", "m0001"})
    assert segs[1].ctx == ["m0000"]  # 本段正文里已有的 m0001 不重复；不存在的 x 不拉


def test_context_only_messages_join_ctx_without_taking_slots():
    msgs = [mk(0, 0), mk(1, 5, CONTEXT), mk(2, 10), mk(3, 12, CONTEXT), mk(4, 20), mk(5, 20 + GAP - 1, CONTEXT)]
    segs = segment_chat(msgs, GAP, 2, 0, 30)  # 正文上限 2：上下文不占名额
    assert [ids(s) for s in segs] == [["m0000", "m0002"], ["m0004"]]
    assert segs[0].ctx == ["m0001"]
    assert set(segs[1].ctx) == {"m0003", "m0005"}  # 段与段之间：归下一段；段后紧贴：归本段
    far = segment_chat([mk(0, 0), mk(1, 10 * GAP, CONTEXT)], GAP, 30, 0, 30)
    assert far[0].ctx == []  # 离得太远的不收


def test_inline_ctx_capped():
    msgs = [mk(0, 0)] + [mk(i, i, CONTEXT) for i in range(1, 50)] + [mk(99, 60)]
    segs = segment_chat(msgs, GAP, 30, 0, 10)
    assert len(segs) == 1 and len(segs[0].ctx) == 10


def test_property_each_annotate_message_in_exactly_one_body():
    rng = random.Random(7)
    for trial in range(30):
        t, msgs = 0.0, []
        for i in range(rng.randrange(1, 400)):
            t += rng.choice([1, 5, 30, 90, 200, 1000])
            msgs.append(mk(i, t, rng.choice([ANNOTATE, ANNOTATE, CONTEXT]), rng.choice("abcde")))
        if trial % 5 == 0:
            msgs.append(mk(999, None))
        segs = segment_chat(msgs, GAP, rng.choice([1, 3, 30]), rng.choice([0, 5]), 30)
        seen = [m.msg_id for s in segs for m in s.body]
        want = [m.msg_id for m in msgs if m.route == ANNOTATE]
        assert sorted(seen) == sorted(want) and len(seen) == len(set(seen))
        for s in segs:
            assert not {m.msg_id for m in s.body} & set(s.ctx)
            ts = [m.t for m in s.body if m.t is not None]
            assert all(b - a <= GAP for a, b in pairwise(ts))


# ── 端到端：messages → clean → units ──

_seq = iter(range(10**6))


def row(author, text, sec, *, channel="c1", kind="chat", reply_to=None, parent_ref=None, parent_text=None) -> dict:
    n = next(_seq)
    ts = T0 + timedelta(seconds=sec)
    return {
        "msg_id": f"r{n:05d}",
        "platform": "discord" if kind == "chat" else "steam",
        "community": "global",
        "server": "global",
        "channel": channel,
        "kind": kind,
        "author_id": author,
        "ts_utc": ts,
        "day_cn": (ts + timedelta(hours=8)).date(),
        "text": text,
        "lang": "zh",
        "has_image": False,
        "reply_to": reply_to,
        "parent_ref": parent_ref,
        "parent_text": parent_text,
        "raw_ref": f"fake/{channel}.jsonl:L{n}",
        "bot_flag": None,
        "msg_type": None,
        "norm_ver": "1",
    }


def build(tmp_path, rows):
    data = tmp_path / "data"
    write_partitioned(messages_root(data), rows, MESSAGES_SCHEMA, "a")
    cv = clean(data, PARAMS).clean_ver
    return data, cv


def units_of(res) -> list[dict]:
    return sorted(read_rows(res.root), key=lambda u: (u["kind"], u["ts_start"] or T0, u["unit_id"]))


def test_end_to_end_chat_and_comments(tmp_path):
    a = row("a", "这次活动奖励太少了", 0)
    b = row("b", "<:hero_cry:1>", 20)  # 纯表情 → context_only
    c = row("c", "同意，体力也不够用", 40, reply_to=a["msg_id"])
    bot = row("bot", "!rank", 50)  # 指令 → skip，不进任何单元
    d = row("a", "第二段开头", 40 + GAP + 600, reply_to=a["msg_id"])
    th = row("d", "讨论串里的话", 45, parent_ref="thread:77")  # 讨论串另成一条流
    long_title = "很长的父帖标题" * 40
    rv = row("e", "整体不错但是卡顿", 100, kind="review", channel="global/review", parent_text=long_title)
    data, cv = build(tmp_path, [a, b, c, bot, d, th, rv])
    res = unitize(data, UP, cv, replay=True)
    us = units_of(res)
    chat = [u for u in us if u["kind"] == "chat"]
    assert [u["msg_ids"] for u in chat] == [[a["msg_id"], c["msg_id"]], [th["msg_id"]], [d["msg_id"]]]
    assert chat[0]["ctx_msg_ids"] == [b["msg_id"]] and chat[0]["n_msgs"] == 2 and chat[0]["n_authors"] == 2
    assert chat[2]["ctx_msg_ids"] == [a["msg_id"], c["msg_id"]]  # 前一段最后几条（被回复的那条已在其中）
    assert all(bot["msg_id"] not in u["msg_ids"] + u["ctx_msg_ids"] for u in us)
    review = [u for u in us if u["kind"] == "review"]
    assert len(review) == 1 and review[0]["msg_ids"] == [rv["msg_id"]]
    assert review[0]["ctx_text"] == long_title[:200]
    assert chat[0]["unit_id"] == unit_id_of(res.unit_ver, a["msg_id"]) and chat[0]["lang"] == "zh"


def test_cross_day_unit_goes_to_first_day(tmp_path):
    # T0 是 UTC 04:00（UTC+8 12:00）：再过 11:59 是 UTC+8 当天 23:59，过 12:00:30 是次日 00:00:30
    x = row("a", "零点前一句话", 11 * 3600 + 59 * 60)
    y = row("b", "零点后一句话", 12 * 3600 + 30)
    data, cv = build(tmp_path, [x, y])
    u = units_of(unitize(data, UP, cv, replay=True))
    assert x["day_cn"] == date(2026, 9, 1) and y["day_cn"] == date(2026, 9, 2)
    assert len(u) == 1 and u[0]["day_cn"] == date(2026, 9, 1)


def test_seal_holds_back_recent_segment(tmp_path):
    rows = [row("a", "早先的一段话", 0), row("b", "最近的一段话", 3 * 3600)]
    data, cv = build(tmp_path, rows)
    now = T0 + timedelta(seconds=3 * 3600 + 60)  # 最近一段才过 1 分钟
    r1 = unitize(data, UP, cv, now=now)
    assert r1.written == 1 and r1.unsealed == 1
    r2 = unitize(data, UP, cv, now=now + timedelta(minutes=UP["seal_minutes"]))
    assert r2.written == 1 and r2.unsealed == 0
    assert len(read_rows(r2.root)) == 2
    assert unitize(data, UP, cv, replay=True).written == 0


def test_rerun_stable_and_param_change_new_version(tmp_path):
    rows = [row("a", f"第{i}句正经话", i * 30) for i in range(12)]
    data, cv = build(tmp_path, rows)
    r1 = unitize(data, UP, cv, replay=True)
    files1 = parquet_files(r1.root)
    snap = {p: p.read_bytes() for p in files1}
    ids1 = sorted(u["unit_id"] for u in read_rows(r1.root))
    r2 = unitize(data, UP, cv, replay=True)
    assert r2.unit_ver == r1.unit_ver and r2.written == 0 and parquet_files(r2.root) == files1
    other = tmp_path / "other"
    write_partitioned(messages_root(other), rows, MESSAGES_SCHEMA, "a")
    clean(other, PARAMS)
    r_other = unitize(other, UP, cv, replay=True)
    assert sorted(u["unit_id"] for u in read_rows(r_other.root)) == ids1  # 换目录重跑编号也一样
    changed = {**UP, "max_msgs": 5}
    r3 = unitize(data, changed, cv, replay=True)
    assert r3.unit_ver != r1.unit_ver and r3.unit_ver == unit_ver_of(changed, cv)
    assert [u["n_msgs"] for u in sorted(read_rows(r3.root), key=lambda u: u["ts_start"])] == [5, 5, 2]
    assert all(p.read_bytes() == b for p, b in snap.items()) and parquet_files(r1.root) == files1  # 旧分区不动
    assert unit_ver_of({**UP, "seal_minutes": 99}, cv) == r1.unit_ver  # 封口时机不改版本


def test_requires_clean_first(tmp_path):
    data = tmp_path / "data"
    write_partitioned(messages_root(data), [row("a", "一句话", 0)], MESSAGES_SCHEMA, "a")
    with pytest.raises(Exception, match="yuqing clean"):
        unitize(data, UP, "nope", replay=True)


def test_cli_unitize(tmp_path, monkeypatch, capsys):
    data, _ = build(tmp_path, [row("a", "一句正经话", 0)])
    monkeypatch.setenv("DATA_ROOT", str(data))
    assert cli.main(["unitize", "--replay"]) == 0
    assert "共 1 段" in capsys.readouterr().out
    assert units_root(data, unit_ver_of(UP, _)).is_dir()
