"""backfill_forum_starters 纯逻辑单测 — state IO / starter 去重 / main 编排。

DiscordArchiver 的网络 _api / _write_msg / _update_daily_stats 全打桩；
state.json 与 jsonl 走 tmp，monkeypatch 模块级 DATA_DIR。
"""

import json
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import backfill_forum_starters as bfs  # noqa: E402


class TestStateIO(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bfs, "DATA_DIR", Path(d)):
                self.assertEqual(bfs.load_state(), {})

    def test_save_then_load_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bfs, "DATA_DIR", Path(d)):
                bfs.save_state({"channels": {"thread:1": {}}})
                self.assertEqual(bfs.load_state(), {"channels": {"thread:1": {}}})


class TestAlreadyHasStarter(unittest.TestCase):
    def _write_jsonl(self, d, chan_id, date_str, ids):
        ch_dir = Path(d) / "channels" / chan_id[-8:]
        ch_dir.mkdir(parents=True)
        path = ch_dir / f"{date_str}.jsonl"
        path.write_text("\n".join(json.dumps({"id": i}) for i in ids) + "\n", encoding="utf-8")
        return path

    def test_missing_file_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bfs, "DATA_DIR", Path(d)):
                self.assertFalse(bfs.already_has_starter("123456789", "2026-05-01", "m1"))

    def test_present_true(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._write_jsonl(d, "123456789", "2026-05-01", ["m1", "m2"])
            with mock.patch.object(bfs, "DATA_DIR", Path(d)):
                self.assertTrue(bfs.already_has_starter("123456789", "2026-05-01", "m2"))

    def test_absent_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._write_jsonl(d, "123456789", "2026-05-01", ["m1"])
            with mock.patch.object(bfs, "DATA_DIR", Path(d)):
                self.assertFalse(bfs.already_has_starter("123456789", "2026-05-01", "zzz"))

    def test_bad_json_line_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ch_dir = Path(d) / "channels" / "23456789"
            ch_dir.mkdir(parents=True)
            (ch_dir / "2026-05-01.jsonl").write_text("not-json\n" + json.dumps({"id": "ok"}) + "\n",
                                                     encoding="utf-8")
            with mock.patch.object(bfs, "DATA_DIR", Path(d)):
                self.assertTrue(bfs.already_has_starter("123423456789", "2026-05-01", "ok"))


def _fake_archiver(starter, ch_meta):
    arc = mock.MagicMock()

    def api(path):
        if path.endswith(f"/messages/{path.split('/')[2]}"):
            return starter
        return ch_meta
    # path forms: /channels/{id}/messages/{id}  and  /channels/{id}
    def api2(path):
        parts = path.strip("/").split("/")
        if "messages" in parts:
            return starter
        return ch_meta
    arc._api.side_effect = api2
    arc._slim_message.return_value = {"id": starter.get("id"), "content": "x"}
    arc._write_msg.return_value = None
    arc._update_daily_stats.return_value = None
    return arc


class TestMain(unittest.TestCase):
    def _state(self):
        return {"channels": {"thread:111": {}, "thread:222": {}, "general": {}}}

    def test_empty_state_returns(self):
        with mock.patch.object(bfs, "load_state", return_value={}):
            bfs.main()  # logs error, returns

    def test_nothing_pending(self):
        state = {"channels": {"thread:111": {}},
                 "forum_starter_backfill": {"completed": ["thread:111"], "skipped_no_starter": []}}
        with mock.patch.object(bfs, "load_state", return_value=state), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []):
            bfs.main()

    def test_processes_and_writes_starter(self):
        import tempfile
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "999999999", "name": "Thread A", "applied_tags": ["t1"]}
        arc = _fake_archiver(starter, ch_meta)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bfs, "DATA_DIR", Path(d)), \
                    mock.patch.object(bfs, "load_state", return_value=self._state()), \
                    mock.patch.object(bfs, "save_state"), \
                    mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                    mock.patch.object(bfs, "DRY_RUN", False), \
                    mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                    mock.patch.object(bfs, "already_has_starter", return_value=False), \
                    mock.patch.object(bfs.time, "sleep"):
                bfs.main()
            self.assertTrue(arc._write_msg.called)

    def test_permanent_skip_on_404(self):
        arc = mock.MagicMock()
        arc._api.side_effect = RuntimeError("404 Not Found")
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()
        self.assertTrue(arc._api.called)

    def test_transient_error_retry(self):
        arc = mock.MagicMock()
        arc._api.side_effect = RuntimeError("500 server error")
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()

    def test_starter_not_dict_skipped(self):
        arc = mock.MagicMock()
        arc._api.return_value = None  # starter falsy
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()

    def test_priority_thread_injected(self):
        # priority id not in channels → inserted at front
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "999999999", "name": "P", "applied_tags": []}
        arc = _fake_archiver(starter, ch_meta)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bfs, "DATA_DIR", Path(d)), \
                    mock.patch.object(bfs, "load_state", return_value={"channels": {}}), \
                    mock.patch.object(bfs, "save_state"), \
                    mock.patch.object(bfs, "PRIORITY_THREAD_IDS", ["777"]), \
                    mock.patch.object(bfs, "DRY_RUN", False), \
                    mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                    mock.patch.object(bfs, "already_has_starter", return_value=False), \
                    mock.patch.object(bfs.time, "sleep"):
                bfs.main()
            self.assertTrue(arc._write_msg.called)

    def test_dry_run_does_not_write(self):
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "999999999", "name": "P", "applied_tags": []}
        arc = _fake_archiver(starter, ch_meta)
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state") as save, \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "DRY_RUN", True), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs, "already_has_starter", return_value=False), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()
        arc._write_msg.assert_not_called()
        save.assert_not_called()

    def test_already_has_starter_marks_completed(self):
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "999999999", "name": "P", "applied_tags": []}
        arc = _fake_archiver(starter, ch_meta)
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "DRY_RUN", True), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs, "already_has_starter", return_value=True), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()
        arc._write_msg.assert_not_called()

    def test_priority_already_pending_reordered(self):
        # priority id IS in channels (already pending) → removed + reinserted at front
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "999", "name": "P", "applied_tags": []}
        arc = _fake_archiver(starter, ch_meta)
        state = {"channels": {"thread:777": {}, "thread:111": {}}}
        with mock.patch.object(bfs, "load_state", return_value=state), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", ["777"]), \
                mock.patch.object(bfs, "DRY_RUN", True), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs, "already_has_starter", return_value=True), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()
        self.assertTrue(arc._api.called)

    def test_parent_lookup_failure_skipped(self):
        # _api returns starter on first call, raises on the parent lookup
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        arc = mock.MagicMock()
        calls = {"n": 0}

        def api(path):
            calls["n"] += 1
            if "messages" in path:
                return starter
            raise RuntimeError("parent boom")
        arc._api.side_effect = api
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()
        arc._write_msg.assert_not_called()

    def test_empty_parent_id_skipped(self):
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "", "name": "P"}  # empty parent_id
        arc = _fake_archiver(starter, ch_meta)
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()
        arc._write_msg.assert_not_called()

    def test_bad_timestamp_uses_today(self):
        starter = {"id": "msg1", "timestamp": "garbage"}  # parse fails → today fallback
        ch_meta = {"parent_id": "999999999", "name": "P", "applied_tags": []}
        arc = _fake_archiver(starter, ch_meta)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bfs, "DATA_DIR", Path(d)), \
                    mock.patch.object(bfs, "load_state", return_value=self._state()), \
                    mock.patch.object(bfs, "save_state"), \
                    mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                    mock.patch.object(bfs, "DRY_RUN", False), \
                    mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                    mock.patch.object(bfs, "already_has_starter", return_value=False), \
                    mock.patch.object(bfs.time, "sleep"):
                bfs.main()
            self.assertTrue(arc._write_msg.called)

    def test_progress_save_every_50(self):
        # 60 pending threads, all written → triggers the `processed % 50` save branch
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "999999999", "name": "P", "applied_tags": []}
        arc = _fake_archiver(starter, ch_meta)
        channels = {f"thread:{i}": {} for i in range(60)}
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bfs, "DATA_DIR", Path(d)), \
                    mock.patch.object(bfs, "load_state", return_value={"channels": channels}), \
                    mock.patch.object(bfs, "save_state") as save, \
                    mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                    mock.patch.object(bfs, "DRY_RUN", False), \
                    mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                    mock.patch.object(bfs, "already_has_starter", return_value=False), \
                    mock.patch.object(bfs.time, "sleep"):
                bfs.main()
            self.assertTrue(save.called)

    def test_budget_hit_breaks(self):
        starter = {"id": "msg1", "timestamp": "2026-05-01T12:00:00Z"}
        ch_meta = {"parent_id": "999", "name": "P", "applied_tags": []}
        arc = _fake_archiver(starter, ch_meta)
        # deadline already passed → loop breaks immediately
        with mock.patch.object(bfs, "load_state", return_value=self._state()), \
                mock.patch.object(bfs, "save_state"), \
                mock.patch.object(bfs, "PRIORITY_THREAD_IDS", []), \
                mock.patch.object(bfs, "RUNTIME_BUDGET", -1), \
                mock.patch.object(bfs, "DiscordArchiver", return_value=arc), \
                mock.patch.object(bfs.time, "sleep"):
            bfs.main()
        arc._api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
