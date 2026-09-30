"""Additional discord_archiver coverage — targets the uncovered branches the
existing test_discord_archiver.py leaves: HTTP retry/backoff, slim/process,
daily-stats save/merge, forum threads, the
run()/run_history_only()/main() pipelines.

All hermetic: no network, no real subprocess (gh/git mocked), tmp data root,
time.sleep neutralised. Mirrors the existing env-patch + _api mock style.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, UTC
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_layout  # 日期基准 SSOT（桶名 = 北京日）
import discord_archiver as da
from discord_archiver import DiscordArchiver, request_with_retry, _month_bounds


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


class _HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")

        class _R:
            status_code = status
        self.response = _R()


# ── request_with_retry ───────────────────────────────────────────────────────

class TestRequestWithRetry(unittest.TestCase):
    def test_success_first_try(self):
        resp = mock.Mock(status_code=200)
        resp.raise_for_status = lambda: None
        fake_requests = mock.Mock()
        fake_requests.request.return_value = resp
        with mock.patch.dict(sys.modules, {"requests": fake_requests}):
            out = request_with_retry("GET", "https://x")
        self.assertIs(out, resp)

    def test_429_then_success(self):
        rl = mock.Mock(status_code=429)
        rl.json.return_value = {"retry_after": 0.01}
        ok = mock.Mock(status_code=200)
        ok.raise_for_status = lambda: None
        fake_requests = mock.Mock()
        fake_requests.request.side_effect = [rl, ok]
        with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
                mock.patch.object(da.time, "sleep"):
            out = request_with_retry("GET", "https://x")
        self.assertIs(out, ok)

    def test_non_retryable_http_error_reraises(self):
        # A 404 HTTPError must not be retried — raised immediately.
        import requests as real_requests

        err = real_requests.exceptions.HTTPError("404")
        err.response = mock.Mock(status_code=404)
        resp = mock.Mock(status_code=404)
        resp.raise_for_status.side_effect = err
        fake_requests = mock.Mock()
        fake_requests.request.return_value = resp
        fake_requests.exceptions = real_requests.exceptions
        with mock.patch.dict(sys.modules, {"requests": fake_requests}):
            with self.assertRaises(real_requests.exceptions.HTTPError):
                request_with_retry("GET", "https://x", max_retries=2)

    def test_retryable_then_exhausts_raises(self):
        import requests as real_requests

        fake_requests = mock.Mock()
        fake_requests.request.side_effect = RuntimeError("boom")
        fake_requests.exceptions = real_requests.exceptions
        with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
                mock.patch.object(da.time, "sleep"):
            with self.assertRaises(RuntimeError):
                request_with_retry("GET", "https://x", max_retries=1)


# ── _api / _load_state error path ────────────────────────────────────────────

class TestApiAndState(unittest.TestCase):
    def test_api_delegates_to_request_with_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            resp = mock.Mock()
            resp.json.return_value = [{"id": "1"}]
            with mock.patch.object(da, "request_with_retry", return_value=resp) as rwr:
                out = arch._api("/channels/x/messages", limit=100)
            self.assertEqual(out, [{"id": "1"}])
            rwr.assert_called_once()

    def test_load_state_corrupt_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state_path.parent.mkdir(parents=True, exist_ok=True)
            arch.state_path.write_text("{bad json")
            state = arch._load_state()
            self.assertEqual(state["channels"], {})

    def test_data_dir_region_layout(self):
        # 2026-07-10 方案甲：guild → discord/<区服>/；未登记 guild 响亮失败。
        with tempfile.TemporaryDirectory():
            with mock.patch.dict(os.environ, {
                "DISCORD_BOT_TOKEN": "t", "DISCORD_GUILD_ID": da.GLOBAL_GUILD_ID,
            }, clear=False):
                os.environ.pop("DISCORD_DATA_ROOT", None)
                arch = DiscordArchiver()
                self.assertEqual(arch.data_dir, da.DISCORD_DATA_DIR / "global")
                with mock.patch.dict(os.environ,
                                     {"DISCORD_GUILD_ID": "1377475512716234902"},
                                     clear=False):
                    self.assertEqual(DiscordArchiver().data_dir,
                                     da.DISCORD_DATA_DIR / "jp")
                with mock.patch.dict(os.environ, {"DISCORD_GUILD_ID": "555"},
                                     clear=False):
                    with self.assertRaises(KeyError):
                        DiscordArchiver()

    def test_missing_token_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                DiscordArchiver()


# ── _slim_message / _process_message / _update_daily_stats ───────────────────

class TestSlimAndProcess(unittest.TestCase):
    def test_slim_message_full_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            raw = _msg(
                1,
                reactions=[{"emoji": {"name": "👍", "id": "e1"}, "count": 3}],
                attachments=[{"id": "a1", "filename": "f.png", "content_type": "image/png", "size": 10, "url": "u"}],
                embeds=[{"type": "rich", "title": "T", "url": "u", "description": "d" * 400}],
                message_reference={"message_id": "ref1"},
                thread={"id": "t1"},
                mentions=[{"id": "m1"}],
                pinned=True,
                flags=2,
            )
            slim = arch._slim_message(raw)
            self.assertEqual(slim["reactions"][0]["emoji"], "👍")
            self.assertEqual(len(slim["embeds"][0]["description"]), 300)
            self.assertEqual(slim["reply_to"], "ref1")
            self.assertTrue(slim["has_thread"])
            self.assertEqual(slim["thread_id"], "t1")
            self.assertEqual(slim["mentions"], ["m1"])

    def test_process_message_queues_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            raw = _msg(1, thread={"id": "t1"})
            arch._process_message(raw, "chan", "general")
            self.assertEqual(arch._pending_threads, ["t1"])

    def test_process_message_bad_timestamp_uses_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            raw = _msg(1, timestamp="garbage")
            slim = arch._process_message(raw, "chan", "general")
            self.assertEqual(slim["id"], "1")

    def test_update_daily_stats_bad_ts_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            slim = arch._slim_message(_msg(1, timestamp="bad"))
            arch._update_daily_stats(slim, "general")
            self.assertEqual(len(arch.daily_stats), 0)

    def test_update_daily_stats_records_reactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            slim = arch._slim_message(_msg(1, reactions=[{"emoji": {"name": "x"}, "count": 5}]))
            arch._update_daily_stats(slim, "general")
            day = next(iter(arch.daily_stats.values()))
            self.assertEqual(day["reactions_total"], 5)
            self.assertEqual(len(day["top_reacted"]), 1)

    def test_write_msg_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            slim = arch._slim_message(_msg(1))
            self.assertTrue(arch._write_msg("chan", "2026-05-03", slim))
            # second write is a dedup no-op
            self.assertFalse(arch._write_msg("chan", "2026-05-03", slim))

    def test_file_ids_reads_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            ch_dir = arch._ch_dir("chan")
            ch_dir.mkdir(parents=True, exist_ok=True)
            fp = ch_dir / "2026-05-03.jsonl"
            fp.write_text(json.dumps({"id": "9"}) + "\n" + "\n" + "{bad\n")
            ids = arch._file_ids(fp)
            self.assertIn("9", ids)


# ── fetch_guild_meta / index ─────────────────────────────────────────────────

class TestBucketDayIsBeijing(unittest.TestCase):
    """消息落哪个日档 = **北京日**，与 archive_layout 的日期基准同源。

    原写法对带 `+00:00` 的 timestamp 直接 strftime，取到 UTC 日，于是每天北京
    00:00–08:00 的消息归进前一天的桶（2026-09-17 全量对账：970 万条里 278 万条
    错位，漂移清一色 -1 天）。这类错位靠既有用例抓不到——它们用的时刻恰好在
    UTC 日与北京日相同的那 16 小时里。本类专挑那 8 小时的边界。
    """

    #: UTC 23:00 = 北京次日 07:00：两套基准在此分岔。
    ACROSS = "2026-05-03T23:30:00.000000+00:00"
    #: UTC 14:41 = 北京 22:41：同日，两套基准一致。
    SAME = "2026-05-03T14:41:39.000000+00:00"

    def test_across_the_boundary_lands_on_the_beijing_day(self):
        self.assertEqual(da._bucket_day(self.ACROSS), "2026-05-04")

    def test_inside_the_shared_window_is_unchanged(self):
        self.assertEqual(da._bucket_day(self.SAME), "2026-05-03")

    def test_bad_timestamp_falls_back_to_today_beijing(self):
        self.assertEqual(da._bucket_day("garbage"), archive_layout.archive_date_str())

    def test_message_file_is_named_by_beijing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch._process_message(_msg(1, ts=self.ACROSS), "chan", "general")
            written = sorted(p.name for p in arch._ch_dir("chan").glob("*.jsonl"))
            self.assertEqual(written, ["2026-05-04.jsonl"])

    def test_forum_thread_replies_land_on_the_beijing_day(self):
        """论坛帖的**回复**与首楼走的是两条落档路径，必须同基准。

        2026-09-21 实测：首楼那条改对了，回复那条还在 strftime UTC 日——合并后两天
        新采的 34,057 条里 132 条因此落错桶，无一例外全带 thread_title。同一个帖的
        首楼和回复分家在两个日子里，比整层偏一天更难看出来。
        """
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            meta = {"thread_id": "t1", "thread_title": "帖", "forum_channel_id": "forum1"}
            replies = [_msg("r1", ts=self.ACROSS)]

            def api(path, **params):
                if path.endswith("/messages/t1"):
                    return _msg("t1", ts=self.ACROSS)
                out, replies[:] = list(replies), []
                return out

            with mock.patch.object(arch, "_api", side_effect=api), \
                    mock.patch.object(arch, "_is_time_up", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                arch._fetch_forum_thread("t1", "forum1", meta)
            written = sorted(p.name for p in arch._ch_dir("forum1").glob("*.jsonl"))
            self.assertEqual(written, ["2026-05-04.jsonl"], "首楼与回复须同落北京日档")

    def test_daily_stats_key_matches_the_bucket(self):
        """activity_daily 的日键必须与 JSONL 日档同基准，否则两份档对不上账。"""
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch._process_message(_msg(1, ts=self.ACROSS), "chan", "general")
            self.assertEqual(list(arch.daily_stats), ["2026-05-04"])
            # 小时分布同折北京时：UTC 23:30 → 北京 07:30
            self.assertEqual(arch.daily_stats["2026-05-04"]["hourly_activity"], {"7": 1})


class TestGuildMeta(unittest.TestCase):
    def test_fetch_guild_meta_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            channels = [{"id": "1", "name": "general", "type": 0, "parent_id": None}]
            with mock.patch.object(arch, "_api", return_value=channels):
                out = arch.fetch_guild_meta()
            self.assertEqual(out, channels)
            self.assertTrue((arch.data_dir / "guild_meta.json").exists())

    def test_save_channel_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch._save_channel_index([
                {"id": "12345678901234567890", "name": "g", "type": 15, "parent_id": "p"},
            ])
            idx = json.loads((arch.data_dir / "channel_index.json").read_text())
            self.assertEqual(idx["12345678901234567890"]["type"], "forum")


# ── cold-start / incremental error branches ──────────────────────────────────

class TestColdStartErrors(unittest.TestCase):
    def test_cold_start_forbidden_marks_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(arch, "_api", side_effect=_HTTPError(403)), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._cold_start_backfill("chan", "general")
            self.assertEqual(total, 0)
            self.assertTrue(arch.state["channels"]["chan"]["forbidden"])

    def test_cold_start_generic_error_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(arch, "_api", side_effect=RuntimeError("net")), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._cold_start_backfill("chan", "general")
            self.assertEqual(total, 0)

    def test_cold_start_non_list_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(arch, "_api", return_value={"not": "list"}), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._cold_start_backfill("chan", "general")
            self.assertEqual(total, 0)

    def test_incremental_forbidden_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["chan"] = {"forbidden": True}
            called = []
            arch._api = lambda *a, **k: called.append(1) or []
            self.assertEqual(arch.fetch_channel_incremental("chan", "g"), 0)
            self.assertEqual(called, [])

    def test_incremental_forbidden_on_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["chan"] = {"last_message_id": "100"}
            with mock.patch.object(arch, "_api", side_effect=_HTTPError(403)), \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_channel_incremental("chan", "g")
            self.assertEqual(total, 0)
            self.assertTrue(arch.state["channels"]["chan"]["forbidden"])

    def test_incremental_generic_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["chan"] = {"last_message_id": "100"}
            with mock.patch.object(arch, "_api", side_effect=RuntimeError("net")), \
                    mock.patch.object(da.time, "sleep"):
                self.assertEqual(arch.fetch_channel_incremental("chan", "g"), 0)


# ── historical month fetch ───────────────────────────────────────────────────

class TestHistoryMonth(unittest.TestCase):
    def test_channel_created_after_month_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            # 频道 snowflake 晚于所查月份 → 该月直接跳过。
            # （原先此处还留着一个写废又没删的 future_id 变量，注释自己都写着
            #  "2023-ish actually; use a recent one"——读者会误以为它参与断言。）
            from discord_archiver import _sf_from_dt
            recent = _sf_from_dt(datetime(2026, 6, 1, tzinfo=UTC))
            res = arch.fetch_channel_history_month(recent, "n", 2023, 1)
            self.assertEqual(res, -1)

    def test_empty_month_skipped_without_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            from discord_archiver import _sf_from_dt
            old_id = _sf_from_dt(datetime(2024, 1, 1, tzinfo=UTC))
            arch.state["channels"][str(old_id)] = {"empty_months": ["2025-05"]}
            called = []
            arch._api = lambda *a, **k: called.append(1) or []
            res = arch.fetch_channel_history_month(old_id, "n", 2025, 5)
            self.assertEqual(res, 0)
            self.assertEqual(called, [])

    def test_already_complete_for_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            from discord_archiver import _sf_from_dt
            old_id = _sf_from_dt(datetime(2024, 1, 1, tzinfo=UTC))
            _, before_sf = _month_bounds(2025, 5)
            arch.state["channels"][str(old_id)] = {
                "last_historical_month": "2025-05",
                "last_historical_message_id": before_sf,
            }
            res = arch.fetch_channel_history_month(old_id, "n", 2025, 5)
            self.assertEqual(res, 0)

    def test_fetch_messages_in_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            from discord_archiver import _sf_from_dt
            old_id = _sf_from_dt(datetime(2024, 1, 1, tzinfo=UTC))
            after_sf, before_sf = _month_bounds(2025, 5)
            mid = str((int(after_sf) + int(before_sf)) // 2)
            calls = [0]

            def fake_api(path, **params):
                calls[0] += 1
                if calls[0] == 1:
                    return [_msg(mid, ts="2025-05-15T00:00:00+00:00")]
                return []

            with mock.patch.object(arch, "_api", side_effect=fake_api), \
                    mock.patch.object(da.time, "sleep"):
                res = arch.fetch_channel_history_month(old_id, "n", 2025, 5)
            self.assertEqual(res, 1)

    def test_history_forbidden_during_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            from discord_archiver import _sf_from_dt
            old_id = _sf_from_dt(datetime(2024, 1, 1, tzinfo=UTC))
            with mock.patch.object(arch, "_api", side_effect=_HTTPError(403)), \
                    mock.patch.object(da.time, "sleep"):
                arch.fetch_channel_history_month(old_id, "n", 2025, 5)
            self.assertTrue(arch.state["channels"][str(old_id)]["forbidden"])

    def test_history_empty_month_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            from discord_archiver import _sf_from_dt
            old_id = _sf_from_dt(datetime(2024, 1, 1, tzinfo=UTC))
            with mock.patch.object(arch, "_api", return_value=[]), \
                    mock.patch.object(da.time, "sleep"):
                res = arch.fetch_channel_history_month(old_id, "n", 2025, 5)
            self.assertEqual(res, 0)
            self.assertIn("2025-05", arch.state["channels"][str(old_id)]["empty_months"])


# ── forum threads ────────────────────────────────────────────────────────────

class TestForum(unittest.TestCase):
    def test_fetch_archived_threads_paginates(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            page1 = {"threads": [{"id": "t1", "thread_metadata": {"archive_timestamp": "2026-01-01"}}],
                     "has_more": True}
            page2 = {"threads": [{"id": "t2", "thread_metadata": {"archive_timestamp": "2025-01-01"}}],
                     "has_more": False}
            with mock.patch.object(arch, "_api", side_effect=[page1, page2]), \
                    mock.patch.object(da.time, "sleep"):
                threads = arch._fetch_archived_threads("forum1", "f")
            self.assertEqual(len(threads), 2)

    def test_fetch_archived_threads_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(arch, "_api", side_effect=RuntimeError("x")):
                self.assertEqual(arch._fetch_archived_threads("forum1", "f"), [])

    def test_fetch_forum_thread_starter_and_replies(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            calls = [0]

            def fake_api(path, **params):
                calls[0] += 1
                if path.endswith("/messages/t1"):  # starter
                    return _msg("t1", ts="2026-05-01T00:00:00+00:00")
                if calls[0] == 2:
                    return [_msg("100", ts="2026-05-02T00:00:00+00:00")]
                return []

            meta = {"thread_id": "t1", "thread_title": "Letter", "forum_channel_id": "forum1"}
            with mock.patch.object(arch, "_api", side_effect=fake_api), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._fetch_forum_thread("t1", "forum1", meta, "200")
            self.assertGreaterEqual(total, 2)

    def test_fetch_forum_thread_skip_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["channels"]["thread:t1"] = {"last_message_id": "500"}
            meta = {"thread_id": "t1", "thread_title": "x", "forum_channel_id": "f"}
            res = arch._fetch_forum_thread("t1", "f", meta, "400")
            self.assertEqual(res, 0)

    def test_fetch_forum_threads_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            active = {"threads": [{"id": "t1", "parent_id": "forum1", "name": "A", "last_message_id": "0"}]}

            def fake_api(path, **params):
                if path.endswith("/threads/active"):
                    return active
                if "archived" in path:
                    return {"threads": [], "has_more": False}
                if path.endswith("/messages/t1"):
                    return {}
                return []

            with mock.patch.object(arch, "_api", side_effect=fake_api), \
                    mock.patch.object(da.time, "sleep"):
                total = arch.fetch_forum_threads("forum1", "f")
            self.assertIsInstance(total, int)

    def test_fetch_thread_incremental(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            calls = [0]

            def fake_api(path, **params):
                calls[0] += 1
                if calls[0] == 1:
                    return [_msg("100")]
                return []

            with mock.patch.object(arch, "_api", side_effect=fake_api), \
                    mock.patch.object(da.time, "sleep"):
                total = arch._fetch_thread_incremental("t1")
            self.assertEqual(total, 1)

    def test_fetch_thread_incremental_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(arch, "_api", side_effect=RuntimeError("x")):
                self.assertEqual(arch._fetch_thread_incremental("t1"), 0)


# ── daily stats save (+ merge) ───────────────────────────────────────────────

class TestSaveDailyStats(unittest.TestCase):
    def test_save_and_merge_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            slim = arch._slim_message(_msg(1, reactions=[{"emoji": {"name": "x"}, "count": 3}]))
            arch._update_daily_stats(slim, "general")
            arch._save_daily_stats()
            date_str = "2026-05-03"
            f = arch.data_dir / "activity_daily" / f"{date_str}.json"
            self.assertTrue(f.exists())
            # second run merges with existing
            slim2 = arch._slim_message(_msg(2, reactions=[{"emoji": {"name": "y"}, "count": 2}]))
            arch.daily_stats.clear()
            arch._update_daily_stats(slim2, "general")
            arch._save_daily_stats()
            data = json.loads(f.read_text())
            self.assertGreaterEqual(data["messages"], 2)


# ── run() / run_history_only() / main() pipelines ────────────────────────────

class TestPipelines(unittest.TestCase):
    def _channels(self):
        return [
            {"id": "111", "name": "text", "type": 0},
            {"id": "222", "name": "forum", "type": 15},
        ]

    def test_run_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["history_backfill_complete"] = True  # short-circuit historical loop
            with mock.patch.object(arch, "fetch_guild_meta", return_value=self._channels()), \
                    mock.patch.object(arch, "fetch_channel_incremental", return_value=3) as fci, \
                    mock.patch.object(arch, "fetch_forum_threads", return_value=0), \
                    mock.patch.object(da.time, "sleep"):
                arch.run()
            fci.assert_called()
            self.assertTrue((arch.data_dir / "state.json").exists())

    def test_catchup_drains_channel_over_quota(self):
        """撞了单轮配额的频道会被反复补抽，直到抽干。

        回归 general-chat（日产 ~7.2k）在 5k 配额下卡死在 2026-08-07 的那个缺陷：
        只有配额、没有追赶轮时一轮只排 5k，日增大于日排 → 游标永不收敛。
        """
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["history_backfill_complete"] = True
            # 频道 111 积压 12k：首轮 5k（撞配额）→ 追赶 5k、2k（抽干）
            drains = iter([da.MAX_MESSAGES_PER_CHANNEL,
                           da.MAX_MESSAGES_PER_CHANNEL,
                           2000])

            def fake_incremental(_cid, _name=""):
                return next(drains, 0)

            with mock.patch.object(arch, "fetch_guild_meta", return_value=self._channels()), \
                    mock.patch.object(arch, "fetch_channel_incremental",
                                      side_effect=fake_incremental) as fci, \
                    mock.patch.object(arch, "fetch_forum_threads", return_value=0), \
                    mock.patch.object(da.time, "sleep"):
                arch.run()
            # 首轮 1 次 + 追赶 2 次；最后一次低于配额即判定抽干、停止追赶
            self.assertEqual(fci.call_count, 3)

    def test_catchup_yields_when_budget_exhausted(self):
        """预算耗尽时追赶轮必须让位——不能为了抽干把作业拖到被 CI 砍掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["history_backfill_complete"] = True
            # 永远抽不干（每轮都撞配额）：没有时间护栏就是死循环
            calls = {"n": 0}

            def time_up():
                calls["n"] += 1
                return calls["n"] > 3

            with mock.patch.object(arch, "fetch_guild_meta", return_value=self._channels()), \
                    mock.patch.object(arch, "fetch_channel_incremental",
                                      return_value=da.MAX_MESSAGES_PER_CHANNEL) as fci, \
                    mock.patch.object(arch, "fetch_forum_threads", return_value=0), \
                    mock.patch.object(arch, "_is_time_up", side_effect=time_up), \
                    mock.patch.object(da.time, "sleep"):
                arch.run()
            # 有限次返回后收尾，且状态照常落盘（下一轮从游标接着抓）
            self.assertLess(fci.call_count, 10)
            self.assertTrue((arch.data_dir / "state.json").exists())

    def test_unresolved_backlog_warns(self):
        """预算耗尽仍在撞配额的频道必须出声——此前这类停摆对所有监控都是隐形的。"""
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(da.logger, "warning") as warn:
                arch._warn_unresolved_backlog([{"id": "111", "name": "morimens-game-chat"}])
            self.assertTrue(warn.called)
            self.assertIn("morimens-game-chat", warn.call_args[0][0])

    def test_drained_backlog_stays_quiet(self):
        """抽干了就不该报警——判据是「撞配额」，不是「游标旧」。

        用游标年龄做判据会把已停用频道全部误报：实测 global 121 个文字频道里 31 个
        游标超 3 天，绝大多数是「已停止使用」类频道（最久 990 天没人发言）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            with mock.patch.object(da.logger, "warning") as warn:
                arch._warn_unresolved_backlog([])
            self.assertFalse(warn.called)

    def test_runtime_budget_env_override(self):
        """作业要能把归档器预算压到 job timeout 之下，给 commit/push 留出空档。"""
        import importlib
        with mock.patch.dict(os.environ, {"DISCORD_RUNTIME_BUDGET": "2100"}, clear=False):
            reloaded = importlib.reload(da)
            self.assertEqual(reloaded.MAX_RUNTIME_SECONDS, 2100)
        importlib.reload(da)  # 还原默认值，避免污染同批其余用例
        self.assertEqual(da.MAX_RUNTIME_SECONDS, 45 * 60)

    def test_run_history_only_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["history_backfill_complete"] = True
            with mock.patch.object(arch, "fetch_guild_meta", return_value=self._channels()), \
                    mock.patch.object(da.time, "sleep"):
                arch.run_history_only()
            self.assertTrue((arch.data_dir / "state.json").exists())

    def test_run_advances_one_month(self):
        # historical_month set, all channels complete → advances
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            channels = [{"id": "111", "name": "text", "type": 0}]
            with mock.patch.object(arch, "fetch_guild_meta", return_value=channels), \
                    mock.patch.object(arch, "fetch_channel_incremental", return_value=0), \
                    mock.patch.object(arch, "fetch_channel_history_month", return_value=0), \
                    mock.patch.object(arch, "_all_channels_done_for_month", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                arch.run()
            self.assertIsNotNone(arch.state.get("historical_month"))

    def test_main_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {
                "DISCORD_BOT_TOKEN": "t", "DISCORD_GUILD_ID": "999", "DISCORD_DATA_ROOT": tmp,
            }, clear=False), \
                    mock.patch.object(sys, "argv", ["discord_archiver.py"]), \
                    mock.patch.object(DiscordArchiver, "run") as run:
                da.main()
            run.assert_called_once()

    def test_main_history_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {
                "DISCORD_BOT_TOKEN": "t", "DISCORD_GUILD_ID": "999", "DISCORD_DATA_ROOT": tmp,
            }, clear=False), \
                    mock.patch.object(sys, "argv", ["discord_archiver.py", "--history-only"]), \
                    mock.patch.object(DiscordArchiver, "run_history_only") as rho:
                da.main()
            rho.assert_called_once()


class TestHistoryLoopBodies(unittest.TestCase):
    """Drive the historical-backfill while-loops in run()/run_history_only with a
    concrete historical_month so the per-month body (fetch + advance) executes."""

    def _channels_text(self):
        return [{"id": "111", "name": "text", "type": 0}]

    def test_run_history_only_completes_one_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["historical_month"] = "2025-05"
            with mock.patch.object(arch, "fetch_guild_meta", return_value=self._channels_text()), \
                    mock.patch.object(arch, "fetch_channel_history_month", return_value=2), \
                    mock.patch.object(arch, "_all_channels_done_for_month", side_effect=[True, False]), \
                    mock.patch.object(da.time, "sleep"):
                arch.run_history_only()
            # advanced to previous month
            self.assertEqual(arch.state.get("historical_month"), "2025-04")

    def test_run_history_only_skips_archived_month(self):
        # All candidate months are in Releases → loop advances by skip path only,
        # never invoking the per-channel history fetch, until backfill latches
        # complete (guild start ahead of all months).
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["historical_month"] = "2025-05"
            with mock.patch.object(arch, "fetch_guild_meta", return_value=self._channels_text()), \
                    mock.patch.object(arch, "_archived_months",
                                      return_value={"2025-05", "2025-04", "2025-03"}), \
                    mock.patch.object(arch, "_guild_start_month", return_value=(2025, 5)), \
                    mock.patch.object(arch, "fetch_channel_history_month") as fch, \
                    mock.patch.object(da.time, "sleep"):
                arch.run_history_only()
            # 2025-05 skipped, then prev month < guild start → latched complete
            self.assertTrue(arch.state.get("history_backfill_complete"))
            fch.assert_not_called()

    def test_run_skips_archived_then_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["historical_month"] = "2025-05"
            channels = [{"id": "111", "name": "text", "type": 0}]
            with mock.patch.object(arch, "fetch_guild_meta", return_value=channels), \
                    mock.patch.object(arch, "fetch_channel_incremental", return_value=0), \
                    mock.patch.object(arch, "_archived_months", return_value={"2025-05"}), \
                    mock.patch.object(arch, "fetch_channel_history_month", return_value=-1), \
                    mock.patch.object(arch, "_all_channels_done_for_month", return_value=False), \
                    mock.patch.object(da.time, "sleep"):
                arch.run()
            self.assertEqual(arch.state.get("historical_month"), "2025-04")

    def test_run_processes_deferred_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = _make_archiver(tmp)
            arch.state["history_backfill_complete"] = True
            channels = [{"id": "111", "name": "text", "type": 0}]
            arch._pending_threads = ["t1", "t2"]
            with mock.patch.object(arch, "fetch_guild_meta", return_value=channels), \
                    mock.patch.object(arch, "fetch_channel_incremental", return_value=0), \
                    mock.patch.object(arch, "_fetch_thread_incremental", return_value=1) as fti, \
                    mock.patch.object(da.time, "sleep"):
                arch.run()
            self.assertEqual(fti.call_count, 2)


if __name__ == "__main__":
    unittest.main()
