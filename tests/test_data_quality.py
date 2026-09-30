import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import data_quality
from data_quality import SilentPlatformTracker, is_hot_normalized, normalize_engagement


class TestNormalizeEngagement(unittest.TestCase):
    def test_bilibili_views_only_uses_view_weight(self):
        # No interaction metadata → engagement treated as play count * 0.001
        item = {"source": "bilibili", "engagement": 50000}
        self.assertEqual(normalize_engagement(item), 50.0)

    def test_bilibili_weighted_interactions(self):
        item = {"source": "bilibili", "engagement": 0, "metadata": {
            "play": 10000, "like": 5, "coin": 2, "favorite": 3, "share": 1}}
        # 10000*0.001 + 5 + 2*2 + 3*2 + 1*3 = 28
        self.assertEqual(normalize_engagement(item), 28.0)

    def test_weibo_weighted_formula(self):
        item = {"source": "weibo", "engagement": 0, "metadata": {
            "reposts_count": 10, "comments_count": 5, "attitudes_count": 20}}
        # 10*3 + 5*2 + 20*1 = 60
        self.assertEqual(normalize_engagement(item), 60.0)

    def test_weibo_without_metadata_uses_engagement_as_likes(self):
        item = {"source": "weibo", "engagement": 7}
        self.assertEqual(normalize_engagement(item), 7.0)

    def test_youtube_weighted_formula(self):
        item = {"source": "youtube", "engagement": 0, "metadata": {
            "viewCount": 100000, "likeCount": 10, "commentCount": 5}}
        # 100000*0.0001 + 10 + 5 = 25
        self.assertEqual(normalize_engagement(item), 25.0)

    def test_flat_platform_passthrough(self):
        self.assertEqual(normalize_engagement({"source": "reddit", "engagement": 42}), 42.0)

    def test_unknown_source_default_weight(self):
        self.assertEqual(normalize_engagement({"source": "mystery", "engagement": 9}), 9.0)

    def test_missing_fields_default_to_zero(self):
        self.assertEqual(normalize_engagement({}), 0.0)


class TestIsHotNormalized(unittest.TestCase):
    def test_reddit_threshold_boundary(self):
        # Threshold is inclusive (>=)
        self.assertTrue(is_hot_normalized({"source": "reddit", "engagement": 50}))
        self.assertFalse(is_hot_normalized({"source": "reddit", "engagement": 49}))

    def test_bilibili_threshold_on_normalized_score(self):
        # 100000 plays → score 100 == threshold; 99999 → 99.999 just below
        self.assertTrue(is_hot_normalized({"source": "bilibili", "engagement": 100000}))
        self.assertFalse(is_hot_normalized({"source": "bilibili", "engagement": 99999}))

    def test_unknown_source_uses_default_threshold(self):
        self.assertTrue(is_hot_normalized({"source": "mystery", "engagement": 50}))
        self.assertFalse(is_hot_normalized({"source": "mystery", "engagement": 49}))


class TestSilentPlatformTracker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.health_path = Path(self._tmp.name) / "source-health.json"

    def _tracker(self):
        return SilentPlatformTracker(health_path=self.health_path)

    def test_successful_collection_marks_active(self):
        t = self._tracker()
        t.update_platform_status("reddit", items_count=5)
        p = t.health_data["platforms"]["reddit"]
        self.assertEqual(p["level"], SilentPlatformTracker.LEVEL_ACTIVE)
        self.assertEqual(p["consecutive_silent_days"], 0)
        self.assertEqual(p["total_items"], 5)

    def test_state_persisted_to_file_and_reloaded(self):
        self._tracker().update_platform_status("reddit", items_count=3)
        self.assertTrue(self.health_path.exists())
        # A fresh instance must reload the saved state
        t2 = self._tracker()
        self.assertEqual(t2.health_data["platforms"]["reddit"]["total_items"], 3)

    def test_silent_day_counted_once_per_day(self):
        t = self._tracker()
        t.update_platform_status("weibo", items_count=0)
        t.update_platform_status("weibo", items_count=0)  # same day, no double count
        p = t.health_data["platforms"]["weibo"]
        self.assertEqual(p["consecutive_silent_days"], 1)
        self.assertEqual(p["level"], SilentPlatformTracker.LEVEL_ACTIVE)

    def _seed_silent(self, t, platform, silent_days):
        # Seed prior-day state so the next zero-items update increments the counter.
        t.health_data["platforms"][platform] = {
            "level": SilentPlatformTracker.LEVEL_ACTIVE,
            "last_success_date": None,
            "last_check_date": "2000-01-01",
            "consecutive_silent_days": silent_days,
            "total_items": 0,
            "errors": [],
        }

    def test_degraded_after_seven_silent_days(self):
        t = self._tracker()
        self._seed_silent(t, "nga", SilentPlatformTracker.DEGRADED_THRESHOLD - 1)
        t.update_platform_status("nga", items_count=0)
        self.assertEqual(t.get_platform_level("nga"), SilentPlatformTracker.LEVEL_DEGRADED)
        self.assertFalse(t.should_skip_platform("nga"))

    def test_dormant_after_thirty_silent_days(self):
        t = self._tracker()
        self._seed_silent(t, "taptap", SilentPlatformTracker.DORMANT_THRESHOLD - 1)
        t.update_platform_status("taptap", items_count=0)
        self.assertEqual(t.get_platform_level("taptap"), SilentPlatformTracker.LEVEL_DORMANT)
        self.assertTrue(t.should_skip_platform("taptap"))

    def test_recovery_resets_to_active(self):
        t = self._tracker()
        self._seed_silent(t, "taptap", SilentPlatformTracker.DORMANT_THRESHOLD)
        t.health_data["platforms"]["taptap"]["level"] = SilentPlatformTracker.LEVEL_DORMANT
        t.update_platform_status("taptap", items_count=2)
        p = t.health_data["platforms"]["taptap"]
        self.assertEqual(p["level"], SilentPlatformTracker.LEVEL_ACTIVE)
        self.assertEqual(p["consecutive_silent_days"], 0)

    def test_unknown_platform_defaults_active(self):
        t = self._tracker()
        self.assertEqual(t.get_platform_level("never-seen"), SilentPlatformTracker.LEVEL_ACTIVE)
        self.assertFalse(t.should_skip_platform("never-seen"))

    def test_errors_truncated_and_capped(self):
        t = self._tracker()
        for i in range(12):
            t.update_platform_status("zhihu", items_count=0, error="x" * 500 + str(i))
        errors = t.health_data["platforms"]["zhihu"]["errors"]
        self.assertEqual(len(errors), 10)  # only the most recent 10 kept
        self.assertEqual(len(errors[-1]["error"]), 200)  # truncated

    def test_report_buckets_by_level(self):
        t = self._tracker()
        t.update_platform_status("reddit", items_count=5)
        self._seed_silent(t, "nga", SilentPlatformTracker.DEGRADED_THRESHOLD - 1)
        t.update_platform_status("nga", items_count=0)
        report = t.get_report()
        self.assertEqual(report["summary"]["active_count"], 1)
        self.assertEqual(report["summary"]["degraded_count"], 1)
        self.assertEqual(report["summary"]["dormant_count"], 0)
        self.assertEqual(report["degraded_platforms"][0]["platform"], "nga")

    def test_saved_file_is_valid_json_with_timestamp(self):
        self._tracker().update_platform_status("reddit", items_count=1)
        data = json.loads(self.health_path.read_text(encoding="utf-8"))
        self.assertIn("updated_at", data)
        self.assertIn("reddit", data["platforms"])

    def test_note_recorded_on_silent_then_cleared_on_recovery(self):
        t = self._tracker()
        # Silent update with a note records it (degrade reason annotation).
        t.update_platform_status("nga", items_count=0, note="待配 NGA_COOKIE")
        self.assertEqual(t.health_data["platforms"]["nga"]["note"], "待配 NGA_COOKIE")
        # A successful collection clears the note (line 222 branch).
        t.update_platform_status("nga", items_count=3)
        self.assertNotIn("note", t.health_data["platforms"]["nga"])

    def test_should_skip_dormant_only_after_today_check(self):
        # Dormant but not checked today → still gets one probe (returns False).
        t = self._tracker()
        t.health_data["platforms"]["telegram"] = {
            "level": SilentPlatformTracker.LEVEL_DORMANT,
            "last_check_date": "2000-01-01",
            "consecutive_silent_days": 40,
            "total_items": 0,
            "errors": [],
        }
        self.assertFalse(t.should_skip_platform("telegram"))

    def test_report_buckets_dormant_platform(self):
        # Exercises the dormant else-branch of get_report (line 279).
        t = self._tracker()
        self._seed_silent(t, "telegram", SilentPlatformTracker.DORMANT_THRESHOLD - 1)
        t.update_platform_status("telegram", items_count=0)
        report = t.get_report()
        self.assertEqual(report["summary"]["dormant_count"], 1)
        self.assertEqual(report["dormant_platforms"][0]["platform"], "telegram")


class TestGenerateHealthReport(unittest.TestCase):
    """generate_health_report stitches tracker state + recommendations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = Path(self._tmp.name)
        self.health_path = self.out_dir / "source-health.json"
        # Redirect module-level paths into the temp dir. HEALTH_PATH is the
        # SilentPlatformTracker default arg, frozen at class-def time, so the
        # no-arg tracker built inside generate_health_report() must be steered
        # by wrapping the constructor to inject the temp path.
        _orig_init = data_quality.SilentPlatformTracker.__init__
        tmp_health = self.health_path

        def _init(self, health_path=tmp_health):
            _orig_init(self, health_path=health_path)

        self._patches = [
            mock.patch.object(data_quality, "HEALTH_PATH", self.health_path),
            mock.patch.object(data_quality.SilentPlatformTracker, "__init__", _init),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _seed_health(self):
        t = SilentPlatformTracker(health_path=self.health_path)
        t.update_platform_status("reddit", items_count=5)
        # Force a dormant + degraded platform via direct state.
        t.health_data["platforms"]["nga"] = {
            "level": SilentPlatformTracker.LEVEL_DEGRADED,
            "last_success_date": None, "last_check_date": "2000-01-01",
            "consecutive_silent_days": 8, "total_items": 0, "errors": [],
        }
        t.health_data["platforms"]["telegram"] = {
            "level": SilentPlatformTracker.LEVEL_DORMANT,
            "last_success_date": None, "last_check_date": "2000-01-01",
            "consecutive_silent_days": 35, "total_items": 0, "errors": [],
        }
        t._save_health()

    def test_report_has_no_last_collection(self):
        self._seed_health()
        report = data_quality.generate_health_report()
        self.assertNotIn("last_collection", report)
        # Recommendations cover degraded (investigate) + dormant (skip).
        actions = {(r["platform"], r["action"]) for r in report["recommendations"]}
        self.assertIn(("telegram", "skip"), actions)
        self.assertIn(("nga", "investigate"), actions)

    def test_print_health_report_runs(self):
        # print_health_report walks every print branch; just assert no raise.
        self._seed_health()
        with mock.patch("builtins.print"):
            data_quality.print_health_report()


if __name__ == "__main__":
    unittest.main()
