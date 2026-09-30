"""审计第十七波配套 · 孤儿目录对账（`projects/news/scripts/discord_reconcile.py`）。

为什么这一档跟着本波走：波次十七给 `discord_archiver._save_channel_index` 换上了原子写，
判词是「直写被中断留下的半截索引会被判成 unreadable, rebuilding from scratch —— offline/orphan
历史条目全部蒸发，**正是 T35 那 498 个不可反查孤儿目录的成因**」。那句话里的另一半——
真正把孤儿捞回来的那个工具——此前几乎没有测试（58% 覆盖，`collect_historical_names` 与
CLI 整段没人跑过）。修好了写方却不测捞方，等于修了船底洞不查救生艇。

`channel_index.json` 是**频道反查的唯一入口**：归档目录名只有 channel_id 后 8 位，名字与
类型只此一份。而 CLAUDE.md §6.3 已记明全仓压扁后 git 历史一路已断——名字回收这条路
只剩「现存档案里还留着的那些」，一次撕裂即永久失名。故本档钉三件：

1. `recover_channel_id`：从 JSONL（裸 / 冷层 `.gz`）首行捞回完整 id，坏档只跳过不炸；
2. `collect_historical_names`：名字尽力回收，git 不可用 / 仓外路径一律降级而非失败；
3. `reconcile_region`：孤儿逐个登记 `status: orphan`、`--dry-run` 一个字节不写、
   写回走 tmp + `os.replace`（与本波三处原子写同一手法）。

零真实数据、零真实仓：数据根在 tmp，git 调用全部打桩。

小学生比喻：储藏室里堆着一屋子没贴标签的箱子——先从箱子里翻出货号，再去旧账本里找
这个货号叫什么名字，找不到名字也得先把货号贴上，起码下次有人问「那箱是什么」时查得到。
"""
from __future__ import annotations

import gzip
import json
import logging
import sys
from pathlib import Path

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_layout  # noqa: E402
import discord_reconcile as dr  # noqa: E402


def _jsonl(path: Path, *records):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                    encoding="utf-8")


@pytest.fixture
def region(tmp_path):
    """一个区服根：channels/ 下三个目录，其中两个是索引里没有的孤儿。"""
    root = tmp_path / "discord" / "global"
    (root / "channels").mkdir(parents=True)
    return root


def _chan(region_root: Path, dir_name: str, channel_id: str, *, gz: bool = False):
    d = region_root / "channels" / dir_name
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": "1", "channel_id": channel_id, "author_id": "u", "author_name": "t",
           "content": "hi", "timestamp": "2026-05-03T00:00:00+00:00"}
    if gz:
        with gzip.open(d / "2026-05-03.jsonl.gz", "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    else:
        _jsonl(d / "2026-05-03.jsonl", rec)
    return d


class TestRecoverChannelId:
    def test_from_a_plain_jsonl(self, region):
        d = _chan(region, "23456789", "111123456789")
        assert dr.recover_channel_id(d) == "111123456789"

    def test_from_a_cold_compressed_jsonl(self, region):
        """冷层（`.jsonl.gz`）同样要能捞——上上个月起的归档全是这个形态。"""
        d = _chan(region, "34567890", "222234567890", gz=True)
        assert dr.recover_channel_id(d) == "222234567890"

    def test_prefers_the_newest_file_and_skips_broken_ones(self, region):
        d = region / "channels" / "45678901"
        d.mkdir(parents=True)
        (d / "2026-05-01.jsonl").write_text("{ 半截\n", encoding="utf-8")
        _jsonl(d / "2026-05-02.jsonl", {"id": "9", "channel_id": "333345678901"})
        assert dr.recover_channel_id(d) == "333345678901"

    def test_returns_empty_when_nothing_is_recoverable(self, region):
        d = region / "channels" / "56789012"
        d.mkdir(parents=True)
        (d / "2026-05-01.jsonl").write_text("{ 半截\n", encoding="utf-8")
        _jsonl(d / "2026-05-02.jsonl", {"id": "9"})  # 无 channel_id 字段
        assert dr.recover_channel_id(d) == ""

    def test_empty_directory_yields_empty(self, region):
        d = region / "channels" / "67890123"
        d.mkdir(parents=True)
        assert dr.recover_channel_id(d) == ""


class TestCollectHistoricalNames:
    def test_harvests_both_index_and_guild_meta_on_disk(self, region, monkeypatch):
        (region / "channel_index.json").write_text(json.dumps({
            "111": {"name": "general"}, "222": {"name": ""}, "333": "不是字典",
        }), encoding="utf-8")
        (region / "guild_meta.json").write_text(json.dumps({
            "channels": [{"id": "444", "name": "off-topic"}, {"id": "", "name": "无 id"},
                         "不是字典"],
        }), encoding="utf-8")
        monkeypatch.setattr(dr, "_REPO_ROOT", region.parent)  # 仓内 → 走 git 分支
        monkeypatch.setattr(dr.subprocess, "run", _no_git)

        names = dr.collect_historical_names(region)
        assert names == {"111": "general", "444": "off-topic"}, "空名与无 id 条目不入表"

    def test_git_history_versions_are_merged_oldest_last(self, region, monkeypatch):
        """名字取**最早出现**的那一份（setdefault）——现存档案缺名时才由历史补上。"""
        monkeypatch.setattr(dr, "_REPO_ROOT", region.parent)

        shown = {
            "sha-new": {"555": {"name": "renamed-later"}},
            "sha-old": {"555": {"name": "original"}, "666": {"name": "long-gone"}},
        }

        class _R:
            def __init__(self, stdout, returncode=0):
                self.stdout = stdout
                self.returncode = returncode

        def fake_run(cmd, **kwargs):  # noqa: ARG001
            if cmd[1] == "log":
                return _R("sha-new\nsha-old\n" if cmd[-1].endswith("channel_index.json") else "")
            sha = cmd[2].split(":", 1)[0]
            return _R(json.dumps(shown[sha]))

        monkeypatch.setattr(dr.subprocess, "run", fake_run)
        names = dr.collect_historical_names(region)
        assert names == {"555": "renamed-later", "666": "long-gone"}

    def test_git_unavailable_degrades_instead_of_failing(self, region, monkeypatch):
        """git 不在 PATH / 超时：名字回收退化为空，绝不让整轮对账挂掉。"""
        (region / "channel_index.json").write_text(
            json.dumps({"777": {"name": "still-here"}}), encoding="utf-8"
        )
        monkeypatch.setattr(dr, "_REPO_ROOT", region.parent)

        def boom(*_a, **_kw):
            raise FileNotFoundError("git")

        monkeypatch.setattr(dr.subprocess, "run", boom)
        assert dr.collect_historical_names(region) == {"777": "still-here"}

    def test_paths_outside_the_repo_skip_the_git_probe(self, region, monkeypatch):
        """数据湖已分仓（`BIAV_SC_DATA_ROOT`）：区服根本就不在 code 仓里。"""
        monkeypatch.setattr(dr, "_REPO_ROOT", Path("/nonexistent-repo-root"))
        monkeypatch.setattr(dr.subprocess, "run", _explode)
        assert dr.collect_historical_names(region) == {}

    def test_corrupt_files_on_disk_are_tolerated(self, region, monkeypatch):
        (region / "channel_index.json").write_text("{ 半截", encoding="utf-8")
        monkeypatch.setattr(dr, "_REPO_ROOT", Path("/nonexistent-repo-root"))
        assert dr.collect_historical_names(region) == {}


def _no_git(cmd, **kwargs):  # noqa: ARG001
    class _R:
        stdout = ""
        returncode = 1
    return _R()


def _explode(*_a, **_kw):
    raise AssertionError("仓外路径不该触发 git 探测")


class TestReconcileRegion:
    def test_orphan_dirs_are_registered_so_every_dir_can_be_looked_up(self, region):
        """对账后每个归档目录都必须能由索引反查——这正是 T35 的验收条件。"""
        (region / "channel_index.json").write_text(json.dumps({
            "111123456789": {"name": "general", "dir": "23456789", "status": "active"},
        }), encoding="utf-8")
        _chan(region, "23456789", "111123456789")           # 已在索引
        _chan(region, "34567890", "222234567890")           # 孤儿，历史里有名字
        _chan(region, "45678901", "333345678901")           # 孤儿，名字不可考

        stats = dr.reconcile_region(region, names={"222234567890": "ex-fanart"})

        assert stats == {"dirs": 3, "index_before": 1, "orphans": 2, "named": 1,
                         "unrecovered": 0, "index_after": 3}
        index = json.loads((region / "channel_index.json").read_text(encoding="utf-8"))
        assert index["222234567890"] == {
            "name": "ex-fanart", "type": "unknown", "parent_id": "",
            "dir": "34567890", "status": "orphan",
        }
        assert index["333345678901"]["name"] == "", "名字不可考也要登记（否则目录仍不可反查）"
        assert {v["dir"] for v in index.values()} == {"23456789", "34567890", "45678901"}

    def test_unrecoverable_id_is_counted_and_named_in_the_log(self, region, caplog):
        d = region / "channels" / "99999999"
        d.mkdir(parents=True)
        (d / "2026-05-01.jsonl").write_text("{ 半截\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            stats = dr.reconcile_region(region, names={})
        assert (stats["orphans"], stats["unrecovered"]) == (1, 1)
        assert "无法从 JSONL 恢复 channel_id" in caplog.text

    def test_dry_run_writes_nothing(self, region):
        _chan(region, "34567890", "222234567890")
        stats = dr.reconcile_region(region, names={}, dry_run=True)
        assert stats["orphans"] == 1
        assert not (region / "channel_index.json").exists(), "--dry-run 不许留下任何字节"

    def test_write_back_is_atomic(self, region, monkeypatch):
        """写回走 tmp + os.replace（与本波三处原子写同一手法）：写一半被杀，旧索引仍可解析。

        `channel_index.json` 撕裂的代价不是「少一次更新」——归档器读不出就
        「rebuilding from scratch」，offline/orphan 条目当场蒸发，而孤儿名字自全仓压扁后
        已无 git 历史可再回收（CLAUDE.md §6.3），一次撕裂即永久失名。
        """
        good = {"111123456789": {"name": "general", "dir": "23456789", "status": "active"}}
        (region / "channel_index.json").write_text(json.dumps(good), encoding="utf-8")
        _chan(region, "23456789", "111123456789")
        _chan(region, "34567890", "222234567890")

        def half_then_die(obj, fh, **_kw):  # noqa: ARG001
            fh.write('{"half": "written…')
            raise RuntimeError("killed mid-write")

        monkeypatch.setattr(dr.json, "dump", half_then_die)
        with pytest.raises(RuntimeError, match="killed mid-write"):
            dr.reconcile_region(region, names={})

        assert json.loads((region / "channel_index.json").read_text(encoding="utf-8")) == good

    def test_successful_write_back_leaves_no_temp_file(self, region):
        _chan(region, "34567890", "222234567890")
        dr.reconcile_region(region, names={})
        assert [p.name for p in region.iterdir() if p.suffix == ".tmp"] == []
        assert (region / "channel_index.json").exists()

    def test_missing_channels_dir_is_not_an_error(self, tmp_path):
        root = tmp_path / "jp"
        root.mkdir()
        assert dr.reconcile_region(root, names={}, dry_run=True)["dirs"] == 0

    def test_names_are_collected_when_not_injected(self, region, monkeypatch):
        """不传 names 时自行回收——这条路径是 CLI 的实际走法。"""
        calls = []
        monkeypatch.setattr(dr, "collect_historical_names",
                            lambda root: calls.append(root) or {})
        dr.reconcile_region(region, dry_run=True)
        assert calls == [region]


class TestReconcileCli:
    def test_no_region_roots_is_an_error(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(dr, "DISCORD_ROOT", tmp_path / "empty")
        monkeypatch.setattr(sys, "argv", ["discord_reconcile.py"])
        with caplog.at_level(logging.ERROR):
            assert dr.main() == 1
        assert "no discord region roots" in caplog.text

    def test_reports_each_region(self, tmp_path, monkeypatch, caplog):
        discord_root = tmp_path / "discord"
        for area in ("global", "jp"):
            (discord_root / area / "channels").mkdir(parents=True)
        _chan(discord_root / "global", "34567890", "222234567890")
        monkeypatch.setattr(dr, "DISCORD_ROOT", discord_root)
        monkeypatch.setattr(dr, "collect_historical_names", lambda root: {})  # noqa: ARG005
        monkeypatch.setattr(sys, "argv", ["discord_reconcile.py", "--dry-run"])

        with caplog.at_level(logging.INFO):
            assert dr.main() == 0

        assert "global: 目录 1" in caplog.text
        assert "jp: 目录 0" in caplog.text
        assert "[dry-run]" in caplog.text
        assert not (discord_root / "global" / "channel_index.json").exists()

    def test_region_roots_come_from_the_layout_ssot(self, tmp_path):
        """区服根解析只有一个真相源——本档不许自己另立一套路径知识。"""
        discord_root = tmp_path / "discord"
        (discord_root / "global" / "channels").mkdir(parents=True)
        assert set(archive_layout.discord_region_roots(discord_root)) == {"global"}


class TestMergeChannelIndex:
    def test_offline_and_orphan_statuses_are_preserved(self):
        merged = dr.merge_channel_index(
            existing={
                "a": {"name": "still-online"},
                "b": {"name": "went-dark"},
                "c": {"name": "", "status": "orphan"},
            },
            current={"a": {"name": "still-online", "dir": "aaaa"}},
        )
        assert merged["a"]["status"] == "active"
        assert merged["b"]["status"] == "offline", "下线条目保留标 offline，不许覆盖蒸发"
        assert merged["c"]["status"] == "orphan", "孤儿登记不许被降级成 offline"

    def test_current_entries_win_on_content(self):
        merged = dr.merge_channel_index(
            existing={"a": {"name": "old-name", "dir": "aaaa"}},
            current={"a": {"name": "new-name", "dir": "aaaa"}},
        )
        assert merged["a"]["name"] == "new-name"


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__)), "-v"]))
