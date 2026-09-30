"""taptap_collector 纯解析 / 转换 / 增量过滤 / 状态读写单测。

不触网、不开浏览器；所有文件写入用临时路径 monkeypatch，绝不污染真实
data/ 目录。覆盖 _parse_num / API body 解析 / DOM 时间委托 / 状态读写 /
增量过滤（_raw_to_item / _filter_incremental）。
"""

import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import taptap_collector  # noqa: E402


class TestParseNum(unittest.TestCase):
    def test_int_and_float_passthrough(self):
        self.assertEqual(taptap_collector._parse_num(42), 42)
        self.assertEqual(taptap_collector._parse_num(3.9), 3)

    def test_empty_and_none(self):
        self.assertEqual(taptap_collector._parse_num(""), 0)
        self.assertEqual(taptap_collector._parse_num(None), 0)

    def test_comma_stripped(self):
        self.assertEqual(taptap_collector._parse_num("1,234"), 1234)

    def test_wan_unit(self):
        self.assertEqual(taptap_collector._parse_num("1.2万"), 12000)
        self.assertEqual(taptap_collector._parse_num("3万"), 30000)

    def test_plain_digits_extracted(self):
        self.assertEqual(taptap_collector._parse_num("赞 88 个"), 88)

    def test_no_digits_returns_zero(self):
        self.assertEqual(taptap_collector._parse_num("无"), 0)


class TestParseTaptapDomTime(unittest.TestCase):
    def test_delegates_to_news_common(self):
        with mock.patch.object(
            taptap_collector.news_common, "parse_relative_time",
            return_value=("2026-06-01T00:00:00+00:00", None),
        ) as m:
            out = taptap_collector._parse_taptap_dom_time("昨天")
        self.assertEqual(out, "2026-06-01T00:00:00+00:00")
        m.assert_called_once_with("昨天")


class TestParseTopicApiBody(unittest.TestCase):
    def test_empty_body(self):
        self.assertEqual(taptap_collector._parse_topic_api_body({}), [])

    def test_non_list_data(self):
        self.assertEqual(taptap_collector._parse_topic_api_body({"data": 123}), [])

    def test_list_at_top_level(self):
        body = {"data": [{"title": "Hello", "id": 7}]}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Hello")
        self.assertEqual(out[0]["item_id"], "7")
        self.assertEqual(out[0]["url"], "https://www.taptap.cn/moment/7")

    def test_known_list_key(self):
        body = {"data": {"topics": [{"title": "T", "id": 1, "like_count": "1.2万"}]}}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertEqual(out[0]["like_count"], 12000)

    def test_nested_one_level(self):
        body = {"data": {"wrapper": {"moments": [{"title": "Deep", "id": 5}]}}}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertEqual(out[0]["title"], "Deep")

    def test_item_without_title_skipped(self):
        body = {"data": [{"id": 1}, {"title": "keep", "id": 2}]}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertEqual([o["title"] for o in out], ["keep"])

    def test_summary_used_as_title_fallback(self):
        body = {"data": [{"summary": "a" * 200, "id": 3}]}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertEqual(len(out[0]["title"]), 100)

    def test_ms_timestamp_converted(self):
        ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
        body = {"data": [{"title": "X", "id": 1, "created_time": ms}]}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertTrue(out[0]["created"].startswith("2026-01-01"))

    def test_string_timestamp(self):
        sec = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
        body = {"data": [{"title": "X", "id": 1, "created_at": str(sec)}]}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertTrue(out[0]["created"].startswith("2026-01-01"))

    def test_author_from_user_dict(self):
        body = {"data": [{"title": "X", "id": 1, "user": {"name": "Alice"}}]}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertEqual(out[0]["author"], "Alice")

    def test_non_dict_items_skipped(self):
        body = {"data": ["garbage", {"title": "ok", "id": 1}]}
        out = taptap_collector._parse_topic_api_body(body)
        self.assertEqual(len(out), 1)


class TestParseReviewApiBody(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(taptap_collector._parse_review_api_body({}), [])

    def test_content_required(self):
        body = {"data": {"reviews": [{"id": 1}, {"content": "good", "id": 2}]}}
        out = taptap_collector._parse_review_api_body(body)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["summary"], "good")

    def test_star_prefix_in_title(self):
        body = {"data": {"reviews": [{"comment": "great", "id": 1, "score": 4}]}}
        out = taptap_collector._parse_review_api_body(body)
        self.assertTrue(out[0]["title"].startswith("★★★★☆"))

    def test_no_score_no_stars(self):
        body = {"data": {"reviews": [{"comment": "ok", "id": 1}]}}
        out = taptap_collector._parse_review_api_body(body)
        self.assertEqual(out[0]["title"], "ok")

    def test_review_url_fallback(self):
        body = {"data": {"reviews": [{"comment": "ok", "id": 9}]}}
        out = taptap_collector._parse_review_api_body(body)
        self.assertEqual(out[0]["url"], "https://www.taptap.cn/review/9")

    def test_nested_rating_list(self):
        body = {"data": {"box": {"rating_list": [{"text": "hi", "id": 1}]}}}
        out = taptap_collector._parse_review_api_body(body)
        self.assertEqual(out[0]["summary"], "hi")


class TestStateIO(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        with mock.patch.object(taptap_collector, "STATE_PATH", Path("/nonexistent/x.json")):
            self.assertEqual(taptap_collector._load_state(), {})

    def test_load_bad_json_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text("{bad json", encoding="utf-8")
            with mock.patch.object(taptap_collector, "STATE_PATH", p):
                self.assertEqual(taptap_collector._load_state(), {})

    def test_save_then_load_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "state.json"
            with mock.patch.object(taptap_collector, "STATE_PATH", p), \
                    mock.patch.object(taptap_collector, "DATA_DIR", Path(d) / "sub"):
                taptap_collector._save_state({"taptap": {"last_post_id": "42"}})
                loaded = taptap_collector._load_state()
        self.assertEqual(loaded["taptap"]["last_post_id"], "42")


def _raw(item_id="", days_ago=0, like=0, comment=0):
    t = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {
        "title": f"t{item_id}", "summary": "s", "like_count": like,
        "comment_count": comment, "created": t, "url": f"u{item_id}",
        "author": "a", "item_id": item_id,
    }


class TestRawToItem(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime.now(UTC) - timedelta(hours=24)

    def test_recent_item_converted(self):
        out = taptap_collector._raw_to_item(_raw("1", like=10, comment=5), "taptap_post", self.cutoff)
        self.assertIsNotNone(out)
        self.assertEqual(out["engagement"], 15)
        self.assertEqual(out["source"], "taptap_post")
        self.assertEqual(out["lang"], "zh")

    def test_old_item_dropped(self):
        out = taptap_collector._raw_to_item(_raw("1", days_ago=5), "taptap_post", self.cutoff)
        self.assertIsNone(out)

    def test_is_hot_threshold(self):
        hot = taptap_collector._raw_to_item(_raw("1", like=51), "taptap_post", self.cutoff)
        cold = taptap_collector._raw_to_item(_raw("2", like=50), "taptap_post", self.cutoff)
        self.assertTrue(hot["is_hot"])
        self.assertFalse(cold["is_hot"])

    def test_bad_created_falls_back_to_now(self):
        raw = _raw("1")
        raw["created"] = "not-a-date"
        out = taptap_collector._raw_to_item(raw, "taptap_post", self.cutoff)
        self.assertIsNotNone(out)


class TestFilterIncremental(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime.now(UTC) - timedelta(hours=24)

    def test_records_first_id_as_new_last(self):
        items, new_last = taptap_collector._filter_incremental(
            [_raw("100"), _raw("99")], "taptap_post", self.cutoff, "", backfill=False
        )
        self.assertEqual(new_last, "100")
        self.assertEqual(len(items), 2)

    def test_incremental_short_circuit_on_last_id(self):
        raws = [_raw("3"), _raw("2"), _raw("1")]
        items, _ = taptap_collector._filter_incremental(
            raws, "taptap_post", self.cutoff, "2", backfill=False
        )
        # stops at id==2; only id 3 kept
        self.assertEqual([i["title"] for i in items], ["t3"])

    def test_backfill_ignores_last_id(self):
        raws = [_raw("3"), _raw("2"), _raw("1")]
        items, _ = taptap_collector._filter_incremental(
            raws, "taptap_post", self.cutoff, "2", backfill=True
        )
        self.assertEqual(len(items), 3)

    def test_existing_last_id_preserved_when_no_new(self):
        # all items lack item_id → new_last keeps the incoming last_id
        raws = [_raw("")]
        _, new_last = taptap_collector._filter_incremental(
            raws, "taptap_post", self.cutoff, "old", backfill=False
        )
        self.assertEqual(new_last, "old")

    def test_old_items_filtered_by_cutoff(self):
        raws = [_raw("1", days_ago=10)]
        items, _ = taptap_collector._filter_incremental(
            raws, "taptap_post", self.cutoff, "", backfill=False
        )
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
