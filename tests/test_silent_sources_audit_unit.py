"""silent_sources_audit.py 纯逻辑单测：分级 / 沉默天数 / 审计 / 报告构建。

ARCHIVE_DIR / DISCORD_ARCHIVE_DIR / HEALTH_PATH monkeypatch 到 tmp 目录，
绝不触碰真实 data 树、绝不触网。
"""

import json
import sys
from unittest import mock

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import silent_sources_audit as ssa  # noqa: E402
from datetime import UTC


# ── compute_silent_days ─────────────────────────────────────────────────────

def test_compute_silent_days_none_returns_sentinel():
    assert ssa.compute_silent_days(None, "2026-06-01") == 9999


def test_compute_silent_days_normal():
    assert ssa.compute_silent_days("2026-05-25", "2026-06-01") == 7


def test_compute_silent_days_future_clamped_to_zero():
    assert ssa.compute_silent_days("2026-06-10", "2026-06-01") == 0


def test_compute_silent_days_bad_input_sentinel():
    assert ssa.compute_silent_days("garbage", "2026-06-01") == 9999


# ── classify ────────────────────────────────────────────────────────────────

def test_classify_never_when_zero_items():
    assert ssa.classify(0, 0) == "never"
    assert ssa.classify(100, 0) == "never"  # zero items dominates


def test_classify_active():
    assert ssa.classify(0, 5) == "active"
    assert ssa.classify(6, 5) == "active"


def test_classify_degraded():
    assert ssa.classify(7, 5) == "degraded"
    assert ssa.classify(29, 5) == "degraded"


def test_classify_dormant():
    assert ssa.classify(30, 5) == "dormant"
    assert ssa.classify(9999, 5) == "dormant"


# ── audit_source ────────────────────────────────────────────────────────────

@pytest.fixture
def dirs(tmp_path, monkeypatch):
    adir = tmp_path / "platforms"
    ddir = tmp_path / "discord"
    adir.mkdir()
    ddir.mkdir()
    monkeypatch.setattr(ssa, "ARCHIVE_DIR", adir)
    monkeypatch.setattr(ssa, "DISCORD_ARCHIVE_DIR", ddir)
    monkeypatch.setattr(ssa, "HEALTH_PATH", tmp_path / "output" / "source-health.json")
    monkeypatch.setattr(ssa, "_REPO_ROOT", tmp_path)
    return tmp_path, adir, ddir


def _write_platform_day(adir, source, date_str, item_count):
    pdir = adir / source
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{date_str}.json").write_text(
        json.dumps({"item_count": item_count, "items": []}), encoding="utf-8"
    )


def test_platform_dir_routes_discord(dirs):
    _, adir, ddir = dirs
    assert ssa._platform_dir("discord") == ddir
    assert ssa._platform_dir("reddit") == adir / "reddit"


def test_audit_source_missing_dir(dirs):
    res = ssa.audit_source("reddit")
    assert res["days_archived"] == 0
    assert res["total_items"] == 0
    assert res["first_archive_date"] is None


def test_audit_source_empty_dir(dirs):
    _, adir, _ = dirs
    (adir / "reddit").mkdir()
    res = ssa.audit_source("reddit")
    assert res["days_archived"] == 0


def test_audit_source_sums_item_count(dirs):
    _, adir, _ = dirs
    _write_platform_day(adir, "reddit", "2026-04-01", 3)
    _write_platform_day(adir, "reddit", "2026-04-02", 5)
    res = ssa.audit_source("reddit")
    assert res["days_archived"] == 2
    assert res["total_items"] == 8
    assert res["first_archive_date"] == "2026-04-01"
    assert res["last_archive_date"] == "2026-04-02"


def test_audit_source_skips_corrupt_file(dirs):
    _, adir, _ = dirs
    _write_platform_day(adir, "reddit", "2026-04-01", 3)
    (adir / "reddit" / "2026-04-02.json").write_text("{not json", encoding="utf-8")
    res = ssa.audit_source("reddit")
    assert res["total_items"] == 3
    assert res["days_archived"] == 2  # file count includes the bad one


def test_audit_source_discord_counts_messages_list(dirs):
    _, _, ddir = dirs
    (ddir / "2026-04-01.json").write_text(
        json.dumps({"messages": [{"a": 1}, {"b": 2}]}), encoding="utf-8"
    )
    res = ssa.audit_source("discord")
    assert res["total_items"] == 2


def test_audit_source_discord_counts_messages_int(dirs):
    _, _, ddir = dirs
    (ddir / "2026-04-01.json").write_text(
        json.dumps({"messages": 7}), encoding="utf-8"
    )
    res = ssa.audit_source("discord")
    assert res["total_items"] == 7


# ── build_report ────────────────────────────────────────────────────────────

def test_build_report_structure(dirs):
    _, adir, _ = dirs
    _write_platform_day(adir, "reddit", "2026-04-01", 3)
    with mock.patch.object(ssa, "ALL_REGISTERED_SOURCES", ["reddit", "bilibili"]):
        report = ssa.build_report()
    assert {e["source"] for e in report["entries"]} == {"reddit", "bilibili"}
    assert report["window_start"] == "2026-04-01"
    assert report["window_days"] >= 1
    reddit = next(e for e in report["entries"] if e["source"] == "reddit")
    assert reddit["level"] in ("active", "degraded", "dormant")


def test_build_report_no_platform_dates_uses_today(dirs):
    with mock.patch.object(ssa, "ALL_REGISTERED_SOURCES", ["reddit"]):
        report = ssa.build_report()
    # No archives -> window_start == today, window_days == 1
    assert report["window_start"] == report["today"]
    assert report["window_days"] == 1


# ── scan_unregistered_dirs ──────────────────────────────────────────────────

def test_scan_unregistered_dirs(dirs):
    _, adir, _ = dirs
    (adir / "reddit").mkdir()
    (adir / "taptap_post").mkdir()
    (adir / "loose.txt").write_text("x", encoding="utf-8")
    with mock.patch.object(ssa, "ALL_REGISTERED_SOURCES", ["reddit"]):
        found = ssa.scan_unregistered_dirs()
    assert found == ["taptap_post"]


def test_scan_unregistered_dirs_missing_archive(dirs, monkeypatch):
    monkeypatch.setattr(ssa, "ARCHIVE_DIR", dirs[0] / "nope")
    assert ssa.scan_unregistered_dirs() == []


# ── core_source_alarms ──────────────────────────────────────────────────────

def test_core_source_alarms_flags_never_and_dormant():
    report = {"entries": [
        {"source": "reddit", "level": "never"},
        {"source": "bilibili", "level": "active"},
        {"source": "steam", "level": "dormant"},
        {"source": "weixin", "level": "never"},  # not a core source
    ]}
    with mock.patch.object(ssa, "CORE_SOURCES", ["reddit", "bilibili", "steam"]):
        alarmed = ssa.core_source_alarms(report)
    assert alarmed == ["reddit", "steam"]


# ── write_health ────────────────────────────────────────────────────────────

def _report_with(entries, window_days=10, today="2026-06-01", start="2026-05-22"):
    return {
        "today": today,
        "window_start": start,
        "window_days": window_days,
        "entries": entries,
    }


def test_write_health_never_seeded_active(dirs):
    report = _report_with([
        {"source": "reddit", "level": "never", "silent_days": 9999,
         "last_archive_date": None, "total_items": 0},
    ])
    ssa.write_health(report)
    payload = json.loads(ssa.HEALTH_PATH.read_text(encoding="utf-8"))
    plat = payload["platforms"]["reddit"]
    assert plat["level"] == "active"
    assert plat["consecutive_silent_days"] == 0
    assert payload["seeded_from"] == "silent_sources_audit"


def test_write_health_sentinel_silent_uses_window_days(dirs):
    report = _report_with([
        {"source": "reddit", "level": "dormant", "silent_days": 9999,
         "last_archive_date": "2026-05-01", "total_items": 4},
    ], window_days=12)
    ssa.write_health(report)
    payload = json.loads(ssa.HEALTH_PATH.read_text(encoding="utf-8"))
    assert payload["platforms"]["reddit"]["consecutive_silent_days"] == 12


def test_write_health_preserves_last_check_date(dirs):
    ssa.HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    ssa.HEALTH_PATH.write_text(json.dumps({
        "platforms": {"reddit": {"last_check_date": "2026-05-30"}}
    }), encoding="utf-8")
    report = _report_with([
        {"source": "reddit", "level": "active", "silent_days": 2,
         "last_archive_date": "2026-05-30", "total_items": 4},
    ])
    ssa.write_health(report)
    payload = json.loads(ssa.HEALTH_PATH.read_text(encoding="utf-8"))
    assert payload["platforms"]["reddit"]["last_check_date"] == "2026-05-30"
    assert payload["platforms"]["reddit"]["consecutive_silent_days"] == 2


def test_write_health_corrupt_existing_ignored(dirs):
    ssa.HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    ssa.HEALTH_PATH.write_text("{bad json", encoding="utf-8")
    report = _report_with([
        {"source": "reddit", "level": "active", "silent_days": 1,
         "last_archive_date": "2026-05-31", "total_items": 4},
    ])
    ssa.write_health(report)  # should not raise
    payload = json.loads(ssa.HEALTH_PATH.read_text(encoding="utf-8"))
    assert payload["platforms"]["reddit"]["last_check_date"] is None


# ── print_* (smoke: ensure no crash on representative reports) ───────────────

def test_print_report_smoke(capsys):
    report = _report_with([
        {"source": "reddit", "level": "active", "silent_days": 1,
         "last_archive_date": "2026-05-31", "total_items": 4, "days_archived": 3},
        {"source": "bilibili", "level": "degraded", "silent_days": 10,
         "last_archive_date": "2026-05-20", "total_items": 2, "days_archived": 2},
        {"source": "weibo", "level": "dormant", "silent_days": 40,
         "last_archive_date": "2026-04-20", "total_items": 1, "days_archived": 1},
        {"source": "pixiv", "level": "never", "silent_days": 9999,
         "last_archive_date": None, "total_items": 0, "days_archived": 0},
    ], window_days=5)
    ssa.print_report(report)
    out = capsys.readouterr().out
    assert "沉默源审计" in out
    assert "合计 4 源" in out


def test_print_legacy_section_empty(capsys):
    ssa.print_legacy_section([])
    assert capsys.readouterr().out == ""


def test_print_legacy_section_with_entries(dirs, capsys):
    _, adir, _ = dirs
    (adir / "taptap_post").mkdir()
    with mock.patch.object(ssa, "LEGACY_SOURCES", ["taptap_post"]):
        ssa.print_legacy_section(["taptap_post", "mystery_src"])
    out = capsys.readouterr().out
    assert "遗留源" in out
    assert "已知遗留" in out
    assert "未登记" in out


def test_suggest_prune_all_healthy(capsys):
    report = _report_with([
        {"source": "reddit", "level": "active", "silent_days": 1,
         "last_archive_date": "2026-05-31", "total_items": 4},
    ])
    ssa.suggest_prune(report)
    assert "无需清理" in capsys.readouterr().out


def test_suggest_prune_lists_all_buckets(capsys):
    report = _report_with([
        {"source": "pixiv", "level": "never", "silent_days": 9999,
         "last_archive_date": None, "total_items": 0},
        {"source": "weibo", "level": "dormant", "silent_days": 40,
         "last_archive_date": "2026-04-20", "total_items": 1},
        {"source": "bilibili", "level": "degraded", "silent_days": 10,
         "last_archive_date": "2026-05-20", "total_items": 2},
    ], window_days=5)
    ssa.suggest_prune(report)
    out = capsys.readouterr().out
    assert "候选摘除" in out
    assert "强制调查" in out
    assert "轻度观察" in out


# ── main ────────────────────────────────────────────────────────────────────

def _run_main(argv):
    with mock.patch.object(sys, "argv", ["silent_sources_audit.py", *argv]):
        ssa.main()


def test_main_plain_run(dirs, capsys):
    with mock.patch.object(ssa, "ALL_REGISTERED_SOURCES", ["reddit"]):
        _run_main([])
    assert "沉默源审计" in capsys.readouterr().out


def test_main_write_and_suggest(dirs, capsys):
    _, adir, _ = dirs
    _write_platform_day(adir, "reddit", "2026-04-01", 3)
    with mock.patch.object(ssa, "ALL_REGISTERED_SOURCES", ["reddit"]):
        _run_main(["--write", "--suggest-prune"])
    assert ssa.HEALTH_PATH.exists()


def test_main_strict_exits_on_core_alarm(dirs):
    # reddit registered + core, but no archive => 'never' => alarm
    with mock.patch.object(ssa, "ALL_REGISTERED_SOURCES", ["reddit"]), \
            mock.patch.object(ssa, "CORE_SOURCES", ["reddit"]):
        with pytest.raises(SystemExit) as exc:
            _run_main(["--strict"])
    assert exc.value.code == 1


def test_main_alarm_without_strict_no_exit(dirs, capsys):
    with mock.patch.object(ssa, "ALL_REGISTERED_SOURCES", ["reddit"]), \
            mock.patch.object(ssa, "CORE_SOURCES", ["reddit"]):
        _run_main([])  # should not raise
    assert "健康门控" in capsys.readouterr().out


# ── 区服/类型分层布局（2026-07-02 修复：6 源假 degraded）─────────────────────

def _write_layered_day(adir, platform, region, subtype, date_str, item_count):
    pdir = adir / platform / region / subtype
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{date_str}.json").write_text(
        json.dumps({"item_count": item_count, "items": []}), encoding="utf-8"
    )


def test_folded_source_reads_region_subtype(dirs):
    """official 的信号在 steam/<区服>/news/，旧平级目录停更不得算沉默。"""
    _, adir, _ = dirs
    _write_platform_day(adir, "official", "2026-06-22", 2)      # 旧平级最后一天
    _write_layered_day(adir, "steam", "global", "news", "2026-07-01", 4)
    res = ssa.audit_source("official")
    assert res["last_archive_date"] == "2026-07-01"
    assert res["total_items"] == 6
    assert res["days_archived"] == 2


def test_host_platform_skips_claimed_subtypes(dirs):
    """steam 自身只认 review + 旧平级，不吞 official 的 news 子目录。"""
    _, adir, _ = dirs
    _write_platform_day(adir, "steam", "2026-06-22", 2)
    _write_layered_day(adir, "steam", "global", "review", "2026-07-01", 3)
    _write_layered_day(adir, "steam", "global", "news", "2026-07-01", 5)
    res = ssa.audit_source("steam")
    assert res["total_items"] == 5  # 2 + 3，不含 news 的 5


def test_regular_source_recurses_region_dirs(dirs):
    """appstore 等未折叠源递归区服子目录；同日多区服按去重日期计天数。"""
    _, adir, _ = dirs
    _write_layered_day(adir, "appstore", "global", ".", "2026-07-01", 3)
    _write_layered_day(adir, "appstore", "jp", ".", "2026-07-01", 1)
    res = ssa.audit_source("appstore")
    assert res["total_items"] == 4
    assert res["days_archived"] == 1
    assert res["last_archive_date"] == "2026-07-01"


def test_non_date_files_ignored(dirs):
    """state.json / manifest 类文件不得污染 last_archive_date。"""
    _, adir, _ = dirs
    pdir = adir / "youtube_comments"
    pdir.mkdir(parents=True)
    (pdir / "state.json").write_text("{}", encoding="utf-8")
    (pdir / "2026-06-20.json").write_text(json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8")
    res = ssa.audit_source("youtube_comments")
    assert res["last_archive_date"] == "2026-06-20"
    assert res["total_items"] == 2  # 裸列表按长度计数


# ── P0-3 校验丢弃门控 ────────────────────────────────────────────────────────

def test_drop_alarms_threshold(tmp_path, monkeypatch):
    from datetime import datetime
    p = tmp_path / "validation-drops.json"
    p.write_text(json.dumps({
        "generated_at": datetime.now(UTC).isoformat(),
        "total_dropped": 113,
        "by_source": {"taptap_review": 108, "weibo": 5},
    }), encoding="utf-8")
    monkeypatch.setattr(ssa, "DROPS_PATH", p)
    alarms = ssa.drop_alarms()
    assert alarms == ["taptap_review(108)"]  # 108 >= 50 响铃；5 条属正常噪声


def test_drop_alarms_missing_file_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(ssa, "DROPS_PATH", tmp_path / "nowhere.json")
    assert ssa.drop_alarms() == []
    assert ssa.load_validation_drops()["total_dropped"] == 0


def test_write_health_embeds_validation_drops(dirs, tmp_path, monkeypatch):
    _, adir, _ = dirs
    _write_platform_day(adir, "reddit", "2026-07-01", 3)
    p = tmp_path / "validation-drops.json"
    from datetime import datetime
    p.write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(),
                             "total_dropped": 7,
                             "by_source": {"ghost": 7}}), encoding="utf-8")
    monkeypatch.setattr(ssa, "DROPS_PATH", p)
    report = ssa.build_report()
    ssa.write_health(report)
    health = json.loads(ssa.HEALTH_PATH.read_text(encoding="utf-8"))
    assert health["validation_drops"]["by_source"] == {"ghost": 7}


def test_stale_drops_file_ignored(tmp_path, monkeypatch):
    """崩溃轮残留的旧 drops 文件不得被当作本轮重复告警（验证编队 minor）。"""
    from datetime import datetime, timedelta
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    p = tmp_path / "validation-drops.json"
    p.write_text(json.dumps({"generated_at": old, "total_dropped": 108,
                             "by_source": {"taptap_review": 108}}), encoding="utf-8")
    monkeypatch.setattr(ssa, "DROPS_PATH", p)
    drops = ssa.load_validation_drops()
    assert drops["total_dropped"] == 0 and drops.get("stale_ignored") is True
    assert ssa.drop_alarms() == []


def test_fresh_drops_file_honoured(tmp_path, monkeypatch):
    from datetime import datetime
    now = datetime.now(UTC).isoformat()
    p = tmp_path / "validation-drops.json"
    p.write_text(json.dumps({"generated_at": now, "total_dropped": 108,
                             "by_source": {"taptap_review": 108}}), encoding="utf-8")
    monkeypatch.setattr(ssa, "DROPS_PATH", p)
    assert ssa.drop_alarms() == ["taptap_review(108)"]


# ── 叶级下钻（区服/类型粒度，2026-07-10）────────────────────────────────────

def _write_leaf_day(adir, relpath, date_str, item_count=1):
    leaf = adir / relpath
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / f"{date_str}.json").write_text(
        json.dumps({"item_count": item_count, "items": []}), encoding="utf-8"
    )


def test_leaf_cadence_days_daily():
    dates = [f"2026-06-{d:02d}" for d in range(1, 11)]
    assert ssa.leaf_cadence_days(dates) == 1


def test_leaf_cadence_days_sparse():
    assert ssa.leaf_cadence_days(["2026-05-01", "2026-05-11", "2026-05-21"]) == 10


def test_leaf_cadence_days_insufficient():
    assert ssa.leaf_cadence_days(["2026-05-01"]) is None
    assert ssa.leaf_cadence_days([]) is None


def test_leaf_stall_threshold():
    assert ssa.leaf_stall_threshold(1) == ssa.DEGRADED_THRESHOLD  # 日更叶 7 天即报
    assert ssa.leaf_stall_threshold(10) == 30                     # 稀疏叶 3 倍节拍放宽
    assert ssa.leaf_stall_threshold(None) == ssa.DORMANT_THRESHOLD


def test_audit_leaves_flat_source_returns_empty(dirs):
    """单叶平铺源无下钻价值（与平台级重复），返回空表。"""
    _, adir, _ = dirs
    _write_platform_day(adir, "reddit", "2026-07-01", 3)
    assert ssa.audit_leaves("reddit") == []


def test_audit_leaves_layered_source(dirs):
    _, adir, _ = dirs
    _write_leaf_day(adir, "appstore/global", "2026-07-01")
    _write_leaf_day(adir, "appstore/global", "2026-07-02")
    _write_leaf_day(adir, "appstore/jp", "2026-06-01")
    leaves = ssa.audit_leaves("appstore")
    assert {ln["leaf"] for ln in leaves} == {"appstore/global", "appstore/jp"}
    jp = next(ln for ln in leaves if ln["leaf"] == "appstore/jp")
    assert jp["last_archive_date"] == "2026-06-01"
    assert jp["days_archived"] == 1


def test_build_leaf_report_flags_stalled_daily_leaf(dirs):
    """appstore/jp 形态复现：日更叶骤停 30 天必须告警，活跃叶不告警。"""
    _, adir, _ = dirs
    for d in range(1, 11):
        _write_leaf_day(adir, "appstore/jp", f"2026-06-{d:02d}")
        _write_leaf_day(adir, "appstore/global", f"2026-07-{d:02d}")
    rows = ssa.build_leaf_report("2026-07-10")
    by_leaf = {r["leaf"]: r for r in rows}
    assert by_leaf["appstore/jp"]["stalled"] is True
    assert by_leaf["appstore/jp"]["cadence_days"] == 1
    assert by_leaf["appstore/global"]["stalled"] is False


def test_build_leaf_report_sparse_leaf_not_false_alarmed(dirs):
    """稀疏叶（节拍 10 天）沉默 20 天 < 3 倍节拍，不得误报。"""
    _, adir, _ = dirs
    for d in ["2026-05-01", "2026-05-11", "2026-05-21", "2026-06-01",
              "2026-06-10", "2026-06-20"]:
        _write_leaf_day(adir, "google_play/jp", d)
    for d in range(1, 11):
        _write_leaf_day(adir, "google_play/global", f"2026-07-{d:02d}")
    rows = ssa.build_leaf_report("2026-07-10")
    jp = next(r for r in rows if r["leaf"] == "google_play/jp")
    assert jp["silent_days"] == 20
    assert jp["stalled"] is False  # 阈值 = 3 x ~10d 节拍


def test_build_leaf_report_folded_source_leaves(dirs):
    """折叠源（official → steam/*/news）的叶按宿主路径下钻且能告警。"""
    _, adir, _ = dirs
    for d in range(1, 11):
        _write_leaf_day(adir, "steam/global/news", f"2026-06-{d:02d}")
    for d in ["2026-05-01", "2026-06-01", "2026-07-01"]:
        _write_leaf_day(adir, "steam/jp/news", d)
    rows = ssa.build_leaf_report("2026-07-10")
    news_rows = {r["leaf"]: r for r in rows if r["source"] == "official"}
    assert set(news_rows) == {"steam/global/news", "steam/jp/news"}
    assert news_rows["steam/global/news"]["stalled"] is True   # 日更叶停 30 天
    assert news_rows["steam/jp/news"]["stalled"] is False      # 月更叶沉默 9 天


def test_write_health_embeds_stalled_leaves(dirs):
    # build_report() 用真实当日（UTC+8）判定，夹具日期必须相对当日生成：
    # global 写到当日（永不 stalled），jp 固定 2026-06 旧档（永远 stalled）。
    # 写死 2026-07 固定日期的旧夹具是日期定时炸弹（2026-07-14 起自然变红）。
    from datetime import datetime, timedelta
    _, adir, _ = dirs
    today = datetime.now(UTC) + timedelta(hours=8)
    for d in range(1, 11):
        _write_leaf_day(adir, "appstore/jp", f"2026-06-{d:02d}")
    for i in range(10):
        day = (today - timedelta(days=9 - i)).strftime("%Y-%m-%d")
        _write_leaf_day(adir, "appstore/global", day)
    report = ssa.build_report()
    ssa.write_health(report)
    health = json.loads(ssa.HEALTH_PATH.read_text(encoding="utf-8"))
    stalled = {ln["leaf"] for ln in health["stalled_leaves"]}
    assert "appstore/jp" in stalled
    assert "appstore/global" not in stalled


def test_window_floor_clamps_content_dated_noise(dirs):
    """weixin 2016 老文（内容日期落桶）不得把审计窗口拉到数千天。"""
    _, adir, _ = dirs
    _write_platform_day(adir, "weixin", "2016-02-03", 1)
    _write_platform_day(adir, "weixin", "2026-07-01", 2)
    report = ssa.build_report()
    assert report["window_start"] == ssa.AUDIT_WINDOW_FLOOR
    assert report["window_clamped"] is True


def test_window_not_clamped_when_within_floor(dirs):
    _, adir, _ = dirs
    _write_platform_day(adir, "reddit", "2026-07-01", 2)
    report = ssa.build_report()
    assert report["window_clamped"] is False
    assert report["window_start"] == "2026-07-01"
