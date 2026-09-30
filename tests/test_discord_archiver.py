import os
import tempfile
import unittest
from datetime import datetime, UTC
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

# Import-time side effects are limited to logging.basicConfig (no env reads,
# no network), so the module can be imported directly.
from discord_archiver import (
    DISCORD_EPOCH_MS,
    DiscordArchiver,
    _dt_from_sf,
    _is_forbidden,
    _mstr,
    _month_bounds,
    _prev_month,
    _sf_from_dt,
)


def _msg(mid: int) -> dict:
    """Minimal raw Discord message with a numeric snowflake id."""
    return {
        "id": str(mid),
        "channel_id": "chan",
        "type": 0,
        "author": {"id": "u1", "username": "tester", "bot": False},
        "content": f"msg {mid}",
        "timestamp": "2026-05-03T14:41:39.000000+00:00",
    }


class TestColdStartBackfill(unittest.TestCase):
    """First-ever channel pass pages backward to capture full history, not
    just the latest 100 (regression guard for the volunteer-guild fix)."""

    def _make_archiver(self, tmpdir):
        env = {
            "DISCORD_BOT_TOKEN": "dummy",
            "DISCORD_GUILD_ID": "999",
            "DISCORD_DATA_ROOT": tmpdir,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return DiscordArchiver()

    def test_cold_start_pages_backward_and_sets_cursor_to_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            # 250 messages: ids 1001..1250. Discord returns newest-first per page.
            all_ids = list(range(1001, 1251))

            def fake_api(path, **params):
                before = params.get("before")
                # newest-first ordering
                pool = sorted(all_ids, reverse=True)
                if before is not None:
                    pool = [i for i in pool if i < int(before)]
                return [_msg(i) for i in pool[:100]]

            with mock.patch.object(arch, "_api", side_effect=fake_api), \
                    mock.patch("discord_archiver.time.sleep"):
                total = arch.fetch_channel_incremental("chan", "general")

            self.assertEqual(total, 250)  # full channel, not just latest 100
            st = arch.state["channels"]["chan"]
            self.assertEqual(st["last_message_id"], "1250")  # cursor = newest
            self.assertTrue(st["cold_started"])

    def test_second_run_uses_incremental_after_not_cold_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            arch.state["channels"]["chan"] = {"last_message_id": "2000"}
            seen_params = []

            def fake_api(path, **params):
                seen_params.append(params)
                if "after" in params:  # only new messages after 2000
                    return [_msg(2001), _msg(2002)]
                return []

            with mock.patch.object(arch, "_api", side_effect=fake_api), \
                    mock.patch("discord_archiver.time.sleep"):
                total = arch.fetch_channel_incremental("chan", "general")

            self.assertEqual(total, 2)
            # Existing channel must use after-based incremental, never before-paging
            self.assertTrue(all("after" in p for p in seen_params))
            self.assertFalse(any("before" in p for p in seen_params))


class TestSnowflakeHelpers(unittest.TestCase):
    def test_discord_epoch_constant(self):
        # 2015-01-01T00:00:00Z in milliseconds — the documented Discord epoch.
        epoch = datetime(2015, 1, 1, tzinfo=UTC)
        self.assertEqual(DISCORD_EPOCH_MS, int(epoch.timestamp() * 1000))

    def test_epoch_maps_to_snowflake_zero(self):
        epoch = datetime(2015, 1, 1, tzinfo=UTC)
        self.assertEqual(_sf_from_dt(epoch), "0")

    def test_snowflake_zero_maps_to_epoch(self):
        self.assertEqual(_dt_from_sf("0"), datetime(2015, 1, 1, tzinfo=UTC))

    def test_pre_epoch_datetime_clamps_to_zero(self):
        # Datetimes before the Discord epoch must not produce negative snowflakes.
        self.assertEqual(_sf_from_dt(datetime(2014, 6, 1, tzinfo=UTC)), "0")

    def test_round_trip_millisecond_precision(self):
        # dt -> snowflake -> dt is exact for millisecond-precision datetimes.
        dt = datetime(2023, 7, 15, 12, 34, 56, 789000, tzinfo=UTC)
        self.assertEqual(_dt_from_sf(_sf_from_dt(dt)), dt)

    def test_round_trip_accepts_int_snowflake(self):
        dt = datetime(2026, 6, 10, tzinfo=UTC)
        self.assertEqual(_dt_from_sf(int(_sf_from_dt(dt))), dt)

    def test_snowflake_low_22_bits_are_worker_bits(self):
        # Worker/process/increment bits below bit 22 must not change the timestamp.
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        sf = int(_sf_from_dt(dt))
        self.assertEqual(_dt_from_sf(sf + (1 << 22) - 1), dt)


class TestMonthBounds(unittest.TestCase):
    def test_regular_month(self):
        after_sf, before_sf = _month_bounds(2026, 3)
        self.assertEqual(_dt_from_sf(after_sf), datetime(2026, 3, 1, tzinfo=UTC))
        self.assertEqual(_dt_from_sf(before_sf), datetime(2026, 4, 1, tzinfo=UTC))

    def test_december_rolls_over_to_next_year(self):
        after_sf, before_sf = _month_bounds(2025, 12)
        self.assertEqual(_dt_from_sf(after_sf), datetime(2025, 12, 1, tzinfo=UTC))
        self.assertEqual(_dt_from_sf(before_sf), datetime(2026, 1, 1, tzinfo=UTC))

    def test_leap_year_february_spans_29_days(self):
        after_sf, before_sf = _month_bounds(2024, 2)
        span = _dt_from_sf(before_sf) - _dt_from_sf(after_sf)
        self.assertEqual(span.days, 29)

    def test_non_leap_february_spans_28_days(self):
        after_sf, before_sf = _month_bounds(2023, 2)
        span = _dt_from_sf(before_sf) - _dt_from_sf(after_sf)
        self.assertEqual(span.days, 28)

    def test_bounds_are_strings_and_ordered(self):
        after_sf, before_sf = _month_bounds(2026, 6)
        self.assertIsInstance(after_sf, str)
        self.assertIsInstance(before_sf, str)
        self.assertLess(int(after_sf), int(before_sf))


class TestPrevMonth(unittest.TestCase):
    def test_mid_year(self):
        self.assertEqual(_prev_month(2026, 6), (2026, 5))

    def test_january_rolls_back_to_previous_december(self):
        self.assertEqual(_prev_month(2026, 1), (2025, 12))

    def test_december_stays_in_same_year(self):
        self.assertEqual(_prev_month(2026, 12), (2026, 11))


class TestMstr(unittest.TestCase):
    def test_zero_pads_month(self):
        self.assertEqual(_mstr(2026, 3), "2026-03")

    def test_two_digit_month_unpadded(self):
        self.assertEqual(_mstr(2025, 12), "2025-12")

    def test_zero_pads_year_to_four_digits(self):
        self.assertEqual(_mstr(999, 7), "0999-07")


class TestForbiddenChannelHandling(unittest.TestCase):
    """403 (no-permission) channels must not block the historical backfill
    cursor — regression guard for the JP-guild private-channel deadlock where
    unreadable channels (mod-log, hidden channels, etc.) pinned the cursor
    forever on the first historical month."""

    def _make_archiver(self, tmpdir):
        env = {
            "DISCORD_BOT_TOKEN": "dummy",
            "DISCORD_GUILD_ID": "999",
            "DISCORD_DATA_ROOT": tmpdir,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return DiscordArchiver()

    def test_is_forbidden_detects_403_only(self):
        class _Resp:
            def __init__(self, code):
                self.status_code = code

        class _Err(Exception):
            def __init__(self, code):
                self.response = _Resp(code)

        self.assertTrue(_is_forbidden(_Err(403)))
        self.assertFalse(_is_forbidden(_Err(500)))
        self.assertFalse(_is_forbidden(Exception("no response attribute")))

    def test_forbidden_channel_excluded_from_month_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            month = "2026-05"
            _, before_sf = _month_bounds(2026, 5)
            arch.state["channels"]["readable"] = {
                "last_historical_month": month,
                "last_historical_message_id": before_sf,
            }
            arch.state["channels"]["locked"] = {"forbidden": True}
            self.assertTrue(
                arch._all_channels_done_for_month(["readable", "locked"], month, before_sf),
                "forbidden channel must not block month completion",
            )

    def test_unreached_channel_still_blocks(self):
        # 回归：非 forbidden 且未抓到月末的频道仍须阻止游标推进
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            month = "2026-05"
            _, before_sf = _month_bounds(2026, 5)
            arch.state["channels"]["readable"] = {
                "last_historical_month": month,
                "last_historical_message_id": before_sf,
            }
            arch.state["channels"]["lagging"] = {
                "last_historical_month": month,
                "last_historical_message_id": "0",
            }
            self.assertFalse(
                arch._all_channels_done_for_month(["readable", "lagging"], month, before_sf)
            )

    def test_forbidden_channel_skips_history_fetch_without_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            arch.state["channels"]["locked"] = {"forbidden": True}
            called = []
            arch._api = lambda *a, **k: called.append(1) or []
            result = arch.fetch_channel_history_month("locked", "locked-name", 2026, 5)
            self.assertEqual(result, -1)
            self.assertEqual(called, [], "forbidden channel must not trigger any API call")


class TestBackfillArchiveAwareness(unittest.TestCase):
    """The hourly history backfill must not re-fetch months already moved to
    Releases, and must not re-initialise its pointer forever once it has walked
    back to guild creation — the cleanup/backfill hedge that churned 2.6GB back
    into the working tree (diagnosis 2026-06-21)."""

    def _make_archiver(self, tmpdir):
        env = {
            "DISCORD_BOT_TOKEN": "dummy",
            "DISCORD_GUILD_ID": "999",
            "DISCORD_DATA_ROOT": tmpdir,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return DiscordArchiver()

    def test_archived_months_reads_only_uploaded_entries(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            log = [
                {"month": "2024-01", "uploaded_to_releases": True},   # legacy key
                {"month": "2024-02", "uploaded_to_releases": False},  # upload failed
                {"group": "2024-03", "uploaded_to_releases": True},   # engine key
                {"uploaded_to_releases": True},                       # no month/group
            ]
            (arch.data_dir / "archive-log.json").write_text(json.dumps(log))
            # Reads both legacy 'month' and engine 'group'; skips failed + keyless.
            self.assertEqual(arch._archived_months(), {"2024-01", "2024-03"})

    def test_archived_months_empty_when_no_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            self.assertEqual(arch._archived_months(), set())

    def test_init_does_not_reinit_after_complete(self):
        # Once latched complete, pointer stays None — no perpetual re-fetch.
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            arch.state["history_backfill_complete"] = True
            arch.state["historical_month"] = None
            arch._init_historical_month()
            self.assertIsNone(arch.state.get("historical_month"))

    def test_init_sets_pointer_when_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            arch.state["historical_month"] = None
            arch._init_historical_month()
            self.assertIsNotNone(arch.state.get("historical_month"))

    def test_advance_latches_complete_at_guild_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            # prev month (2023-06) falls before guild start (2023-07) → complete
            arch._advance_historical_month(2023, 7, 2023, 7)
            self.assertIsNone(arch.state.get("historical_month"))
            self.assertTrue(arch.state.get("history_backfill_complete"))

    def test_advance_steps_back_one_month_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = self._make_archiver(tmp)
            arch._advance_historical_month(2024, 6, 2023, 7)
            self.assertEqual(arch.state.get("historical_month"), "2024-05")
            self.assertFalse(arch.state.get("history_backfill_complete"))


if __name__ == "__main__":
    unittest.main()
