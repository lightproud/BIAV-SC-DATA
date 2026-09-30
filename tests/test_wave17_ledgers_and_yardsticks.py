"""审计第十七波 · 台账读错处与量尺读错口径（同波次其余修复）。

前三档钉的是「写」（原子性、顺序、出声）；本档钉的是「读」——同一波里另一类缺陷：
**东西明明在，读的人看错了地方或换错了口径**，于是守卫、报告、评测各自给出一个说得通
但错误的数字。这类缺陷不会抛异常、不会红，只会让判断慢慢失真。

| 位置 | 读错在哪 | 后果 |
|---|---|---|
| `discord_archiver._archived_months` | 只看区服目录，而引擎的 `archive-log.json` 写在 **discord 根** | 「该月已在 Releases → 跳过重抓」恒不成立；45 分钟预算全耗在已有数据上，真正缺的月份永远轮不到 |
| 采集水位 | 水位在核心源失败判定**之前**推进 | reddit 被拦 30 小时期间轮轮标记成功；恢复那轮回溯窗仍是 24h，最早 6 小时整段静默丢失（2026-08-22 起该职责在 collect_global）|
| `kb_telemetry` | 把向量腿的档案 ref 混进「触达概念」计数 | `reach_ratio` 能冲破 100%——拿别人的借阅记录充自己的读者数 |
| `kb_golden_gen` | 期望答案落裸 stem，而判命中是子串匹配 | `community-youtube` ⊂ `community-youtube-comments`：证明「KB 分得清层与平台」的题，却在给错答案打分 |
| `kb_anchor` | 单字 CJK 锚词参与 preview 子串判定 | 「徐州」「徐徐图之」被判 anchored 排到真相关片段前面 |

零网络、零真实数据根：数据目录经 `BIAV_SC_DATA_ROOT` / `DISCORD_DATA_ROOT` 指向 tmp。

小学生比喻：账本没记错，是查账的人翻错了柜子、量身高的人把鞋跟算进去了——数字看着
都挺正常，只是没有一个是真的。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import collect_global as cg  # noqa: E402
import archive_layout  # noqa: E402
import collection_state  # noqa: E402
import discord_archiver as da  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# 一、discord_archiver._archived_months —— 日志在 discord 根，不在区服目录
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def region_archiver(tmp_path, monkeypatch):
    """区服目录 = <lake>/discord/global，引擎日志落在 <lake>/discord/。"""
    lake = tmp_path / "lake"
    region = lake / "Record" / "Community" / "discord" / "global"
    region.mkdir(parents=True)
    monkeypatch.setenv("BIAV_SC_DATA_ROOT", str(lake))
    env = {
        "DISCORD_BOT_TOKEN": "dummy",
        "DISCORD_GUILD_ID": "1131791637933199470",
        "DISCORD_DATA_ROOT": str(region),
    }
    with mock.patch.dict(os.environ, env, clear=False):
        arch = da.DiscordArchiver()
    return arch, region, archive_layout.discord_root()


def _log(*entries):
    return json.dumps(list(entries))


class TestArchivedMonths:
    def test_engine_log_at_the_discord_root_is_read(self, region_archiver):
        """方案甲把 Global 迁进 discord/global 之后，只看区服目录就再也读不到这份日志。"""
        _arch, _region, root = region_archiver
        (root / "archive-log.json").write_text(_log(
            {"source": "discord", "group": "2026-01", "uploaded_to_releases": True},
            {"source": "discord", "group": "2026-02", "uploaded_to_releases": True},
        ), encoding="utf-8")
        arch, _r, _root = region_archiver
        assert arch._archived_months() == {"2026-01", "2026-02"}

    def test_region_local_log_still_counts(self, region_archiver):
        """旧根特例 / 单测形态：区服目录里的日志照读（两处都读，不是改读一处）。"""
        arch, region, _root = region_archiver
        (region / "archive-log.json").write_text(
            _log({"month": "2025-12", "uploaded_to_releases": True}), encoding="utf-8"
        )
        assert arch._archived_months() == {"2025-12"}

    def test_not_yet_uploaded_months_are_excluded(self, region_archiver):
        arch, region, _root = region_archiver
        (region / "archive-log.json").write_text(_log(
            {"group": "2026-03", "uploaded_to_releases": False},
            {"group": "", "uploaded_to_releases": True},
        ), encoding="utf-8")
        assert arch._archived_months() == set()

    @pytest.mark.parametrize("payload", ['{ 半截', '{"not": "a list"}', '[1, "x", null]'])
    def test_malformed_log_yields_empty_not_an_exception(self, region_archiver, payload):
        """坏日志只许让守卫失效（退回重抓），绝不许把整轮跑打死。"""
        arch, region, _root = region_archiver
        (region / "archive-log.json").write_text(payload, encoding="utf-8")
        assert arch._archived_months() == set()

    def test_result_is_cached_per_run(self, region_archiver):
        arch, region, _root = region_archiver
        (region / "archive-log.json").write_text(
            _log({"group": "2026-04", "uploaded_to_releases": True}), encoding="utf-8"
        )
        assert arch._archived_months() == {"2026-04"}
        (region / "archive-log.json").unlink()
        assert arch._archived_months() == {"2026-04"}, "同一轮内只读一次（缓存）"


class TestChannelIndexAtomicity:
    def test_unreadable_index_is_rebuilt_with_a_warning(self, region_archiver, caplog):
        """坏索引会被判成 rebuilding from scratch —— 正是 498 个孤儿目录的成因。"""
        arch, region, _root = region_archiver
        (region / "channel_index.json").write_text("{ 半截", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            arch._save_channel_index([{"id": "111", "name": "general", "type": 0}])
        assert "channel_index.json unreadable" in caplog.text
        index = json.loads((region / "channel_index.json").read_text(encoding="utf-8"))
        assert index["111"]["name"] == "general"

    def test_crash_mid_write_leaves_the_previous_index_readable(
        self, region_archiver, monkeypatch
    ):
        """半截索引会被下一轮判成 unreadable → offline/orphan 全蒸发，孤儿名字永久失落。"""
        import news_common

        arch, region, _root = region_archiver
        good = {"111": {"name": "general", "dir": "111", "status": "active"},
                "222": {"name": "ex-fanart", "dir": "222", "status": "orphan"}}
        (region / "channel_index.json").write_text(json.dumps(good), encoding="utf-8")

        def half_then_die(obj, fh, **_kw):  # noqa: ARG001
            fh.write('{"half": "written…')
            raise RuntimeError("killed mid-write")

        monkeypatch.setattr(news_common.json, "dump", half_then_die)
        with pytest.raises(RuntimeError, match="killed mid-write"):
            arch._save_channel_index([{"id": "111", "name": "general", "type": 0}])

        assert json.loads((region / "channel_index.json").read_text(encoding="utf-8")) == good

    def test_offline_entries_survive_a_rewrite(self, region_archiver):
        """合并式更新：下线频道标 offline 保留，不许覆盖蒸发。"""
        arch, region, _root = region_archiver
        arch._save_channel_index([
            {"id": "111", "name": "general", "type": 0},
            {"id": "222", "name": "gone-later", "type": 0},
        ])
        arch._save_channel_index([{"id": "111", "name": "general", "type": 0}])
        index = json.loads((region / "channel_index.json").read_text(encoding="utf-8"))
        assert index["222"]["status"] == "offline"
        assert index["111"].get("status", "active") != "offline"


# ════════════════════════════════════════════════════════════════════════════
# 二、采集水位 —— 只在整条链路都干净时推进
# ════════════════════════════════════════════════════════════════════════════
# 2026-08-22「采集 → 直接入湖」裁定后 aggregator.py 退役，水位职责随之迁入
# collect_global（唯一采集入口）。本节改测迁入后的实现；原第三例（断言 run() 内部
# 不碰水位）与 aggregator 源码一同退役——它守的是那份源码的形状，形状已不复存在，
# 其语义由下面「核心源失败即不推进水位」一例接管。
# ════════════════════════════════════════════════════════════════════════════

class TestCollectionWatermark:
    def test_mark_collected_advances_the_watermark(self, monkeypatch):
        seen = []

        def recorder(item_count):
            seen.append(item_count)

        monkeypatch.setattr(collection_state, "mark_collection_done", recorder)
        cg._mark_collected(41)
        assert seen == [41]

    def test_mark_collected_is_a_no_op_without_collection_state(self, monkeypatch):
        """可选依赖缺失时优雅降级——水位推进失败不许打断采集链路。"""
        monkeypatch.setitem(sys.modules, "collection_state", None)
        cg._mark_collected(7)  # 不抛即通过

    def test_core_failure_writes_sentinel_and_leaves_watermark_alone(self, monkeypatch, tmp_path):
        """核心源失败：写哨兵 + 不推进水位 + 不非零退出（否则归档步骤被跳过，本轮数据全丢）。

        「部分成功即推进水位」正是 reddit 被拦 30 小时那次的真因：水位一路顶着，
        下一轮回溯窗永远只有 24h，失败期间的条目再没有任何窗口覆盖得到。
        """
        advanced = []
        monkeypatch.setattr(cg, "_mark_collected", lambda n: advanced.append(n))
        monkeypatch.setattr(cg, "FAILURE_FLAG", tmp_path / "collect-failure.flag")
        monkeypatch.setattr(cg, "run_zero_cost_collectors",
                            lambda: ([{"title": "t", "source": "reddit",
                                       "time": "2026-08-22T00:00:00+00:00", "engagement": 1,
                                       "url": "https://example.com/a"}],
                                     [("reddit", "boom")]))
        monkeypatch.setattr(cg.news_common, "dump_json_atomic", lambda *a, **k: None)
        monkeypatch.setattr(cg.news_common, "write_validation_drops", lambda *a, **k: {"total_dropped": 0, "by_source": {}})

        cg.main()  # 不抛 SystemExit 即为「不非零退出」

        assert advanced == [], "核心源失败时水位不许推进"
        assert (tmp_path / "collect-failure.flag").exists(), "核心源失败必须留下哨兵供 CI 标红"
