import json
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import collection_state


def _iso_hours_ago(hours):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


class _StatePathMixin(unittest.TestCase):
    """Redirect STATE_PATH into a temp dir so no real data is touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "data" / "collection_state.json"
        p = mock.patch.object(collection_state, "STATE_PATH", self.state_path)
        p.start()
        self.addCleanup(p.stop)

    def _write_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")


class TestGetLookbackHours(_StatePathMixin):
    def test_no_state_file_returns_default(self):
        self.assertEqual(collection_state.get_lookback_hours(), collection_state.DEFAULT_HOURS)

    def test_no_last_run_key_returns_default(self):
        self._write_state({"some_other_key": 1})
        self.assertEqual(collection_state.get_lookback_hours(), collection_state.DEFAULT_HOURS)

    def test_recent_run_stays_at_default(self):
        # Gap smaller than DEFAULT_HOURS → result clamps up to DEFAULT_HOURS.
        self._write_state({"last_collected_at": _iso_hours_ago(2)})
        self.assertEqual(collection_state.get_lookback_hours(), collection_state.DEFAULT_HOURS)

    def test_large_gap_expands_lookback(self):
        # ~50h gap → expands beyond default but under the 7-day cap.
        self._write_state({"last_collected_at": _iso_hours_ago(50)})
        result = collection_state.get_lookback_hours()
        self.assertGreater(result, collection_state.DEFAULT_HOURS)
        self.assertLessEqual(result, collection_state.MAX_HOURS)
        self.assertAlmostEqual(result, 50 + collection_state.BUFFER_HOURS, delta=1)

    def test_gap_capped_at_max_hours(self):
        # Gap far exceeding the cap → clamped to MAX_HOURS.
        self._write_state({"last_collected_at": _iso_hours_ago(1000)})
        self.assertEqual(collection_state.get_lookback_hours(), collection_state.MAX_HOURS)

    def test_naive_timestamp_assumed_utc(self):
        naive = (datetime.now(UTC) - timedelta(hours=50)).replace(tzinfo=None)
        self._write_state({"last_collected_at": naive.isoformat()})
        result = collection_state.get_lookback_hours()
        self.assertGreater(result, collection_state.DEFAULT_HOURS)

    def test_bad_timestamp_falls_back_to_default(self):
        self._write_state({"last_collected_at": "not-a-timestamp"})
        self.assertEqual(collection_state.get_lookback_hours(), collection_state.DEFAULT_HOURS)

    def test_corrupt_json_treated_as_empty(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(collection_state.get_lookback_hours(), collection_state.DEFAULT_HOURS)


class TestMarkCollectionDone(_StatePathMixin):
    def test_creates_state_file_with_timestamp_and_count(self):
        collection_state.mark_collection_done(item_count=42)
        self.assertTrue(self.state_path.exists())
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_item_count"], 42)
        # Stored timestamp must be parseable ISO.
        datetime.fromisoformat(state["last_collected_at"])
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["items"], 42)

    def test_default_item_count_is_zero(self):
        collection_state.mark_collection_done()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_item_count"], 0)

    def test_history_appends_and_caps_at_48(self):
        # Seed 50 history entries; a new mark should trim to the latest 48.
        self._write_state({
            "history": [{"time": _iso_hours_ago(i), "items": i} for i in range(50)],
        })
        collection_state.mark_collection_done(item_count=7)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["history"]), 48)
        # The just-added entry is the last one kept.
        self.assertEqual(state["history"][-1]["items"], 7)

    def test_roundtrip_with_get_lookback(self):
        # After marking done, the gap is ~0 → lookback returns the default.
        collection_state.mark_collection_done(item_count=1)
        self.assertEqual(collection_state.get_lookback_hours(), collection_state.DEFAULT_HOURS)


if __name__ == "__main__":
    unittest.main()
