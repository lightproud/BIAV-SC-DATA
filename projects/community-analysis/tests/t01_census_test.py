"""T01 数据普查：小样本上的结果与 tests/expected/census.json 完全一致，外加手算数字与隐私检查。

小样本 tests/fixtures/lake_mini/ 全是合成文字与假名字，不含真实玩家原文。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from yuqing import cli
from yuqing.census import fields as F
from yuqing.census import run as census_run
from yuqing.census.engine import quantiles
from yuqing.census.run import run_census
from yuqing.config import ConfigError, load_config
from yuqing.lake.reader import BUILTIN_READER, LakeReader

HERE = Path(__file__).resolve().parent
LAKE = HERE / "fixtures" / "lake_mini"
EXPECTED = HERE / "expected" / "census.json"
SECRET_KEYS = {"author_name", "author", "screen_name", "content", "text", "review"}


def _params() -> dict:
    # 小样本期望值按 gap_minutes=10 手算；G0 把默认改成 2 后在此钉住，免得参数调整牵动普查回归
    return load_config(environ={"YUQING_UNITIZE_GAP_MINUTES": "10"}).params


def _result() -> dict:
    return run_census(LAKE, _params())


def _normalized(obj: dict) -> dict:
    return json.loads(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _fixture_secrets() -> set[str]:
    """小样本里所有作者名与正文（长度≥3），用来检查输出里搜不到它们。"""
    out: set[str] = set()

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and len(v) >= 3 and key in SECRET_KEYS:
            out.add(v)

    for p in LAKE.rglob("*"):
        if not p.is_file():
            continue
        raw = gzip.decompress(p.read_bytes()).decode() if p.suffix == ".gz" else p.read_text(encoding="utf-8")
        if ".jsonl" in p.name:
            for line in raw.splitlines():
                walk(json.loads(line))
        else:
            walk(json.loads(raw))
    return out


def test_matches_expected_exactly():
    assert _normalized(_result()) == json.loads(EXPECTED.read_text(encoding="utf-8"))


def test_hand_computed_numbers():
    r = _result()
    t = r["totals"]
    assert t["messages"] == 28
    assert t["chat_share"] == 0.75
    assert t["kept_after_clean"] == 19
    assert t["trial_segments"] == 6
    assert t["trial_units_total"] == 11  # 6 段聊天 + 5 条评论 / 帖子 / 评价
    d = r["platforms"]["discord"]
    assert d["messages"] == 21
    assert d["authors_unique"] == 11
    assert d["clean_trial"]["drops"] == {
        "bot": 1,
        "system": 1,
        "empty": 1,
        "emoji_only": 1,
        "dup_same_author": 1,
        "dup_long_repost": 1,
        "image_only（保留不送标）": 1,
    }
    assert d["by_month"] == {"2026-08": 2, "2026-09": 19}  # 2026-08-31T23:30Z 按 UTC+8 归 9 月
    assert d["share"]["image_only"] == round(1 / 21, 4)
    assert d["dup"] == {"same_author": round(1 / 18, 4), "cross_author_short": round(1 / 18, 4)}
    assert d["special"]["reply"]["filled_share"] == round(1 / 21, 4)
    assert d["special"]["bot"]["filled_share"] == round(1 / 21, 4)
    assert d["special"]["native_id"]["filled_share"] == 1.0
    assert d["chat_channels"]["discord/global/channels/1001"]["messages"] == 16
    assert r["platforms"]["weibo"]["share"]["image_only"] == 0.5
    assert r["platforms"]["youtube"]["special"]["reply"]["filled_share"] == 0.5
    assert r["lake"]["skipped"] == {"统计快照目录 activity_daily/": 1, "非日期档（索引 / 状态等）": 1}
    assert r["reader"] == BUILTIN_READER


def test_rerun_is_identical():
    assert _normalized(_result()) == _normalized(_result())


def test_cli_writes_json_and_html(tmp_path):
    out = tmp_path / "census"
    assert cli.main(["census", "--lake", str(LAKE), "--out", str(out)]) == 0
    assert (out / "census.json").is_file()
    html = (out / "census.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "方案假设与实测" in html


def test_outputs_contain_no_raw_text_or_authors(tmp_path, capsys):
    out = tmp_path / "census"
    cli.main(["census", "--lake", str(LAKE), "--out", str(out)])
    blob = (out / "census.json").read_text(encoding="utf-8") + (out / "census.html").read_text(encoding="utf-8")
    blob += capsys.readouterr().out
    secrets = _fixture_secrets()
    assert len(secrets) > 10
    leaked = sorted(s for s in secrets if s in blob)
    assert leaked == []


def test_lake_untouched(tmp_path):
    def snapshot():
        return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in LAKE.rglob("*") if p.is_file()}

    before = snapshot()
    cli.main(["census", "--lake", str(LAKE), "--out", str(tmp_path / "c")])
    assert snapshot() == before


def test_out_inside_lake_refused(tmp_path, capsys):
    lake = tmp_path / "lake"
    shutil.copytree(LAKE, lake)
    assert cli.main(["census", "--lake", str(lake), "--out", str(lake / "report")]) == 2
    assert "数据湖" in capsys.readouterr().err


def test_missing_lake_root_named(monkeypatch, capsys):
    monkeypatch.delenv("LAKE_ROOT", raising=False)
    monkeypatch.setattr(
        census_run, "default_lake", lambda: None
    )  # 完整检出的数据仓里默认路径存在，测试不得普查真实数据
    assert cli.main(["census", "--out", "/tmp/yuqing-census-unused"]) == 2
    assert "LAKE_ROOT" in capsys.readouterr().err


def test_reuses_data_repo_reader(tmp_path):
    scripts = tmp_path / "data" / "projects" / "news" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "archive_layout.py").write_text(
        "import gzip\n"
        "def open_archive_text(path, mode='rt'):\n"
        "    p = str(path)\n"
        "    return gzip.open(p, 'rt', encoding='utf-8') if p.endswith('.gz') else open(p, encoding='utf-8')\n",
        encoding="utf-8",
    )
    lake = tmp_path / "data" / "Record" / "Community"
    shutil.copytree(LAKE, lake)
    r = run_census(lake, _params())
    assert r["reader"] == "数据仓 archive_layout.open_archive_text"
    assert r["totals"] == _result()["totals"]


def test_quantiles_nearest_rank():
    hist = Counter({1: 50, 10: 40, 100: 9, 1000: 1})
    assert quantiles(hist) == {"p50": 1, "p90": 10, "p99": 100}
    assert quantiles(Counter()) == {"p50": 0, "p90": 0, "p99": 0}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("😀😀", True), ("<:pog:123> !!", True), ("……", True), ("+1", False), ("ok 👍", False), ("", False)],
)
def test_emoji_only(text, expected):
    assert F.is_emoji_only(text) is expected


@pytest.mark.parametrize(
    ("text", "label", "tokens"),
    [
        ("测试", "zh", 2),
        ("テストです", "ja", 5),
        ("테스트", "ko", 3),
        ("hello there", "latin", 3),
        ("+1", "none", 1),
    ],
)
def test_script_profile(text, label, tokens):
    assert F.script_profile(text) == (label, tokens)


def test_parse_ts_forms():
    assert F.parse_ts({"timestamp": "2026-09-01T01:00:00+00:00"}).isoformat() == "2026-09-01T01:00:00+00:00"
    assert F.parse_ts({"created_at": "2026-09-02 10:00:00"}).isoformat() == "2026-09-02T10:00:00+00:00"
    assert F.parse_ts({"timestamp_created": 1788220800}).isoformat() == "2026-09-01T00:00:00+00:00"
    assert F.parse_ts({"ts": 1788220800000}).isoformat() == "2026-09-01T00:00:00+00:00"
    assert F.parse_ts({"time": "昨天"}) is None


def test_config_error_type_is_raised_for_missing_dir(tmp_path):
    with pytest.raises(ConfigError):
        run_census(tmp_path / "nope", _params())


def test_default_lake_is_repo_record_community(monkeypatch, tmp_path):
    (tmp_path / "Record" / "Community").mkdir(parents=True)
    monkeypatch.setattr(census_run, "repo_root", lambda: tmp_path)
    assert census_run.default_lake() == tmp_path / "Record" / "Community"
    monkeypatch.setattr(census_run, "repo_root", lambda: tmp_path / "nope")
    assert census_run.default_lake() is None


def test_fixture_lake_does_not_borrow_repo_reader():
    """小样本放在数据仓里，也不能误用本仓 archive_layout：只认 <仓库>/Record/Community。"""
    assert LakeReader(LAKE).source == BUILTIN_READER


def test_news_style_item_reads_summary_not_title():
    # 数据仓 news 体例：title 是截断标题，正文在 summary；普查要量正文
    from yuqing.census import fields as F

    rec = {"title": "[Google Play 好评] ★5 前四十字…", "summary": "完整的评价正文" * 10}
    assert F.get_text(rec) == rec["summary"]
    assert F.get_text({"title": "只有标题", "summary": ""}) == "只有标题"
