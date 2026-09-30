"""backfill_media 纯逻辑单测 — 文件名 / 扩展名 / manifest / URL 收集 / fetch / refresh / main。

网络全打桩：news_common.safe_get、urllib.request.urlopen、subprocess.run 一律 mock。
所有 IO 走 tmp 目录，monkeypatch 模块级 ROOT/FILES/MANIFEST。
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import backfill_media as bm  # noqa: E402


class TestExtAndFname(unittest.TestCase):
    def test_ext_known(self):
        self.assertEqual(bm.ext_of("https://x/a.PNG?sig=1"), "png")
        self.assertEqual(bm.ext_of("https://x/a.jpeg"), "jpeg")
        self.assertEqual(bm.ext_of("https://x/a.mp4"), "mp4")

    def test_ext_default_when_absent(self):
        self.assertEqual(bm.ext_of("https://x/noext"), "jpg")
        self.assertEqual(bm.ext_of("https://x/noext", d="webp"), "webp")

    def test_fname_deterministic(self):
        f1 = bm.fname("https://x/a.png", "discord")
        f2 = bm.fname("https://x/a.png", "discord")
        self.assertEqual(f1, f2)
        self.assertTrue(f1.startswith("discord_"))
        self.assertTrue(f1.endswith(".png"))


class TestManifest(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        with mock.patch.object(bm, "MANIFEST", "/nonexistent/x.json"):
            self.assertEqual(bm.load_manifest(), {})

    def test_save_then_load_roundtrip(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            mpath = str(Path(d) / "sub" / "manifest.json")
            with mock.patch.object(bm, "MANIFEST", mpath):
                bm.save_manifest({"u1": {"status": "ok"}})
                self.assertEqual(bm.load_manifest(), {"u1": {"status": "ok"}})


class TestFetch(unittest.TestCase):
    def _resp(self, content=b"x" * 500, status=200, raise_exc=None):
        r = mock.MagicMock()
        r.content = content
        r.status_code = status
        if raise_exc:
            r.raise_for_status.side_effect = raise_exc
        else:
            r.raise_for_status.return_value = None
        return r

    def test_ok_writes_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = str(Path(d) / "out.jpg")
            with mock.patch.object(bm.news_common, "safe_get", return_value=self._resp()):
                status = bm.fetch("https://x/a.jpg", dest, "discord")
            self.assertEqual(status, "ok")
            self.assertTrue(Path(dest).exists())

    def test_unsafe_url_rejected(self):
        with mock.patch.object(bm.news_common, "safe_get", side_effect=ValueError("bad")):
            self.assertEqual(bm.fetch("https://x/a.jpg", "/tmp/none", "x"), "err_unsafe_url")

    def test_http_error_status(self):
        import requests
        r = self._resp(status=404, raise_exc=requests.HTTPError("404"))
        with mock.patch.object(bm.news_common, "safe_get", return_value=r):
            self.assertEqual(bm.fetch("https://x/a.jpg", "/tmp/none", "x"), "http_404")

    def test_request_exception(self):
        import requests
        r = self._resp(raise_exc=requests.ConnectionError("boom"))
        with mock.patch.object(bm.news_common, "safe_get", return_value=r):
            out = bm.fetch("https://x/a.jpg", "/tmp/none", "x")
        self.assertEqual(out, "err_ConnectionError")

    def test_empty_when_too_small(self):
        with mock.patch.object(bm.news_common, "safe_get", return_value=self._resp(content=b"tiny")):
            self.assertEqual(bm.fetch("https://x/a.jpg", "/tmp/none", "x"), "empty")

    def test_referer_added_for_pixiv(self):
        captured = {}

        def grab(url, headers=None, timeout=None):
            captured["headers"] = headers
            return self._resp()
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bm.news_common, "safe_get", side_effect=grab):
                bm.fetch("https://x/a.jpg", str(Path(d) / "o.jpg"), "pixiv")
        self.assertEqual(captured["headers"].get("Referer"), "https://www.pixiv.net/")


class TestRefreshDiscord(unittest.TestCase):
    def _urlopen_cm(self, payload):
        cm = mock.MagicMock()
        cm.__enter__.return_value = payload
        cm.__exit__.return_value = False
        return cm

    def test_maps_original_to_refreshed(self):
        payload = mock.MagicMock()
        body = {"refreshed_urls": [{"original": "u1", "refreshed": "u1new"}]}
        with mock.patch.object(bm.urllib.request, "urlopen",
                               return_value=self._urlopen_cm(payload)), \
                mock.patch.object(bm.json, "load", return_value=body), \
                mock.patch.object(bm.time, "sleep"):
            out = bm.refresh_discord(["u1"], "token")
        self.assertEqual(out, {"u1": "u1new"})

    def test_exception_swallowed(self):
        with mock.patch.object(bm.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")), \
                mock.patch.object(bm.time, "sleep"):
            out = bm.refresh_discord(["u1"], "token")
        self.assertEqual(out, {})


class TestCollectUrls(unittest.TestCase):
    def _setup(self, d):
        root = Path(d)
        # 摊平布局（2026-06-21）：各平台直接在 Community 根下，与 discord 同级
        pdir = root / "pixiv"
        pdir.mkdir(parents=True)
        (pdir / "2026-04-14.json").write_text(
            json.dumps({"items": [{"media_url": "https://p/img.jpg"}, {"no": "media"}]}),
            encoding="utf-8")
        # platform json as bare list
        bdir = root / "bilibili"
        bdir.mkdir(parents=True)
        (bdir / "2026-04-14.json").write_text(
            json.dumps([{"media_url": "https://b/img.jpg"}]), encoding="utf-8")
        # bad-length filename ignored
        (pdir / "short.json").write_text("[]", encoding="utf-8")
        # discord jsonl
        cdir = root / "discord" / "channels" / "12345678"
        cdir.mkdir(parents=True)
        line = json.dumps({"content_type": "image/png",
                           "attachments": [{"content_type": "image/png", "url": "https://d/a.png"}]})
        (cdir / "2026-04-14.jsonl").write_text(line + "\n", encoding="utf-8")
        return root

    def test_collects_platform_and_discord(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            with mock.patch.object(bm, "SRC", d):
                urls = bm.collect_urls(include_discord=True)
        found = {u for u, _, _ in urls}
        self.assertIn("https://p/img.jpg", found)
        self.assertIn("https://b/img.jpg", found)
        self.assertIn("https://d/a.png", found)

    def test_skip_discord_when_flag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            with mock.patch.object(bm, "SRC", d):
                urls = bm.collect_urls(include_discord=False)
        self.assertNotIn("https://d/a.png", {u for u, _, _ in urls})


class TestUploadRelease(unittest.TestCase):
    def test_no_files_returns_early(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bm, "FILES", str(Path(d) / "empty")), \
                    mock.patch.object(bm.subprocess, "run") as run:
                bm.upload_release()
            run.assert_not_called()

    def test_with_files_invokes_subprocess(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fdir = Path(d) / "files"
            fdir.mkdir()
            (fdir / "a.jpg").write_bytes(b"x")
            result = mock.MagicMock()
            result.stdout = "done"
            result.stderr = ""
            with mock.patch.object(bm, "FILES", str(fdir)), \
                    mock.patch.object(bm, "SRC", d), \
                    mock.patch.object(bm.subprocess, "run", return_value=result) as run:
                bm.upload_release()
            self.assertTrue(run.called)


class TestMain(unittest.TestCase):
    def test_upload_branch(self):
        with mock.patch.object(sys, "argv", ["prog", "--upload"]), \
                mock.patch.object(bm, "upload_release") as up:
            bm.main()
        up.assert_called_once()

    def test_full_run_no_discord(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["prog", "--no-discord", "--budget", "100"]), \
                    mock.patch.object(bm, "FILES", str(Path(d) / "files")), \
                    mock.patch.object(bm, "MANIFEST", str(Path(d) / "manifest.json")), \
                    mock.patch.dict(bm.os.environ, {}, clear=True), \
                    mock.patch.object(bm, "collect_urls",
                                      return_value=[("https://x/a.jpg", "pixiv", "2026-04-14")]), \
                    mock.patch.object(bm, "fetch", return_value="ok"), \
                    mock.patch.object(bm.time, "sleep"):
                bm.main()
            self.assertTrue(Path(d, "manifest.json").exists())

    def test_run_with_discord_no_token_warns(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["prog", "--budget", "100"]), \
                    mock.patch.object(bm, "FILES", str(Path(d) / "files")), \
                    mock.patch.object(bm, "MANIFEST", str(Path(d) / "manifest.json")), \
                    mock.patch.dict(bm.os.environ, {}, clear=True), \
                    mock.patch.object(bm, "collect_urls",
                                      return_value=[("https://d/a.png", "discord", "2026-04-14")]), \
                    mock.patch.object(bm, "fetch", return_value="http_404"), \
                    mock.patch.object(bm.time, "sleep"):
                bm.main()

    def test_run_with_discord_token_refreshes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "argv", ["prog", "--budget", "100"]), \
                    mock.patch.object(bm, "FILES", str(Path(d) / "files")), \
                    mock.patch.object(bm, "MANIFEST", str(Path(d) / "manifest.json")), \
                    mock.patch.dict(bm.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                    mock.patch.object(bm, "collect_urls",
                                      return_value=[("https://d/a.png", "discord", "2026-04-14")]), \
                    mock.patch.object(bm, "refresh_discord",
                                      return_value={"https://d/a.png": "https://d/a.png?new"}) as rd, \
                    mock.patch.object(bm, "fetch", return_value="ok"), \
                    mock.patch.object(bm.time, "sleep"):
                bm.main()
            rd.assert_called_once()

    def test_skips_already_ok_manifest_entries(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            mpath = Path(d) / "manifest.json"
            mpath.write_text(json.dumps({"https://x/a.jpg": {"status": "ok"}}), encoding="utf-8")
            called = {"fetch": 0}

            def counting_fetch(*a, **k):
                called["fetch"] += 1
                return "ok"
            with mock.patch.object(sys, "argv", ["prog", "--no-discord", "--budget", "100"]), \
                    mock.patch.object(bm, "FILES", str(Path(d) / "files")), \
                    mock.patch.object(bm, "MANIFEST", str(mpath)), \
                    mock.patch.dict(bm.os.environ, {}, clear=True), \
                    mock.patch.object(bm, "collect_urls",
                                      return_value=[("https://x/a.jpg", "pixiv", "2026-04-14")]), \
                    mock.patch.object(bm, "fetch", side_effect=counting_fetch), \
                    mock.patch.object(bm.time, "sleep"):
                bm.main()
            self.assertEqual(called["fetch"], 0)


if __name__ == "__main__":
    unittest.main()


# ── 进程级预算（扫描修复 2026-07-28）──────────────────────────────────────────
# 缺陷：--budget 原先只从**下载循环**开始计时，前面还压着 collect_urls 全量遍历
# （数百万条 discord JSONL + 全部平台 json）、去重、以及 refresh_discord 对全部待办
# 链接批量刷签名——三者都不受预算约束。CI 作业于是在进入下载循环之前就撞上
# timeout-minutes: 90。GitHub 侧实测取证：backfill-media.yml 连续 37 天、500+ 次运行
# 无一次成功，每次都是「Backfill 步骤跑满 89 分 47 秒被杀 → 上传与提交 manifest
# 步骤 skipped」，manifest 从不落地，下一轮从同一处重来，恒不前进。
class TestProcessWideBudget(unittest.TestCase):
    def setUp(self):
        # 每个用例重置全局截止时刻，避免互相污染
        bm._DEADLINE = None

    def tearDown(self):
        bm._DEADLINE = None

    def test_no_budget_means_unbounded(self):
        self.assertEqual(bm._budget_left(), float("inf"))
        self.assertFalse(bm._budget_exhausted())
        self.assertEqual(bm._reserve_for_downloads(), 0.0)

    def test_reserve_is_three_tenths_of_budget(self):
        """必须给下载阶段留窗口：遍历/刷新吃干预算 = 那一轮仍然零推进。"""
        bm._set_budget(1000)
        self.assertAlmostEqual(bm._reserve_for_downloads(), 300.0, places=3)

    def test_exhausted_respects_reserve(self):
        with mock.patch.object(bm.time, "time", return_value=bm._START + 800):
            bm._set_budget(1000)  # 截止时刻 = _START + 1000，此刻剩 200s
            # 裸判：还剩 200s，未耗尽
            self.assertFalse(bm._budget_exhausted())
            # 但要为下载留 300s 时，200 < 300 → 视为耗尽，遍历/刷新须收尾
            self.assertTrue(bm._budget_exhausted(reserve=300))

    def test_refresh_discord_stops_when_budget_nearly_spent(self):
        """刷新阶段必须能被预算叫停——原实现会一路刷到作业被杀。"""
        bm._set_budget(1000)
        urls = [f"https://cdn.discordapp.com/a{i}.png" for i in range(bm.REFRESH_BATCH * 5)]
        # 预算已只剩 100s（< 三成预留 300s）→ 一批都不该发
        with mock.patch.object(bm.time, "time", return_value=bm._START + 900), \
                mock.patch.object(bm.urllib.request, "urlopen") as uo, \
                mock.patch.object(bm.time, "sleep"):
            out = bm.refresh_discord(urls, "tok")
        self.assertEqual(out, {})
        uo.assert_not_called()

    def test_collect_urls_stops_walking_when_budget_nearly_spent(self):
        """档案遍历必须能被预算叫停，且已收集的部分照常返回（增量补录，部分即有效）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "bilibili").mkdir()
            (root / "reddit").mkdir()
            bm._set_budget(1000)
            with mock.patch.object(bm, "SRC", str(root)), \
                    mock.patch.object(bm.time, "time", return_value=bm._START + 950):
                urls = bm.collect_urls(include_discord=False)
            self.assertEqual(urls, [])  # 一个平台都没来得及扫，但正常返回而非被杀
