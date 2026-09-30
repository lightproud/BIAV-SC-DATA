"""审计第十七波 · 半截文件与记账顺序（archive_engine / discord_archiver / data_quality）。

三个档共一个病根：**它们的台账都是「唯一一份」，而写法是直写**。直写的意思是——进程若在
`json.dump` 吐字节的中途被杀（CI 作业超时、runner 回收、磁盘写满），落在盘上的就是半截
JSON；而这三处的读方无一例外都是「读不出来就当空的」，于是：

| 台账 | 半截之后发生什么 |
|---|---|
| `archive-log.json` | 整份归档史归零 → `releases-index.json` 重建时抹掉此前所有月桶（Release 里的资产还在，只是没人再指得到），discord「该月已归档 → 跳过重抓」守卫同时失效 |
| discord `state.json` | **每个频道的增量游标 + 历史回填指针 + backfill-complete 闩锁**一起清零 → 下一轮把每个频道当新频道冷启动重抓，历史指针退回上个月重走 |
| `activity_daily/{date}.json` | 累计计数清零；而 JSONL 里的消息还在，`_write_msg` 去重守卫让它们**永不再**被计入统计——差额不可回收 |
| `source-health.json` | 坏档让 tracker 构造函数抛异常 → 两个调用方都吞掉 → 沉默追踪整个哑掉，且再没人写这个文件（自愈路径为零）|

另两条同族：**记账必须先于删源**（日志先落盘，最坏只是多留一份源文件，下轮重删即可；反过来
则是资产在 Release 里、源文件没了、日志里没这一桶——永远补不回来），以及**收尾落盘必须走
finally**（消息一经 `_write_msg` 就已持久化，它对应的统计却只攒在内存里）。

本档全部用「写到一半被杀」的桩来测原子性：把 `json.dump` 换成「先吐半截再抛」，然后断言
**原文件仍然完好可读**。直写实现在同一个桩下会留下坏档——这就是为什么这些断言在修复被
回退时会真的红。

小学生比喻：账本不许用铅笔在原页上边擦边写——先在草稿纸上抄一份完整的，再整页换上去；
写到一半停电了，原来那页还是好的。

零网络、零真实数据：数据根全在 tmp_path，`gh` / `git` 一律不调。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_engine as ae  # noqa: E402
import data_quality as dq  # noqa: E402
import discord_archiver as da  # noqa: E402
import news_common  # noqa: E402


class _KilledMidWrite(RuntimeError):
    """模拟「写到一半进程没了」——半截字节已经落进文件句柄。"""


def _dump_half_then_die(obj, fh, **_kw):  # noqa: ARG001  与 json.dump 签名对齐
    fh.write('{"half": "written…')
    raise _KilledMidWrite("killed mid-write")


# ════════════════════════════════════════════════════════════════════════════
# 一、archive_engine —— 归档史台账
# ════════════════════════════════════════════════════════════════════════════

class TestSaveLogAtomicity:
    def test_crash_mid_write_leaves_the_previous_log_intact(self, tmp_path, monkeypatch):
        """半截 JSON 会被 load_log 读成 `[]`（整份归档史归零）——所以绝不许落盘。"""
        log_path = tmp_path / "archive-log.json"
        old = [{"source": "discord", "group": "2026-01", "files": 31}]
        log_path.write_text(json.dumps(old), encoding="utf-8")

        monkeypatch.setattr(ae.json, "dump", _dump_half_then_die)
        with pytest.raises(_KilledMidWrite):
            ae.save_log(log_path, [*old, {"source": "discord", "group": "2026-02"}])

        assert ae.load_log(log_path) == old, "旧日志必须原封不动（否则归档史当场归零）"
        assert list(tmp_path.glob("*.tmp")) == [], "半截临时文件不许留在原地"

    def test_successful_save_replaces_and_reloads(self, tmp_path):
        log_path = tmp_path / "nested" / "archive-log.json"
        ae.save_log(log_path, [{"group": "2026-03"}])
        assert ae.load_log(log_path) == [{"group": "2026-03"}]

    def test_load_log_tolerates_a_corrupt_file(self, tmp_path):
        """读方的宽容是既定契约（本次不改），正因如此写方才必须原子。"""
        log_path = tmp_path / "archive-log.json"
        log_path.write_text("{ 半截", encoding="utf-8")
        assert ae.load_log(log_path) == []


@pytest.fixture
def engine_tree(tmp_path, monkeypatch):
    """一个自足的归档来源：base_dir 在 tmp，REPO_ROOT 改道，不碰真实数据湖。"""
    monkeypatch.setattr(ae, "REPO_ROOT", tmp_path)
    base = tmp_path / "data" / "src"
    base.mkdir(parents=True)
    for stem in ("2026-01-05", "2026-01-06", "2026-02-01"):
        (base / f"{stem}.jsonl").write_text('{"id":"1"}\n', encoding="utf-8")
    cfg = {
        "base_dir": "data/src",
        "glob": "*.jsonl",
        "group_by": "month_from_stem",
        "cutoff_days": None,
        "after_archive": "git_rm",
        "release_tag": "community-data",
        "asset_template": "src-archive-{group}.tar.gz",
    }
    return base, cfg


def _args(**over):
    return argparse.Namespace(
        **{"dry_run": False, "skip_upload": True, "force_group": [], **over}
    )


class TestLedgerBeforeDeletion:
    def test_log_is_persisted_before_source_files_are_removed(self, engine_tree, monkeypatch):
        """记账必须先于删源：git_rm 触发那一刻，日志里已经有这一桶。"""
        base, cfg = engine_tree
        log_path = base / "archive-log.json"
        seen_at_rm: list[list[str]] = []

        def fake_git_rm(files):
            groups = [e.get("group") for e in ae.load_log(log_path)]
            seen_at_rm.append(groups)
            for f in files:
                f.unlink(missing_ok=True)
            return len(files)

        monkeypatch.setattr(ae, "git_rm_files", fake_git_rm)
        ae.archive_source("src", cfg, _args())

        assert seen_at_rm == [["2026-01"], ["2026-01", "2026-02"]], (
            "删源之前日志里必须已经有该桶——反过来则是资产在 Release、源没了、日志没这条"
        )
        assert [e["group"] for e in ae.load_log(log_path)] == ["2026-01", "2026-02"]

    def test_rerun_replaces_the_bucket_entry_instead_of_appending(self, engine_tree, monkeypatch):
        """重跑是常态（keep 来源原地留存）——追加式日志会让同一月桶长出 N 份索引行。"""
        base, cfg = engine_tree
        cfg = {**cfg, "after_archive": "keep"}
        monkeypatch.setattr(ae, "git_rm_files", len)  # 只数不删（after_archive=keep）
        ae.archive_source("src", cfg, _args())
        ae.archive_source("src", cfg, _args())
        log = ae.load_log(base / "archive-log.json")
        assert [e["group"] for e in log] == ["2026-01", "2026-02"], "同桶只许有一条现行记录"

    def test_nothing_to_archive_is_a_no_op(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(ae, "REPO_ROOT", tmp_path)
        cfg = {"base_dir": "data/empty", "glob": "*.jsonl", "group_by": "month_from_stem"}
        with caplog.at_level(logging.INFO):
            ae.archive_source("empty", cfg, _args())
        assert "nothing to archive" in caplog.text

    def test_dry_run_writes_no_log(self, engine_tree):
        base, cfg = engine_tree
        ae.archive_source("src", cfg, _args(dry_run=True))
        assert not (base / "archive-log.json").exists()


class TestGroupingEdges:
    def test_unknown_group_by_is_loud(self):
        with pytest.raises(ValueError, match=r"unknown group_by"):
            ae.group_of(Path("2026-01-01.jsonl"), "by_vibes", "all")

    def test_dateless_source_is_conservatively_eligible(self):
        """无日期语义的来源不该配 cutoff_days；真配了也只许保守判为可归档。"""
        assert ae.is_eligible(Path("blob.bin"), "single", "2026-01-01") is True

    def test_cutoff_days_filters_by_age(self, engine_tree, caplog):
        """cutoff 由 now 推导：给足够大的天数即全部够龄，够小则一件不剩（无墙钟依赖）。"""
        base, cfg = engine_tree
        with caplog.at_level(logging.INFO):
            wide = ae.discover({**cfg, "cutoff_days": 1}, base, [])
        assert sorted(wide) == ["2026-01", "2026-02"]
        assert "Cutoff date:" in caplog.text
        assert ae.discover({**cfg, "cutoff_days": 40000}, base, []) == {}

    def test_force_group_bypasses_cutoff(self, engine_tree):
        base, cfg = engine_tree
        assert sorted(ae.discover({**cfg, "cutoff_days": 40000}, base, ["2026-02"])) == ["2026-02"]

    def test_clean_empty_dirs_survives_a_racing_rmdir(self, tmp_path, monkeypatch):
        """目录在检查与 rmdir 之间被别人删掉——清理是尽力而为，不许把整轮拖垮。"""
        base = tmp_path / "b"
        sub = base / "2026-01-01"
        sub.mkdir(parents=True)

        def boom(self):
            raise OSError("directory vanished")

        monkeypatch.setattr(Path, "rmdir", boom)
        ae.clean_empty_dirs(base, [sub / "gone.jsonl"])  # 不抛即通过
        assert sub.exists()


# ════════════════════════════════════════════════════════════════════════════
# 二、discord_archiver —— 断点续传台账 / 日统计 / JSONL 追加
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def archiver(tmp_path):
    env = {
        "DISCORD_BOT_TOKEN": "dummy",
        "DISCORD_GUILD_ID": "999",
        "DISCORD_DATA_ROOT": str(tmp_path / "discord"),
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return da.DiscordArchiver()


def _slim(mid: str, ts: str = "2026-05-03T14:41:39.000000+00:00") -> dict:
    return {
        "id": mid,
        "channel_id": "chan-1",
        "author_id": "u1",
        "author_name": "tester",
        "content": f"msg {mid}",
        "timestamp": ts,
        "attachments": [],
        "reactions": [],
        "type": 0,
    }


class TestStateAtomicity:
    def test_crash_mid_write_keeps_every_channel_cursor(self, archiver, monkeypatch):
        """半截 state.json = 每个频道游标 + 历史指针 + backfill 闩锁一起蒸发。"""
        good = {
            "channels": {"111": {"last_message_id": "9001", "cold_started": True}},
            "historical_month": "2026-03",
            "history_backfill_complete": True,
            "last_run": "2026-07-01T00:00:00+00:00",
        }
        archiver.state_path.parent.mkdir(parents=True, exist_ok=True)
        archiver.state_path.write_text(json.dumps(good), encoding="utf-8")
        archiver.state = {"channels": {}, "historical_month": None}

        monkeypatch.setattr(news_common.json, "dump", _dump_half_then_die)
        with pytest.raises(_KilledMidWrite):
            archiver._save_state()

        on_disk = json.loads(archiver.state_path.read_text(encoding="utf-8"))
        assert on_disk == good, "游标必须原封不动——重置的代价是整服重抓 + 历史重走"
        assert list(archiver.state_path.parent.glob(".*tmp*")) == []

    def test_successful_save_stamps_last_run_and_round_trips(self, archiver):
        archiver.state["channels"]["222"] = {"last_message_id": "42"}
        archiver._save_state()
        reloaded = archiver._load_state()
        assert reloaded["channels"]["222"] == {"last_message_id": "42"}
        assert reloaded["last_run"], "last_run 必须落盘（沉默检测据此判活）"

    def test_corrupt_state_falls_back_to_empty(self, archiver, caplog):
        """读方的回落是既定契约（本次不改）；正因它这么宽容，写方才必须原子。"""
        archiver.state_path.parent.mkdir(parents=True, exist_ok=True)
        archiver.state_path.write_text("{ 半截", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert archiver._load_state() == {
                "channels": {}, "historical_month": None, "last_run": None,
            }
        assert "Failed to load state" in caplog.text


class TestJsonlTornLineRepair:
    def test_append_after_a_torn_line_keeps_every_record_parseable(self, archiver):
        """上一轮被杀留下无换行的半截记录：新记录不许被粘在它后面拼成一行。"""
        ch_dir = archiver._ch_dir("chan-1")
        ch_dir.mkdir(parents=True)
        path = ch_dir / "2026-05-03.jsonl"
        path.write_text('{"id":"1001","content":"完整"}\n{"id":"1002","cont', encoding="utf-8")

        assert archiver._write_msg("chan-1", "2026-05-03", _slim("1003")) is True

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[-1].startswith('{"id": "1003"'), "新记录必须自成一行"
        assert json.loads(lines[-1])["id"] == "1003"
        assert lines[1] == '{"id":"1002","cont', "损坏只许停留在原来那半截上"

    def test_tail_is_checked_only_once_per_run(self, archiver):
        """第二条记录不许再补换行（否则文件里会长出空行）。"""
        ch_dir = archiver._ch_dir("chan-1")
        ch_dir.mkdir(parents=True)
        path = ch_dir / "2026-05-03.jsonl"
        path.write_text('{"id":"1002","cont', encoding="utf-8")
        archiver._write_msg("chan-1", "2026-05-03", _slim("1003"))
        archiver._write_msg("chan-1", "2026-05-03", _slim("1004"))
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert [json.loads(x)["id"] for x in lines[1:]] == ["1003", "1004"]

    def test_duplicate_is_not_rewritten(self, archiver):
        archiver._write_msg("chan-1", "2026-05-03", _slim("2001"))
        assert archiver._write_msg("chan-1", "2026-05-03", _slim("2001")) is False

    def test_ends_cleanly_edges(self, tmp_path):
        missing = tmp_path / "nope.jsonl"
        assert da._ends_cleanly(missing) is True, "取不到大小时保守判干净（不乱补换行）"
        empty = tmp_path / "empty.jsonl"
        empty.write_bytes(b"")
        assert da._ends_cleanly(empty) is True
        torn = tmp_path / "torn.jsonl"
        torn.write_bytes(b'{"id":"1"}')
        assert da._ends_cleanly(torn) is False
        whole = tmp_path / "whole.jsonl"
        whole.write_bytes(b'{"id":"1"}\n')
        assert da._ends_cleanly(whole) is True


class TestDailyStats:
    @staticmethod
    def _seed(archiver, date_str="2026-05-03", messages=50):
        stats = archiver.daily_stats[date_str]
        stats["messages"] = messages
        stats["unique_authors"].add("u1")
        stats["channel_activity"]["general"] = messages
        stats["hourly_activity"]["14"] = messages
        stats["message_types"]["0"] = messages
        return archiver.data_dir / "activity_daily" / f"{date_str}.json"

    def test_corrupt_existing_stats_fail_loudly(self, archiver, caplog):
        """静默 pass 会把一天 503 条的统计悄悄改写成本轮那几条，且不可回收。"""
        out = self._seed(archiver, messages=3)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{ 半截", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            archiver._save_daily_stats()

        assert "activity_daily 2026-05-03.json 读取失败" in caplog.text
        assert "不可回收" in caplog.text
        assert json.loads(out.read_text(encoding="utf-8"))["messages"] == 3

    def test_existing_stats_are_merged_not_overwritten(self, archiver):
        """对照臂：档没坏时必须逐字段并轨（含 message_types）。"""
        out = self._seed(archiver, messages=3)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "date": "2026-05-03", "messages": 500, "unique_authors": 7,
            "reactions_total": 4, "attachments": 2,
            "channel_activity": {"general": 500}, "hourly_activity": {"14": 500},
            "message_types": {"0": 500}, "top_reacted_messages": [],
        }), encoding="utf-8")

        archiver._save_daily_stats()
        merged = json.loads(out.read_text(encoding="utf-8"))
        assert merged["messages"] == 503
        assert merged["message_types"]["0"] == 503, "分布与总数同源，漏并轨即自相矛盾"
        assert merged["channel_activity"]["general"] == 503
        assert merged["unique_authors"] == 7

    def test_stats_are_flushed_even_when_the_run_explodes(self, archiver, monkeypatch):
        """收尾必须在 finally：消息已持久化在 JSONL，统计只在内存里。"""
        out = self._seed(archiver)
        monkeypatch.setattr(
            archiver, "_run_tracks", mock.Mock(side_effect=RuntimeError("payload 畸形"))
        )
        with pytest.raises(RuntimeError, match="payload 畸形"):
            archiver.run()

        assert out.exists(), "异常路径下 activity_daily 仍须落盘（否则这笔差额永久缺失）"
        assert json.loads(out.read_text(encoding="utf-8"))["messages"] == 50
        assert archiver.state_path.exists(), "state 也必须一并收尾"

    def test_flush_swallows_and_reports_its_own_failures(self, archiver, monkeypatch, caplog):
        """收尾自身失败不许再抛——否则它会盖掉真正的原始异常。"""
        monkeypatch.setattr(
            archiver, "_save_daily_stats", mock.Mock(side_effect=OSError("ENOSPC"))
        )
        monkeypatch.setattr(archiver, "_save_state", mock.Mock(side_effect=OSError("ENOSPC")))
        with caplog.at_level(logging.ERROR):
            archiver._flush_on_exit()
        assert "收尾保存 activity_daily 失败" in caplog.text
        assert "收尾保存 state.json 失败" in caplog.text


# ════════════════════════════════════════════════════════════════════════════
# 三、data_quality —— 沉默追踪台账
# ════════════════════════════════════════════════════════════════════════════

class TestHealthFileResilience:
    def test_corrupt_health_falls_back_to_empty_with_a_loud_warning(self, tmp_path, caplog):
        """坏档若把构造函数炸掉，两个调用方都会吞掉异常 → 沉默追踪整个哑掉。"""
        path = tmp_path / "source-health.json"
        path.write_text('{"platforms": {"bilibili": ', encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            tracker = dq.SilentPlatformTracker(health_path=path)
        assert tracker.health_data == {"updated_at": None, "platforms": {}}
        assert "source-health 读取失败" in caplog.text
        assert "沉默天数计数将从零重建" in caplog.text

    def test_structurally_wrong_health_is_also_rejected(self, tmp_path, caplog):
        """结构不对（platforms 不是 dict）同样回落——不许让下游 KeyError。"""
        path = tmp_path / "source-health.json"
        path.write_text('{"platforms": []}', encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            tracker = dq.SilentPlatformTracker(health_path=path)
        assert tracker.health_data["platforms"] == {}
        assert "结构异常" in caplog.text

    def test_valid_health_is_loaded_verbatim(self, tmp_path):
        path = tmp_path / "source-health.json"
        payload = {"updated_at": "2026-07-01T00:00:00+00:00",
                   "platforms": {"reddit": {"level": "active", "consecutive_silent_days": 0}}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert dq.SilentPlatformTracker(health_path=path).health_data == payload

    def test_save_is_atomic_so_a_kill_cannot_corrupt_the_ledger(self, tmp_path, monkeypatch):
        """本档一轮采集要落十几次盘；直写被杀即坏档，而坏档没有自愈路径。"""
        path = tmp_path / "source-health.json"
        good = {"updated_at": "2026-07-01T00:00:00+00:00",
                "platforms": {"reddit": {"level": "active", "consecutive_silent_days": 3,
                                         "total_items": 12, "errors": [],
                                         "last_success_date": None, "last_check_date": None}}}
        path.write_text(json.dumps(good), encoding="utf-8")
        tracker = dq.SilentPlatformTracker(health_path=path)

        monkeypatch.setattr(news_common.json, "dump", _dump_half_then_die)
        with pytest.raises(_KilledMidWrite):
            tracker.update_platform_status("reddit", items_count=5)

        assert json.loads(path.read_text(encoding="utf-8")) == good
        assert list(tmp_path.glob(".*tmp*")) == []

    def test_update_round_trips_through_the_ledger(self, tmp_path):
        """对照臂：正常路径写得进、读得出（否则上一条可能只是「总是不写」）。"""
        path = tmp_path / "source-health.json"
        tracker = dq.SilentPlatformTracker(health_path=path)
        tracker.update_platform_status("reddit", items_count=7)
        tracker.update_platform_status("nga", items_count=0, error="cookie 过期",
                                       note="待配 NGA_COOKIE")
        reloaded = dq.SilentPlatformTracker(health_path=path).health_data["platforms"]
        assert reloaded["reddit"]["total_items"] == 7
        assert reloaded["reddit"]["level"] == dq.SilentPlatformTracker.LEVEL_ACTIVE
        assert reloaded["nga"]["note"] == "待配 NGA_COOKIE"
        assert reloaded["nga"]["errors"][0]["error"].startswith("cookie")


def test_news_common_atomic_writer_cleans_up_after_a_kill(tmp_path, monkeypatch):
    """三处原子写共用的那一个真源：失败时旧档留存 + 临时文件清干净。"""
    path = tmp_path / "out.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    monkeypatch.setattr(news_common.json, "dump", _dump_half_then_die)
    with pytest.raises(_KilledMidWrite):
        news_common.dump_json_atomic(path, {"new": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory():
        raise SystemExit(pytest.main([str(Path(__file__)), "-v"]))
