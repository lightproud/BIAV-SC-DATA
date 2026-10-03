"""T13 试切检查包：页面含 50 段；同 seed 结果相同；页面里搜不到作者名与哈希。

样本是测试里现造的合成消息（假作者标识、假文字），不含真实玩家原文。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from yuqing import cli
from yuqing.config import load_config
from yuqing.normalize.clean import clean
from yuqing.normalize.messages import MESSAGES_SCHEMA, author_hash
from yuqing.normalize.run import messages_root
from yuqing.normalize.store import read_rows, write_partitioned
from yuqing.unitize.review import code_of, parse_days, review
from yuqing.unitize.run import unitize

SALT = "test-salt-not-real"
T0 = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
PARAMS = load_config(environ={}).params
UP = PARAMS["unitize"]
AUTHORS = ["987654321", "123123123", "555000555"]  # 假 Discord 用户 ID（作者标识）


def make_data(tmp_path, n_segments: int = 60):
    rows = []
    for s in range(n_segments):
        base = s * 3600  # 每段隔 1 小时：一段一个单元
        for k in range(3):
            a = AUTHORS[(s + k) % 3]
            text = f"第{s}段第{k}句，正常讨论内容"
            if k == 1:
                text = f"<@{AUTHORS[(s + 2) % 3]}> 你说得对，第{s}段"
            rows.append(
                {
                    "msg_id": f"x{s:03d}{k}",
                    "platform": "discord",
                    "community": "global",
                    "server": "global",
                    "channel": "c9",
                    "kind": "chat",
                    "author_hash": author_hash(SALT, "discord", a),
                    "ts_utc": T0 + timedelta(seconds=base + k * 20),
                    "day_cn": date(2026, 9, 1) if s < 14 else date(2026, 9, 2),
                    "text": text,
                    "lang": "zh",
                    "has_image": False,
                    "reply_to": f"x{s:03d}0" if k == 2 else None,
                    "parent_ref": None,
                    "raw_ref": f"fake/c9.jsonl:L{s * 3 + k + 1}",
                    "parent_text": None,
                    "bot_flag": None,
                    "msg_type": None,
                    "norm_ver": "1",
                }
            )
        rows.append({**rows[-1], "msg_id": f"x{s:03d}e", "text": "<:smile:42>", "raw_ref": f"fake/c9.jsonl:E{s}",
                     "ts_utc": T0 + timedelta(seconds=base + 50), "reply_to": None})  # fmt: skip
    data = tmp_path / "data"
    write_partitioned(messages_root(data), rows, MESSAGES_SCHEMA, "a")
    cv = clean(data, PARAMS, SALT).clean_ver
    res = unitize(data, UP, cv, replay=True)
    return data, res


def test_page_has_50_segments_and_is_deterministic(tmp_path):
    data, res = make_data(tmp_path)
    page, k = review(data, res.unit_ver, UP, ["c9"], ["2026-09-01..2026-09-02"], 50, 1, SALT)
    assert k == 50 and page.count('class="seg"') == 50
    again, _ = review(data, res.unit_ver, UP, ["c9"], ["2026-09-01..2026-09-02"], 50, 1, SALT)
    assert page == again
    other, _ = review(data, res.unit_ver, UP, ["c9"], ["2026-09-01..2026-09-02"], 50, 2, SALT)
    assert other != page
    ids = re.findall(r'data-unit="([0-9a-f]+)"', page)
    assert len(set(ids)) == 50
    assert page.count(">看得懂<") == 50 and page.count(">看不懂<") == 50
    assert "localStorage" not in page and "http" not in page  # 不联网、不落盘


def test_no_author_names_or_hashes_in_page(tmp_path):
    data, res = make_data(tmp_path)
    page, _ = review(data, res.unit_ver, UP, None, None, 50, 1, SALT)
    hashes = {r["author_hash"] for r in read_rows(messages_root(data))}
    for h in hashes:
        assert h not in page
    for a in AUTHORS:
        assert a not in page  # @提及里的用户 ID 也换掉了
    assert "@甲" in page or "@乙" in page or "@丙" in page  # 段内的人换成代号
    assert "甲" in page


def test_context_gray_before_body_and_codes(tmp_path):
    data, res = make_data(tmp_path, n_segments=3)
    page, k = review(data, res.unit_ver, UP, ["c9"], None, 50, 1, SALT)
    assert k == 3
    first = page.split('class="seg"')[2]  # 第二段：有前一段的重叠上下文
    assert first.index('class="m c"') < first.index('class="sep">正文')
    assert ":smile:" in page  # 自定义表情显示成名字
    assert "旁注" in page and "上文" in page


def test_day_filter_and_helpers():
    assert parse_days(["2026-09-01..2026-09-03"]) == {date(2026, 9, d) for d in (1, 2, 3)}
    assert parse_days(None) is None
    assert [code_of(i) for i in range(3)] == ["甲", "乙", "丙"] and code_of(30) == "人31"


def test_cli_review(tmp_path, monkeypatch, capsys):
    data, _ = make_data(tmp_path)
    monkeypatch.setenv("DATA_ROOT", str(data))
    monkeypatch.setenv("AUTHOR_SALT", SALT)
    out = tmp_path / "review.html"
    argv = ["unitize", "review", "--channel", "c9", "--day", "2026-09-02", "--n", "50", "--seed", "1"]
    assert cli.main([*argv, "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "46 段" in text and "少于要求的 50 段" in text  # 9-02 只有 46 段
    assert out.read_text(encoding="utf-8").count('class="seg"') == 46
