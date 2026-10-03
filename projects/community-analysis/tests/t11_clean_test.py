"""T11 清洗打标（按 Q9 新设计）：每条规则一组表驱动样例，含两个反例——
「不同作者相同短句不去重」「有图无字保留」。另测多标签、三档路由、只追加、noise_daily 不按作者出数。

样本是测试里现造的合成消息，不含真实玩家原文。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq
import pytest

from yuqing import cli
from yuqing.config import load_config
from yuqing.normalize.clean import (
    ROUTE_ANNOTATE,
    ROUTE_CONTEXT,
    ROUTE_SKIP,
    clean,
    clean_ver_of,
    flags_root,
    noise_root,
    norm_text,
    route_of,
)
from yuqing.normalize.messages import MESSAGES_SCHEMA, author_hash
from yuqing.normalize.run import messages_root
from yuqing.normalize.store import parquet_files, read_rows, write_partitioned

SALT = "test-salt-not-real"
T0 = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
PARAMS = load_config(environ={}).params

_seq = iter(range(10**6))


def m(author: str | None, text: str, sec: float = 0, *, channel="c1", kind="chat", has_image=False, **kw) -> dict:
    ts = T0 + timedelta(seconds=sec)
    n = next(_seq)
    row = {
        "msg_id": f"m{n:05d}",
        "platform": "discord" if kind == "chat" else "steam",
        "community": "global",
        "server": None,
        "channel": channel,
        "kind": kind,
        "author_hash": author_hash(SALT, "discord", author) if author else None,
        "ts_utc": ts,
        "day_cn": date(2026, 9, 1),
        "text": text,
        "lang": None,
        "has_image": has_image,
        "reply_to": None,
        "parent_ref": None,
        "raw_ref": f"fake/{channel}.jsonl:L{n}",
        "parent_text": None,
        "bot_flag": None,
        "msg_type": None,
        "norm_ver": "1",
    }
    row.update(kw)
    return row


def run_clean(tmp_path, rows: list[dict], bots=None) -> dict[str, dict]:
    data = tmp_path / "data"
    write_partitioned(messages_root(data), rows, MESSAGES_SCHEMA, f"t{len(list(data.rglob('*.parquet')))}")
    res = clean(data, PARAMS, SALT, bots)
    got = {r["msg_id"]: r for r in read_rows(flags_root(data, res.clean_ver))}
    return got


# (说明, 消息列表, 要检查的下标, 期望含有的标记, 期望路由)
SINGLE = [
    ("机器人字段", [m("a", "公告内容", bot_flag=True)], 0, {"bot"}, ROUTE_SKIP),
    ("系统消息", [m("a", "joined", msg_type=7)], 0, {"system"}, ROUTE_SKIP),
    ("斜杠指令回执", [m("a", "结果是 3", msg_type=20)], 0, {"bot_command"}, ROUTE_SKIP),
    ("无文字无附件", [m("a", "   ")], 0, {"empty"}, ROUTE_SKIP),
    ("有图无字：保留为上下文", [m("a", "", has_image=True)], 0, {"image_only"}, ROUTE_CONTEXT),
    ("纯表情", [m("a", "<:hero_cry:123> 😭!!")], 0, {"emoji_only"}, ROUTE_CONTEXT),
    ("极短", [m("a", "草")], 0, {"short"}, ROUTE_CONTEXT),
    ("极短（两字）", [m("a", "ok")], 0, {"short"}, ROUTE_CONTEXT),
    ("三字不算极短", [m("a", "lol")], 0, set(), ROUTE_ANNOTATE),
    ("评价里极短不拦", [m("a", "垃圾", kind="review")], 0, set(), ROUTE_ANNOTATE),
    ("仅链接", [m("a", "https://example.com/x 😂")], 0, {"link_only"}, ROUTE_SKIP),
    ("链接带话不算", [m("a", "看这个更新 https://example.com/x")], 0, set(), ROUTE_ANNOTATE),
    ("仅 @", [m("a", "<@123456> <@&99>")], 0, {"mention_only"}, ROUTE_SKIP),
    ("机器人指令", [m("a", "!rank")], 0, {"bot_command"}, ROUTE_SKIP),
    ("像指令但是话", [m("a", "/ 这活动太肝了")], 0, set(), ROUTE_ANNOTATE),
    ("长贴只标不拦", [m("a", "很长" * 260)], 0, {"long_post"}, ROUTE_ANNOTATE),
]


@pytest.mark.parametrize(("desc", "rows", "idx", "want", "route"), SINGLE, ids=[c[0] for c in SINGLE])
def test_single_message_rules(tmp_path, desc, rows, idx, want, route):
    got = run_clean(tmp_path, rows)
    f = got[rows[idx]["msg_id"]]
    assert set(f["flags"]) == want, desc
    assert f["route"] == route, desc


def test_bots_txt_list_matches_by_hash(tmp_path):
    rows = [m("bot-x", "每日签到提醒"), m("b", "每日签到提醒", 600)]
    got = run_clean(tmp_path, rows, bots=[("discord", "bot-x")])
    assert got[rows[0]["msg_id"]]["flags"] == ["bot"]
    assert got[rows[1]["msg_id"]]["route"] == ROUTE_ANNOTATE


SEQ = [
    # 同作者 24 小时内完全相同：留第一条
    ("同作者完全重复", [m("a", "什么时候修 bug", 0), m("a", "什么时候修 bug", 3600)],
     {1: {"dup_same_author"}}, {0: ROUTE_ANNOTATE, 1: ROUTE_SKIP}),
    ("超过 24 小时不算重复", [m("a", "什么时候修 bug", 0), m("a", "什么时候修 bug", 25 * 3600)],
     {1: set()}, {1: ROUTE_ANNOTATE}),
    ("不同频道不算同作者重复", [m("a", "什么时候修 bug", 0), m("a", "什么时候修 bug", 60, channel="c2")],
     {1: set()}, {1: ROUTE_ANNOTATE}),
    ("同作者近似重复", [m("a", "What a GREAT update!!", 0), m("a", "what a great update", 30)],
     {1: {"dup_near_same_author"}}, {1: ROUTE_SKIP}),
    # 反例：不同作者相同短句不去重（声量），两人不成接龙
    ("不同作者相同短句不去重", [m("a", "我也卡关了", 0), m("b", "我也卡关了", 10)],
     {0: set(), 1: set()}, {0: ROUTE_ANNOTATE, 1: ROUTE_ANNOTATE}),
    ("接龙：三人两分钟同一短句", [m("a", "新年快乐", 0), m("b", "新年快乐！", 30), m("c", "新年快乐", 90)],
     {0: {"chorus"}, 1: {"chorus"}, 2: {"chorus"}}, {0: ROUTE_CONTEXT, 2: ROUTE_CONTEXT}),
    ("三人但超出两分钟不算接龙", [m("a", "新年快乐", 0), m("b", "新年快乐", 100), m("c", "新年快乐", 250)],
     {0: set(), 2: set()}, {2: ROUTE_ANNOTATE}),
    ("连发只作属性", [m("a", f"第{i}句话", i * 10) for i in range(5)],
     {0: {"burst"}, 4: {"burst"}}, {0: ROUTE_ANNOTATE, 4: ROUTE_ANNOTATE}),
    ("四条不算连发", [m("a", f"第{i}句话", i * 10) for i in range(4)], {0: set(), 3: set()}, {}),
    ("长文转贴：留最早一条", [m("a", "转" * 80, 0), m("b", "转" * 80, 5000, channel="c2")],
     {0: set(), 1: {"dup_long_repost"}}, {0: ROUTE_ANNOTATE, 1: ROUTE_SKIP}),
    ("短于 60 字的相同文字不算转贴", [m("a", "转" * 59, 0), m("b", "转" * 59, 5000, channel="c2")],
     {1: set()}, {1: ROUTE_ANNOTATE}),
]  # fmt: skip


@pytest.mark.parametrize(("desc", "rows", "want_flags", "want_routes"), SEQ, ids=[c[0] for c in SEQ])
def test_sequence_rules(tmp_path, desc, rows, want_flags, want_routes):
    got = run_clean(tmp_path, rows)
    for i, want in want_flags.items():
        assert set(got[rows[i]["msg_id"]]["flags"]) == want, (desc, i)
    for i, route in want_routes.items():
        assert got[rows[i]["msg_id"]]["route"] == route, (desc, i)


def test_multi_label_keeps_every_hit(tmp_path):
    rows = [m("a", "!rank", 0), m("a", "!rank", 5)]
    got = run_clean(tmp_path, rows)
    assert set(got[rows[1]["msg_id"]]["flags"]) == {"bot_command", "dup_same_author"}  # 两个都记，不只第一个


def test_route_priority():
    assert route_of({"emoji_only", "dup_same_author"}) == ROUTE_SKIP
    assert route_of({"short", "burst"}) == ROUTE_CONTEXT
    assert route_of({"long_post", "burst"}) == ROUTE_ANNOTATE
    assert norm_text("Hello,  World!") == "helloworld"


def test_nothing_deleted_and_append_only(tmp_path):
    rows = [m("a", "第一句"), m("a", "第一句", 1), m(None, "", 2)]
    data = tmp_path / "data"
    write_partitioned(messages_root(data), rows, MESSAGES_SCHEMA, "a")
    r1 = clean(data, PARAMS, SALT)
    assert r1.written == 3 and r1.total == 3  # 每条都有一行标记
    files1 = parquet_files(flags_root(data, r1.clean_ver))
    snap = {p: p.read_bytes() for p in files1}
    r2 = clean(data, PARAMS, SALT)
    assert r2.written == 0 and parquet_files(flags_root(data, r2.clean_ver)) == files1
    assert all(p.read_bytes() == b for p, b in snap.items())  # 旧文件不动
    write_partitioned(messages_root(data), [m("b", "新来的一句", 3)], MESSAGES_SCHEMA, "b")
    r3 = clean(data, PARAMS, SALT)
    assert r3.written == 1 and len(read_rows(flags_root(data, r3.clean_ver))) == 4
    for p in files1:
        assert pq.read_table(p).column("clean_ver").to_pylist()[0] == r1.clean_ver


def test_clean_ver_tracks_params_and_bots():
    p = dict(PARAMS["clean"])
    v = clean_ver_of(p, [])
    assert v == clean_ver_of(dict(p), [])
    assert v != clean_ver_of({**p, "short_max_chars": 3}, [])
    assert v != clean_ver_of(p, [("discord", "x")])


def test_report_and_noise_daily_are_group_level(tmp_path):
    rows = [m(f"u{i}", "<:ok:1>", i) for i in range(6)] + [m("z", "", 50, has_image=True), m("z", "说点正事", 60)]
    data = tmp_path / "data"
    write_partitioned(messages_root(data), rows, MESSAGES_SCHEMA, "a")
    res = clean(data, PARAMS, SALT)
    rep = json.loads(res.report_path.read_text(encoding="utf-8"))
    assert rep["total"] == 8
    assert rep["routes"]["context_only"]["n"] == 7 and rep["routes"]["annotate"]["n"] == 1
    assert abs(sum(v["share"] for v in rep["routes"].values()) - 1) < 1e-9
    assert rep["flags"]["emoji_only"]["n"] == 6 and rep["image_only"]["n"] == 1
    noise = read_rows(noise_root(data, res.clean_ver))
    cols = set(noise[0])
    assert "author_hash" not in cols and cols >= {"platform", "channel", "day_cn", "flag", "n_msgs", "n_authors"}
    by_flag = {r["flag"]: r for r in noise}
    assert by_flag["emoji_only"]["n_msgs"] == 6 and by_flag["emoji_only"]["n_authors"] == 6
    assert by_flag["image_only"]["n_msgs"] == 1 and by_flag["image_only"]["n_authors"] is None  # 不足 5 人只给条数
    assert by_flag["_all"]["n_msgs"] == 8 and by_flag["route:annotate"]["n_msgs"] == 1


def test_cli_clean(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    write_partitioned(messages_root(data), [m("a", "一句话")], MESSAGES_SCHEMA, "a")
    monkeypatch.setenv("DATA_ROOT", str(data))
    monkeypatch.setenv("AUTHOR_SALT", SALT)
    assert cli.main(["clean"]) == 0
    assert "送标 100.00%" in capsys.readouterr().out
