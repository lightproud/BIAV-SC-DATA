"""repair_gaps.py 纯逻辑单测：缺口检测 / 报告写入 / main 编排。

ARCHIVE_DIR monkeypatch 到 tmp 目录，绝不触碰真实 data/platforms、绝不触网。
"""

import json
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import repair_gaps  # noqa: E402


def _make_platform(root: Path, name: str, dates: list[str]):
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    for d in dates:
        (pdir / f"{d}.json").write_text(
            json.dumps({"date": d, "items": []}), encoding="utf-8"
        )
    return pdir


@pytest.fixture
def archive(tmp_path, monkeypatch):
    adir = tmp_path / "platforms"
    adir.mkdir()
    monkeypatch.setattr(repair_gaps, "ARCHIVE_DIR", adir)
    # 报告路径 2026-07-02 起为独立常量（不再从 ARCHIVE_DIR 推导）——同步重定向
    monkeypatch.setattr(repair_gaps, "REPORT_PATH", tmp_path / "gap_report.json")
    return adir


# ── detect_gaps ────────────────────────────────────────────────────────────

def test_detect_gaps_finds_missing_middle_days(archive):
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-04"])
    gaps = repair_gaps.detect_gaps()
    assert gaps == {"reddit": ["2026-04-02", "2026-04-03"]}


def test_detect_gaps_no_gap_when_contiguous(archive):
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-02", "2026-04-03"])
    assert repair_gaps.detect_gaps() == {}


def test_detect_gaps_skips_platform_with_one_file(archive):
    _make_platform(archive, "reddit", ["2026-04-01"])
    assert repair_gaps.detect_gaps() == {}


def test_detect_gaps_ignores_non_directories(archive):
    (archive / "loose.txt").write_text("noise", encoding="utf-8")
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-03"])
    gaps = repair_gaps.detect_gaps()
    assert "loose.txt" not in gaps
    assert gaps == {"reddit": ["2026-04-02"]}


def test_detect_gaps_skips_bad_filename_dates(archive):
    pdir = _make_platform(archive, "reddit", ["2026-04-01", "2026-04-04"])
    # A file matching the glob shape but not a valid ISO date.
    (pdir / "2026-13-99.json").write_text("{}", encoding="utf-8")
    gaps = repair_gaps.detect_gaps()
    assert gaps == {"reddit": ["2026-04-02", "2026-04-03"]}


def test_detect_gaps_since_clamps_start(archive):
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-05"])
    # since after earliest -> only count gaps from since onward
    gaps = repair_gaps.detect_gaps(since=date(2026, 4, 3))
    assert gaps == {"reddit": ["2026-04-03", "2026-04-04"]}


def test_detect_gaps_since_before_earliest_ignored(archive):
    _make_platform(archive, "reddit", ["2026-04-02", "2026-04-04"])
    gaps = repair_gaps.detect_gaps(since=date(2026, 1, 1))
    # since is before dates[0]; start stays dates[0]=04-02
    assert gaps == {"reddit": ["2026-04-03"]}


def test_detect_gaps_multiple_platforms(archive):
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-03"])
    _make_platform(archive, "bilibili", ["2026-05-01", "2026-05-02"])
    gaps = repair_gaps.detect_gaps()
    assert gaps == {"reddit": ["2026-04-02"]}


# ── write_gap_report ───────────────────────────────────────────────────────

def test_write_gap_report_writes_sorted_payload(archive):
    repair_gaps.write_gap_report({
        "reddit": ["2026-04-02", "2026-04-03"],
        "bilibili": ["2026-05-01"],
    })
    report_path = repair_gaps.REPORT_PATH
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["total_gaps"] == 3
    # platforms dict sorted by source name -> bilibili first
    assert list(payload["platforms"].keys()) == ["bilibili", "reddit"]
    assert payload["platforms"]["reddit"]["count"] == 2
    assert payload["platforms"]["reddit"]["missing_dates"] == ["2026-04-02", "2026-04-03"]
    assert "generated_at" in payload


def test_write_gap_report_empty(archive):
    repair_gaps.write_gap_report({})
    payload = json.loads(repair_gaps.REPORT_PATH.read_text(encoding="utf-8"))
    assert payload["total_gaps"] == 0
    assert payload["platforms"] == {}


# ── main ───────────────────────────────────────────────────────────────────

def _run_main(argv):
    with mock.patch.object(sys, "argv", ["repair_gaps.py", *argv]):
        repair_gaps.main()


def test_main_no_gaps_refreshes_empty_report(archive):
    # 2026-07-02 起零缺口也刷新报告：旧缺口落出窗口后必须被擦掉，
    # 否则入库报告与工具日志自相矛盾（验证编队 minor）。
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-02"])
    with mock.patch.object(repair_gaps, "write_gap_report") as w:
        _run_main([])
    w.assert_called_once_with({})


def test_main_dry_run_does_not_write(archive):
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-03"])
    with mock.patch.object(repair_gaps, "write_gap_report") as w:
        _run_main(["--dry-run"])
    w.assert_not_called()


def test_main_writes_report_when_gaps(archive):
    # 夹具日期在默认 60 天窗口之外 → 用 --full 全史模式（顺带覆盖该旗标）
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-03"])
    _run_main(["--full"])
    report_path = repair_gaps.REPORT_PATH
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["total_gaps"] == 1


def test_main_since_argument_parsed(archive):
    _make_platform(archive, "reddit", ["2026-04-01", "2026-04-05"])
    captured = {}

    def fake_detect(since=None):
        captured["since"] = since
        return {}

    with mock.patch.object(repair_gaps, "detect_gaps", side_effect=fake_detect):
        _run_main(["--since", "2026-04-03"])
    assert captured["since"] == date(2026, 4, 3)
