"""定向重抓（断档回填）：绕开历史回填闩锁与 Releases 守卫、不动它们，断点续跑，按 ID 去重。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import discord_archiver as da
from datetime import datetime, UTC


def _arch(tmp):
    env = {"DISCORD_BOT_TOKEN": "dummy", "DISCORD_GUILD_ID": "999", "DISCORD_DATA_ROOT": tmp}
    with mock.patch.dict(os.environ, env, clear=False):
        return da.DiscordArchiver()


def _msg_at(day: int, seq: int, month: int = 2):
    dt = datetime(2026, month, day, 12, 0, seq, tzinfo=UTC)
    return {
        "id": str(int(da._sf_from_dt(dt)) + seq), "channel_id": "111", "type": 0,
        "author": {"id": "u1", "username": "tester", "bot": False},
        "content": f"m{seq}", "timestamp": dt.isoformat(),
    }


CHANNELS = [{"id": "111", "name": "general", "type": 0}]


class FakeAPI:
    """按 after 游标分页返回一个固定的消息列表（升序），模拟 Discord /messages?after=。"""

    def __init__(self, msgs):
        self.msgs = sorted(msgs, key=lambda m: int(m["id"]))
        self.calls = 0

    def __call__(self, path, **params):
        self.calls += 1
        after = int(params.get("after", 0))
        out = [m for m in self.msgs if int(m["id"]) > after][: params.get("limit", 100)]
        return list(reversed(out))  # Discord 返回新→旧，归档器自己排序


class TestRefetchMonths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.arch = _arch(self.tmp.name)
        self.arch.state["history_backfill_complete"] = True
        self.arch.state["historical_month"] = None
        st = self.arch._ch_state("111")
        st.update(last_historical_month="2023-07", last_historical_message_id="42", empty_months=["2026-02"])

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, api, months=("2026-02",)):
        with mock.patch.object(self.arch, "fetch_guild_meta", return_value=CHANNELS), \
                mock.patch.object(self.arch, "_api", side_effect=api), \
                mock.patch.object(da.time, "sleep"):
            self.arch.run_refetch_months(list(months))

    def _archived_ids(self):
        ids = []
        for f in (self.arch.data_dir / "channels").rglob("*.jsonl"):
            ids += [json.loads(x)["id"] for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        return ids

    def test_fetches_gap_month_despite_latch_and_empty_mark(self):
        msgs = [_msg_at(d, s) for d in (3, 17) for s in range(3)]
        self._run(FakeAPI(msgs + [_msg_at(2, 0, month=3)]))
        ids = self._archived_ids()
        self.assertEqual(sorted(ids), sorted(m["id"] for m in msgs))  # 3 月那条不收
        st = self.arch._ch_state("111")
        # 历史回填的闩锁、指针、每频道游标与空月标记全部原样
        self.assertTrue(self.arch.state["history_backfill_complete"])
        self.assertIsNone(self.arch.state["historical_month"])
        self.assertEqual((st["last_historical_month"], st["last_historical_message_id"]), ("2023-07", "42"))
        self.assertEqual(st["empty_months"], ["2026-02"])
        self.assertEqual(self.arch.state["refetch_done"], ["2026-02"])
        self.assertNotIn("2026-02", self.arch.state.get("refetch", {}))

    def test_rerun_is_idempotent(self):
        msgs = [_msg_at(5, s) for s in range(4)]
        self._run(FakeAPI(msgs))
        self.arch.state["refetch_done"] = []  # 强制再跑一遍：去重保证不重复写
        self.arch._file_ids_cache.clear()
        self._run(FakeAPI(msgs))
        self.assertEqual(len(self._archived_ids()), 4)

    def test_resumes_from_cursor_after_time_up(self):
        msgs = [_msg_at(d, s) for d in range(1, 28) for s in range(10)]  # 270 条，3 页
        api = FakeAPI(msgs)
        ticks = iter([False, False, True] + [True] * 50)
        with mock.patch.object(self.arch, "_is_time_up", side_effect=lambda: next(ticks)):
            self._run(api)
        self.assertNotIn("2026-02", self.arch.state.get("refetch_done", []))
        cursor = self.arch.state["refetch"]["2026-02"]["111"]
        after_sf, before_sf = da._month_bounds(2026, 2)
        self.assertTrue(int(after_sf) < int(cursor) < int(before_sf))
        first = len(self._archived_ids())
        self.assertTrue(0 < first < 270)
        self._run(api)  # 第二次运行从游标接着抓
        self.assertEqual(sorted(self._archived_ids()), sorted(m["id"] for m in msgs))
        self.assertEqual(self.arch.state["refetch_done"], ["2026-02"])

    def test_main_parses_and_validates_months(self):
        env = {"DISCORD_BOT_TOKEN": "t", "DISCORD_GUILD_ID": "999", "DISCORD_DATA_ROOT": self.tmp.name}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "argv", ["discord_archiver.py", "--refetch-months", "2026-02, 2026-03"]), \
                mock.patch.object(da.DiscordArchiver, "run_refetch_months") as rr:
            da.main()
        rr.assert_called_once_with(["2026-02", "2026-03"])
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "argv", ["discord_archiver.py", "--refetch-months", "2026-13"]), \
                self.assertRaises(SystemExit):
            da.main()


if __name__ == "__main__":
    unittest.main()
