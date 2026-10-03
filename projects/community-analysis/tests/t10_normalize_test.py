"""T10 规范化：字段与类型、重跑不变、raw_ref 可反查、同盐同人同哈希、换盐不同、产出里没有作者名。

数据湖是测试里现造的合成样本（假名字、假文字），不含真实玩家原文。
"""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from yuqing import cli
from yuqing.config import project_root
from yuqing.normalize.messages import MESSAGES_SCHEMA, Communities, author_hash, msg_id_of
from yuqing.normalize.run import load_ledger, messages_root, normalize, resolve_raw_ref, select_files
from yuqing.normalize.store import parquet_files, read_rows

SALT = "test-salt-not-real"
NAMES = ["假名甲甲", "FakeNameBob", "ダミー名"]


def _jsonl(path: Path, rows: list[dict], gz: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(data)
    else:
        path.write_text(data, encoding="utf-8")


def msg(mid: str, uid: str, name: str, content: str, ts: str, **kw) -> dict:
    return {"id": mid, "author_id": uid, "author_name": name, "content": content, "timestamp": ts + "+00:00", **kw}


def make_lake(root: Path) -> Path:
    lake = root / "lake"
    ch = lake / "discord" / "global" / "channels" / "111"
    _jsonl(
        ch / "2026-09-01.jsonl",
        [
            msg("1", "u1", NAMES[0], "第一句", "2026-09-01T01:00:00"),
            msg("2", "u2", NAMES[1], "reply here", "2026-09-01T01:00:30", type=19, reply_to="1"),
            msg("3", "u3", NAMES[2], "", "2026-09-01T17:00:00", attachments=[{"url": "x"}]),
            msg("4", "bot1", "SomeBot", "公告", "2026-09-01T02:00:00", author_bot=True, thread_id="77",
                thread_title="讨论串标题"),
        ],
    )  # fmt: skip
    _jsonl(ch / "2026-08-31.jsonl.gz", [msg("0", "u1", NAMES[0], "早一天", "2026-08-31T03:00:00")], gz=True)
    _jsonl(
        lake / "discord" / "jp" / "channels" / "222" / "2026-09-01.jsonl",
        [msg("9", "u1", NAMES[0], "こんにちは", "2026-09-01T05:00:00")],
    )
    weibo = lake / "weibo" / "2026-09-02.json"
    weibo.parent.mkdir(parents=True)
    weibo.write_text(
        json.dumps(
            {
                "items": [
                    {"title": "标题与正文不同", "summary": "这是正文内容", "author": NAMES[1],
                     "time": "2026-09-02T10:00:00+08:00", "metadata": {"author_id": "w42"}},
                    {"title": "无作者", "summary": "无作者", "author": "", "time": "2026-09-02 01:00:00",
                     "metadata": {"author_is_unknown": True}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )  # fmt: skip
    yt = lake / "youtube_comments" / "2026-09-02.json"
    yt.parent.mkdir(parents=True)
    yt.write_text(
        json.dumps([{"id": "c1", "video_id": "v1", "video_title": "视频标题", "author": NAMES[2], "text": "nice",
                     "published": "2026-09-02T00:00:00Z"}], ensure_ascii=False),
        encoding="utf-8",
    )  # fmt: skip
    return lake


@pytest.fixture
def lake(tmp_path):
    return make_lake(tmp_path)


def comms() -> Communities:
    return Communities.load(project_root() / "config" / "communities.toml")


def run(lake: Path, data: Path, salt: str = SALT):
    return normalize(lake, data, salt, comms())


def rows_by_ref(data: Path) -> dict[str, dict]:
    return {r["raw_ref"]: r for r in read_rows(messages_root(data))}


def test_fields_and_types(lake, tmp_path):
    data = tmp_path / "out"
    res = run(lake, data)
    assert res.rows == 9
    files = parquet_files(messages_root(data))
    for f in files:
        assert pq.read_schema(f).remove_metadata().equals(MESSAGES_SCHEMA)
        assert f.parent.name.startswith("day_cn=")
    rows = rows_by_ref(data)
    m1 = rows["discord/global/channels/111/2026-09-01.jsonl:L1"]
    assert m1["platform"] == "discord" and m1["kind"] == "chat" and m1["channel"] == "111"
    assert m1["community"] == "global" and m1["server"] == "global"
    assert m1["ts_utc"] == datetime(2026, 9, 1, 1, tzinfo=UTC)
    assert m1["day_cn"] == date(2026, 9, 1)
    assert m1["msg_id"] == msg_id_of("discord", "1")
    assert len(m1["author_hash"]) == 16 and int(m1["author_hash"], 16) >= 0
    assert m1["has_image"] is False and m1["reply_to"] is None
    m2 = rows["discord/global/channels/111/2026-09-01.jsonl:L2"]
    assert m2["reply_to"] == m1["msg_id"]  # 回复指向换成同一套 msg_id
    m3 = rows["discord/global/channels/111/2026-09-01.jsonl:L3"]
    assert m3["has_image"] is True and m3["text"] == ""
    assert m3["day_cn"] == date(2026, 9, 2)  # UTC 17:00 = UTC+8 次日 01:00
    m4 = rows["discord/global/channels/111/2026-09-01.jsonl:L4"]
    assert m4["bot_flag"] is True and m4["parent_ref"] == "thread:77" and m4["parent_text"] == "讨论串标题"
    jp = rows["discord/jp/channels/222/2026-09-01.jsonl:L1"]
    assert jp["community"] == "jp" and jp["server"] == "jp" and jp["lang"] == "ja"
    w0 = rows["weibo/2026-09-02.json#0"]
    assert w0["community"] == "cn" and w0["server"] is None  # 路径没写区服：留空不猜
    assert w0["kind"] == "post" and w0["text"] == "这是正文内容" and w0["parent_text"] == "标题与正文不同"
    assert w0["has_image"] is None  # 判断不了附件
    assert w0["msg_id"] != msg_id_of("weibo", "")
    w1 = rows["weibo/2026-09-02.json#1"]
    assert w1["author_hash"] is None and w1["parent_text"] is None
    yt = rows["youtube_comments/2026-09-02.json#0"]
    assert (
        yt["parent_ref"] == "video:v1"
        and yt["parent_text"] == "视频标题"
        and yt["msg_id"] == msg_id_of("youtube_comments", "c1")
    )


def test_author_id_preferred_over_name_q8(lake, tmp_path):
    data = tmp_path / "out"
    run(lake, data)
    w0 = rows_by_ref(data)["weibo/2026-09-02.json#0"]
    assert w0["author_hash"] == author_hash(SALT, "weibo", "w42")  # metadata.author_id，不是昵称


def test_rerun_is_stable_and_incremental(lake, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    run(lake, a)
    run(lake, b)
    key = lambda r: r["raw_ref"]  # noqa: E731
    assert sorted(read_rows(messages_root(a)), key=key) == sorted(read_rows(messages_root(b)), key=key)
    assert [p.relative_to(a) for p in parquet_files(a)] == [p.relative_to(b) for p in parquet_files(b)]
    again = run(lake, a)  # 同一目录重跑：没有新行，不写新文件
    assert again.rows == 0 and again.parts == []
    # 文件追加新行：只读新行
    f = lake / "discord/global/channels/111/2026-09-01.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg("5", "u2", NAMES[1], "追加", "2026-09-01T03:00:00")) + "\n")
    more = run(lake, a)
    assert more.rows == 1
    refs = [r["raw_ref"] for r in read_rows(messages_root(a))]
    assert len(refs) == len(set(refs)) == 10
    # 冷层压缩：.jsonl 变 .jsonl.gz 后不重读
    with f.open("rb") as src, gzip.open(str(f) + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    f.unlink()
    assert run(lake, a).rows == 0
    assert load_ledger(messages_root(a))["discord/global/channels/111/2026-09-01.jsonl"] == 5


def test_raw_ref_resolves_back(lake, tmp_path):
    data = tmp_path / "out"
    run(lake, data)
    for r in read_rows(messages_root(data)):
        rec = resolve_raw_ref(lake, r["raw_ref"])
        text = rec.get("content") or rec.get("text") or rec.get("summary") or ""
        assert text == r["text"]


def test_salt_same_person_same_hash_other_salt_differs(lake, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    run(lake, a, "salt-one")
    run(lake, b, "salt-two")
    ra, rb = rows_by_ref(a), rows_by_ref(b)
    u1 = ["discord/global/channels/111/2026-09-01.jsonl:L1", "discord/global/channels/111/2026-08-31.jsonl.gz:L1",
          "discord/jp/channels/222/2026-09-01.jsonl:L1"]  # fmt: skip
    assert len({ra[k]["author_hash"] for k in u1}) == 1  # 同盐同人
    assert ra[u1[0]]["author_hash"] != ra["discord/global/channels/111/2026-09-01.jsonl:L2"]["author_hash"]
    assert ra[u1[0]]["author_hash"] != rb[u1[0]]["author_hash"]  # 换盐不同


def test_no_author_names_in_outputs(lake, tmp_path):
    data = tmp_path / "out"
    run(lake, data)
    blob = b"".join(p.read_bytes() for p in data.rglob("*") if p.is_file())
    tables = [pq.read_table(f) for f in parquet_files(data)]
    cells = []
    for t in tables:
        for col in t.columns:
            if pa.types.is_string(col.type):
                cells += [v for v in col.to_pylist() if v]
    for name in [*NAMES, "SomeBot", "u1", "w42", "bot1"]:
        assert name.encode("utf-8") not in blob
        assert not any(name in c for c in cells)


def test_select_files_filters():
    from yuqing.lake.layout import LakeFile

    fs = [
        LakeFile("discord/global/channels/1/2026-09-01.jsonl", "discord", "discord/global/channels/1", "1", "chat",
                 "jsonl", False, "2026-09-01"),
        LakeFile("discord/global/channels/12/2026-09-02.jsonl", "discord", "discord/global/channels/12", "12", "chat",
                 "jsonl", False, "2026-09-02"),
        LakeFile("weibo/2026-09-01.json", "weibo", "weibo", None, "post", "json", False, "2026-09-01"),
    ]  # fmt: skip
    assert [f.channel for f in select_files(fs, prefixes=["discord/global/channels/1"])] == ["1"]
    assert [f.platform for f in select_files(fs, platforms=["weibo"])] == ["weibo"]
    assert len(select_files(fs, since="2026-09-02")) == 1


def test_cli_requires_salt_and_runs(lake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "out"))
    monkeypatch.delenv("AUTHOR_SALT", raising=False)
    assert cli.main(["normalize", "--lake", str(lake)]) == 2
    assert "AUTHOR_SALT" in capsys.readouterr().err
    monkeypatch.setenv("AUTHOR_SALT", SALT)
    assert cli.main(["normalize", "--lake", str(lake), "--platform", "discord"]) == 0
    out = capsys.readouterr().out
    assert "新增 6 行" in out
    for name in NAMES:
        assert name not in out
