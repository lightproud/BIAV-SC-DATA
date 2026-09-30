"""discord_archiver 降级路径 / 失败隔离不变量（P5，2026-07-02）。

采集器每小时无人值守跑，真实网络环境最常走的恰是错误分支。本档只钉「失败被正确
隔离」这一不变量——单点失败不拖垮整轮、失败后游标不错进、时间片耗尽干净收尾——
不为凑覆盖数字铺 mock 面（testing-strategy 反凑数立场）。

全部密闭：无网络、tmp 数据根、time.sleep 中和；沿用 test_discord_archiver_unit 的
env-patch + 假 _api 风格。
"""

import os
import tempfile
import unittest
from datetime import datetime, UTC
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_layout  # 日期基准 SSOT（桶名 = 北京日）
import discord_archiver as da
from discord_archiver import DiscordArchiver, _sf_from_dt


def _make_archiver(tmpdir, guild="999"):
    env = {
        "DISCORD_BOT_TOKEN": "dummy",
        "DISCORD_GUILD_ID": guild,
        "DISCORD_DATA_ROOT": tmpdir,
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return DiscordArchiver()


def _msg(mid, ts="2026-05-03T14:41:39.000000+00:00", **extra):
    m = {
        "id": str(mid),
        "channel_id": "chan",
        "type": 0,
        "author": {"id": "u1", "username": "tester", "bot": False},
        "content": f"msg {mid}",
        "timestamp": ts,
    }
    m.update(extra)
    return m


def _batch(start, n, ts="2026-05-03T14:41:39.000000+00:00"):
    return [_msg(start + i, ts=ts) for i in range(n)]


# ── 时间片耗尽：干净收尾，游标不错进 ─────────────────────────────────────────

class TestTimeUp(unittest.TestCase):
    def test_cold_start_time_up_keeps_first_batch_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(arch, "_api", return_value=_batch(1000, 100)), \
                    mock.patch.object(arch, "_is_time_up", side_effect=[False, True]), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._cold_start_backfill("chan", "general")
            self.assertEqual(total, 100, "时间片耗尽前的批次必须已入档")
            st = arch.state["channels"]["chan"]
            self.assertEqual(st["last_message_id"], "1099", "游标必须停在已抓最新一条")
            self.assertTrue(st["cold_started"])

    def test_incremental_time_up_leaves_cursor_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["chan"] = {"last_message_id": "500"}
            with mock.patch.object(arch, "_is_time_up", return_value=True), \
                    mock.patch.object(arch, "_api") as api, \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_channel_incremental("chan", "g")
            self.assertEqual(total, 0)
            api.assert_not_called()
            self.assertEqual(arch.state["channels"]["chan"]["last_message_id"], "500",
                             "时间片耗尽不得动游标")

    def test_history_time_up_breaks_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(arch, "_is_time_up", return_value=True), \
                    mock.patch.object(arch, "_api") as api, \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_channel_history_month("123", "g", 2026, 5)
            self.assertEqual(total, 0)
            api.assert_not_called()
            # 断点续传状态仍被持久化（月份 + 游标落盘）
            self.assertEqual(arch.state["channels"]["123"]["last_historical_month"], "2026-05")

    def test_forum_time_up_stops_thread_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            threads = [{"id": "t1", "name": "a"}, {"id": "t2", "name": "b"}]
            with mock.patch.object(arch, "_api", return_value={"threads": []}), \
                    mock.patch.object(arch, "_fetch_archived_threads", return_value=threads), \
                    mock.patch.object(arch, "_is_time_up", return_value=True), \
                    mock.patch.object(arch, "_fetch_forum_thread") as fft, \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_forum_threads("forum1", "论坛")
            self.assertEqual(total, 0)
            fft.assert_not_called()


# ── 采集上限：到顶即停、留待下轮 ─────────────────────────────────────────────

class TestCapHit(unittest.TestCase):
    def test_cold_start_cap_hit_sets_cursor_for_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(da, "MAX_MESSAGES_PER_CHANNEL", 100), \
                    mock.patch.object(arch, "_api", return_value=_batch(2000, 100)), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._cold_start_backfill("chan", "general")
            self.assertEqual(total, 100)
            self.assertEqual(arch.state["channels"]["chan"]["last_message_id"], "2099")

    def test_incremental_cap_hit_persists_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["chan"] = {"last_message_id": "500"}
            with mock.patch.object(da, "MAX_MESSAGES_PER_CHANNEL", 100), \
                    mock.patch.object(arch, "_api", return_value=_batch(600, 100)), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_channel_incremental("chan", "g")
            self.assertEqual(total, 100)
            self.assertEqual(arch.state["channels"]["chan"]["last_message_id"], "699",
                             "到顶后游标必须已推进，下轮从断点继续")

    def test_incremental_empty_response_breaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["chan"] = {"last_message_id": "500"}
            with mock.patch.object(arch, "_api", return_value=[]), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                self.assertEqual(arch.fetch_channel_incremental("chan", "g"), 0)


# ── 历史回填：月界与完成判定 ─────────────────────────────────────────────────

class TestHistoryBoundaries(unittest.TestCase):
    def test_batch_crossing_month_end_stops_at_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            in_month = _sf_from_dt(datetime(2026, 5, 10, tzinfo=UTC))
            next_month = _sf_from_dt(datetime(2026, 6, 2, tzinfo=UTC))
            batch = [_msg(in_month, ts="2026-05-10T00:00:00+00:00"),
                     _msg(next_month, ts="2026-06-02T00:00:00+00:00")]
            with mock.patch.object(arch, "_api", return_value=batch), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_channel_history_month("123", "g", 2026, 5)
            self.assertEqual(total, 1, "只入档月界内消息")
            st = arch.state["channels"]["123"]
            _, before_sf = da._month_bounds(2026, 5)
            self.assertEqual(st["last_historical_message_id"], before_sf,
                             "越界即视为本月完成，游标钉在月末")

    def test_all_channels_done_month_mismatch_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["1"] = {"last_historical_month": "2026-04",
                                           "last_historical_message_id": "999999"}
            self.assertFalse(arch._all_channels_done_for_month(["1"], "2026-05", "500"))


# ── 论坛帖：单点失败隔离 ─────────────────────────────────────────────────────

class TestForumFailureIsolation(unittest.TestCase):
    def test_starter_fetch_failure_does_not_block_replies(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            replies = _batch(9000, 2)

            def api(path, **params):
                if path.endswith("/messages/t1"):
                    raise RuntimeError("starter gone")
                return replies if "after" not in params else []

            with mock.patch.object(arch, "_api", side_effect=api), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._fetch_forum_thread("t1", "forum1", {"thread_id": "t1",
                                                                  "thread_title": "帖",
                                                                  "forum_channel_id": "forum1"})
            self.assertEqual(total, 2, "starter 失败必须被隔离，回复照常入档")

    def test_reply_fetch_failure_keeps_first_pass_cursor_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            starter = _msg("t1")

            def api(path, **params):
                if path.endswith("/messages/t1"):
                    return starter
                raise RuntimeError("replies down")

            with mock.patch.object(arch, "_api", side_effect=api), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._fetch_forum_thread("t1", "forum1", {"thread_id": "t1",
                                                                  "thread_title": "帖",
                                                                  "forum_channel_id": "forum1"})
            self.assertEqual(total, 1, "starter 已入档")
            self.assertEqual(arch.state["channels"].get("thread:t1", {}).get("last_message_id", "0"),
                             "0", "回复抓取失败不得推进游标，下轮必须重试回复")

    def test_starter_bad_timestamp_falls_back_to_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            starter = _msg("t1", ts="not-a-timestamp")

            def api(path, **params):
                if path.endswith("/messages/t1"):
                    return starter
                return []

            with mock.patch.object(arch, "_api", side_effect=api), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._fetch_forum_thread("t1", "forum1", {"thread_id": "t1",
                                                                  "thread_title": "帖",
                                                                  "forum_channel_id": "forum1"})
            self.assertEqual(total, 1)
            # 回退日期与落桶基准同源（北京日）。原断言写的是 UTC 日——那只在一天里
            # 两套基准相同的 16 小时内碰巧成立，北京 00:00–08:00 跑就会红。
            today = archive_layout.archive_date_str()
            self.assertTrue((arch.data_dir / "channels" / "forum1" / f"{today}.jsonl").exists(),
                            "坏时间戳回退今日日期，消息不得丢弃")

    def test_reply_bad_timestamp_falls_back_to_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["thread:t1"] = {"last_message_id": "1"}
            replies = [_msg(5, ts="broken")]

            def api(path, **params):
                return replies if "after" in params else []

            with mock.patch.object(arch, "_api", side_effect=api), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._fetch_forum_thread("t1", "forum1", {"thread_id": "t1",
                                                                  "thread_title": "帖",
                                                                  "forum_channel_id": "forum1"},
                                                 api_last_message_id="9")
            self.assertEqual(total, 1)

    def test_active_threads_endpoint_failure_still_processes_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)

            def api(path, **params):
                if "threads/active" in path:
                    raise RuntimeError("active endpoint down")
                raise AssertionError("unexpected api call")

            archived = [{"id": "t9", "name": "老帖", "applied_tags": ["tag1"],
                         "last_message_id": "42"}]
            captured = {}

            def fake_fetch(t_id, ch_id, meta, api_last):
                captured.update(t_id=t_id, meta=meta)
                return 3

            with mock.patch.object(arch, "_api", side_effect=api), \
                    mock.patch.object(arch, "_fetch_archived_threads", return_value=archived), \
                    mock.patch.object(arch, "_fetch_forum_thread", side_effect=fake_fetch), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_forum_threads("forum1", "论坛")
            self.assertEqual(total, 3, "active 端点挂掉必须被隔离，archived 线照常")
            self.assertEqual(captured["t_id"], "t9")
            self.assertEqual(captured["meta"]["thread_tags"], ["tag1"])

    def test_archived_threads_error_isolated_active_still_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            active = {"threads": [{"id": "t1", "name": "新帖", "parent_id": "forum1"}]}
            with mock.patch.object(arch, "_api", return_value=active), \
                    mock.patch.object(arch, "_fetch_archived_threads",
                                      side_effect=RuntimeError("archived down")), \
                    mock.patch.object(arch, "_fetch_forum_thread", return_value=2), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_forum_threads("forum1", "论坛")
            self.assertEqual(total, 2, "archived 线挂掉必须被隔离，active 线照常")

    def test_duplicate_and_empty_thread_ids_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            threads = [{"id": "t1", "name": "a"}, {"id": "t1", "name": "a-dup"},
                       {"name": "no-id"}]
            with mock.patch.object(arch, "_api", return_value={"threads": []}), \
                    mock.patch.object(arch, "_fetch_archived_threads", return_value=threads), \
                    mock.patch.object(arch, "_fetch_forum_thread", return_value=1) as fft, \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_forum_threads("forum1", "论坛")
            self.assertEqual(fft.call_count, 1, "重复 id 与缺 id 帖必须跳过")
            self.assertEqual(total, 1)


# ── 归档帖分页：坏游标终止而非死循环 ─────────────────────────────────────────

class TestArchivedThreadPagination(unittest.TestCase):
    def test_missing_archive_timestamp_cursor_breaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            page = {"threads": [{"id": "t1", "thread_metadata": {}}], "has_more": True}
            with mock.patch.object(arch, "_api", return_value=page), \
                    mock.patch.object(da.time, "sleep"):
                threads = arch._fetch_archived_threads("forum1", "论坛")
            self.assertEqual(len(threads), 1, "缺 archive_timestamp 游标必须终止分页而非死循环")


# ── 普通 thread 增量 ─────────────────────────────────────────────────────────

class TestThreadIncremental(unittest.TestCase):
    def test_resumes_from_cursor_then_stops_on_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["thread:t1"] = {"last_message_id": "100"}
            calls = []

            def api(path, **params):
                calls.append(params)
                return _batch(101, 100) if len(calls) == 1 else []

            with mock.patch.object(arch, "_api", side_effect=api), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._fetch_thread_incremental("t1")
            self.assertEqual(total, 100)
            self.assertEqual(calls[0].get("after"), "100", "必须从存量游标续抓")
            self.assertEqual(arch.state["channels"]["thread:t1"]["last_message_id"], "200")


if __name__ == "__main__":
    unittest.main()
