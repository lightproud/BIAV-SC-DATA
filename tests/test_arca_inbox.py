"""arca.live 收件箱通道离线测试（T111）：CLI 写文件格式、入湖幂等、落点 / 去重键与 collect_global 同构、空 / 被拦文件。
样本全部自造（复用 test_arca_live_collector 的最小 HTML），不含真实帖子与用户名。"""
import json
import tempfile
import unittest
from datetime import datetime, UTC
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401

import arca_live_collector as ac
import archive_platforms
import ingest_inbox
from test_arca_live_collector import (ARTICLE_HTML, CHALLENGE_HTML, LIST_HTML,
                                      FakeResp, FakeSession)


def _handler(url):
    return FakeResp(LIST_HTML) if url == ac.ARCA_LIST_URL else FakeResp(ARTICLE_HTML)


class CliTests(unittest.TestCase):
    def setUp(self):
        for target in (mock.patch.object(ac.time, "sleep"),):
            target.start()
            self.addCleanup(target.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "sub" / "x.json"

    def test_writes_payload_format(self):
        # 窗口取很大，让 2026-09 的旧帖也进来
        payload = ac.run_cli(24 * 365 * 5, self.out, session=FakeSession(_handler))
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(doc, payload)
        self.assertEqual(doc["source"], "arca_live")
        datetime.strptime(doc["collected_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertNotIn("blocked", doc)
        titles = {i["title"] for i in doc["items"]}
        self.assertTrue({"新帖一", "新帖二", "旧帖"} <= titles)
        self.assertTrue(all(i["source"] == "arca_live" for i in doc["items"]))

    def test_blocked_writes_empty_file_with_flag(self):
        for resp in (FakeResp(CHALLENGE_HTML), FakeResp("", 403)):
            ac.run_cli(36, self.out, session=FakeSession(lambda url, r=resp: r))
            doc = json.loads(self.out.read_text(encoding="utf-8"))
            self.assertEqual((doc["items"], doc["blocked"]), ([], True))

    def test_window_empty_is_not_blocked(self):
        ac.run_cli(1, self.out, session=FakeSession(_handler))  # 样本帖都早于 1 小时
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(doc["items"], [])
        self.assertNotIn("blocked", doc)

    def test_main_argparse(self):
        with mock.patch.object(ac, "run_cli", return_value={"items": []}) as m:
            self.assertEqual(ac.main(["--out", str(self.out)]), 0)
            m.assert_called_once_with(36, str(self.out))
            ac.main(["--hours", "12", "--out", str(self.out)])
            self.assertEqual(m.call_args.args[0], 12)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.archive = root / "Record" / "Community"
        self.inbox = root / "inbox"
        self.inbox.mkdir()
        self.state = root / "state.json"
        p = mock.patch.object(archive_platforms, "ARCHIVE_DIR", self.archive)
        p.start()
        self.addCleanup(p.stop)
        mock.patch.object(ac.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def _make_file(self, name):
        ac.run_cli(24 * 365 * 5, self.inbox / name, session=FakeSession(_handler))

    def _read_day(self, day):
        return json.loads((self.archive / "arca_live" / f"{day}.json").read_text(encoding="utf-8"))

    def test_placement_and_item_dates(self):
        self._make_file("20261003T0800.json")
        stats = ingest_inbox.ingest_dir(self.inbox, self.state)
        self.assertEqual(stats["files"], 1)
        # 新帖一 10:00Z / 新帖二 11:00Z = 同为 2026-10-02 北京日；旧帖 2026-09-01 00:00Z = 北京 09-01
        day = self._read_day("2026-10-02")
        self.assertEqual((day["source"], day["date"], day["item_count"]), ("arca_live", "2026-10-02", 2))
        self.assertNotIn("region", day)
        self.assertEqual(self._read_day("2026-09-01")["item_count"], 1)

    def test_same_shape_and_keys_as_archive_all(self):
        """与 collect_global -> archive_all 同构：同一批条目两条路径落出的文件内容一致。"""
        self._make_file("a.json")
        doc = json.loads((self.inbox / "a.json").read_text(encoding="utf-8"))
        ingest_inbox.ingest_dir(self.inbox, self.state)
        via_inbox = self._read_day("2026-10-02")

        other = Path(self.tmp.name) / "other" / "Record" / "Community"
        raw = Path(self.tmp.name) / "run"
        raw.mkdir()
        (raw / "news-raw.json").write_text(json.dumps({"news": doc["items"]}), encoding="utf-8")
        with mock.patch.object(archive_platforms, "ARCHIVE_DIR", other), \
                mock.patch.object(archive_platforms, "RAW_NEWS", raw / "news-raw.json"):
            archive_platforms.archive_all(None, "2026-10-03")
        via_collect = json.loads((other / "arca_live" / "2026-10-02.json").read_text(encoding="utf-8"))
        for d in (via_inbox, via_collect):
            d.pop("archived_at")
        self.assertEqual(via_inbox, via_collect)
        self.assertEqual({archive_platforms.item_key(i) for i in via_inbox["items"]},
                         {"https://arca.live/b/forgettingeve/200", "https://arca.live/b/forgettingeve/201"})

    def test_idempotent_same_file_twice(self):
        self._make_file("a.json")
        s1 = ingest_inbox.ingest_dir(self.inbox, self.state)
        snap = (self.archive / "arca_live" / "2026-10-02.json").read_text(encoding="utf-8")
        s2 = ingest_inbox.ingest_dir(self.inbox, self.state)
        self.assertEqual((s1["files"], s2["files"], s2["skipped"]), (1, 0, 1))
        self.assertEqual((self.archive / "arca_live" / "2026-10-02.json").read_text(encoding="utf-8"), snap)

    def test_idempotent_without_state_and_overlapping_files(self):
        self._make_file("a.json")
        self._make_file("b.json")  # 与 a 内容重叠（同一批帖）
        ingest_inbox.ingest_dir(self.inbox, self.state)
        self.state.unlink()  # 状态丢失重跑：靠 item_key 去重
        ingest_inbox.ingest_dir(self.inbox, self.state)
        self.assertEqual(self._read_day("2026-10-02")["item_count"], 2)
        self.assertEqual(self._read_day("2026-09-01")["item_count"], 1)
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(sorted(st["files"]), ["a.json", "b.json"])

    def test_changed_file_same_name_reingested(self):
        f = self.inbox / "a.json"
        f.write_text(json.dumps({"source": "arca_live", "items": []}), encoding="utf-8")
        ingest_inbox.ingest_dir(self.inbox, self.state)
        self._make_file("a.json")  # 同名内容变了
        s = ingest_inbox.ingest_dir(self.inbox, self.state)
        self.assertEqual(s["files"], 1)
        self.assertEqual(self._read_day("2026-10-02")["item_count"], 2)

    def test_empty_blocked_and_bad_files_do_not_raise(self):
        ac.run_cli(36, self.inbox / "1blocked.json", session=FakeSession(lambda u: FakeResp("", 403)))
        (self.inbox / "2empty.json").write_text("{}", encoding="utf-8")
        (self.inbox / "3bad.json").write_text("{not json", encoding="utf-8")
        (self.inbox / "4items-not-list.json").write_text('{"items": 5}', encoding="utf-8")
        s = ingest_inbox.ingest_dir(self.inbox, self.state)
        self.assertEqual((s["files"], s["bad"], s["items"], s["blocked"]), (2, 2, 0, 1))
        self.assertFalse((self.archive / "arca_live").exists())  # 没有条目就不建空档
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(st["files"]["1blocked.json"]["blocked"])
        self.assertNotIn("3bad.json", st["files"])  # 坏文件不记账，修好后可重入

    def test_main_missing_dir_exit_zero(self):
        self.assertEqual(ingest_inbox.main([str(self.inbox / "nope"), "--state", str(self.state)]), 0)
        self.assertEqual(ingest_inbox.main([str(self.inbox), "--state", str(self.state)]), 0)


if __name__ == "__main__":
    unittest.main()
