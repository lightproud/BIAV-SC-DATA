"""collect_fanart 纯逻辑单测 — ext / refresh / fetch / main 编排。

urllib.request.urlopen 全打桩；IO 走 tmp；channel_index / jsonl / pixiv json 全合成。
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import collect_fanart as cf  # noqa: E402


class TestExtOf(unittest.TestCase):
    def test_known_ext(self):
        self.assertEqual(cf.ext_of("a.PNG?x=1"), "png")
        self.assertEqual(cf.ext_of("a.gif"), "gif")

    def test_default(self):
        self.assertEqual(cf.ext_of("noext"), "jpg")
        self.assertEqual(cf.ext_of("noext", default="png"), "png")


class TestFetch(unittest.TestCase):
    def _cm(self, data):
        payload = mock.MagicMock()
        payload.read.return_value = data
        cm = mock.MagicMock()
        cm.__enter__.return_value = payload
        cm.__exit__.return_value = False
        return cm

    def test_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = str(Path(d) / "o.jpg")
            with mock.patch.object(cf.urllib.request, "urlopen", return_value=self._cm(b"x" * 500)):
                self.assertEqual(cf.fetch("https://x/a.jpg", dest), "ok")
            self.assertTrue(Path(dest).exists())

    def test_empty(self):
        with mock.patch.object(cf.urllib.request, "urlopen", return_value=self._cm(b"tiny")):
            self.assertEqual(cf.fetch("https://x/a.jpg", "/tmp/none"), "empty")

    def test_http_error(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(cf.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(cf.fetch("https://x/a.jpg", "/tmp/none"), "http_404")

    def test_generic_error(self):
        with mock.patch.object(cf.urllib.request, "urlopen", side_effect=urllib.error.URLError("x")):
            out = cf.fetch("https://x/a.jpg", "/tmp/none")
        self.assertTrue(out.startswith("err_"))

    def test_referer_passed(self):
        captured = {}

        def grab(req, timeout=None):
            captured["req"] = req
            return self._cm(b"x" * 500)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cf.urllib.request, "urlopen", side_effect=grab):
                cf.fetch("https://x/a.jpg", str(Path(d) / "o.jpg"), referer="https://ref/")
        self.assertEqual(captured["req"].headers.get("Referer"), "https://ref/")


class TestRefreshDiscord(unittest.TestCase):
    def _cm(self, payload):
        cm = mock.MagicMock()
        cm.__enter__.return_value = payload
        cm.__exit__.return_value = False
        return cm

    def test_maps(self):
        body = {"refreshed_urls": [{"original": "u1", "refreshed": "u1new"}]}
        with mock.patch.object(cf.urllib.request, "urlopen", return_value=self._cm(mock.MagicMock())), \
                mock.patch.object(cf.json, "load", return_value=body), \
                mock.patch.object(cf.time, "sleep"):
            out = cf.refresh_discord(["u1"], "tok")
        self.assertEqual(out, {"u1": "u1new"})

    def test_http_error_swallowed(self):
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        err.read = lambda: b"detail"
        with mock.patch.object(cf.urllib.request, "urlopen", side_effect=err), \
                mock.patch.object(cf.time, "sleep"):
            self.assertEqual(cf.refresh_discord(["u1"], "tok"), {})

    def test_generic_error_swallowed(self):
        with mock.patch.object(cf.urllib.request, "urlopen", side_effect=ValueError("x")), \
                mock.patch.object(cf.time, "sleep"):
            self.assertEqual(cf.refresh_discord(["u1"], "tok"), {})


class TestMain(unittest.TestCase):
    def _build_root(self, d, with_token_chan=True):
        # 2026-07-26：夹具改建在**数据湖布局**下（<BIAV_SC_DATA_ROOT>/Record/Community/），
        # 并经真实 env 根解析——原先 monkeypatch 模块常量 ROOT，恰好绕开了
        # archive_layout 这条真实路径，于是 T62 §7甲 把档案迁出 code 仓后单测照绿、
        # 线上天天崩（死手开关 2026-07-26 首跑抓到）。测试要走生产同一条解析链。
        root = Path(d) / "Record" / "Community"
        root.mkdir(parents=True, exist_ok=True)
        # channel_index.json
        idx = {"c1": {"dir": "11111111", "name": "同人创作"},
               "c2": {"dir": "22222222", "name": "general-chat"}}
        ddir = root / "discord"
        ddir.mkdir(parents=True)
        (ddir / "channel_index.json").write_text(json.dumps(idx), encoding="utf-8")
        # fanart channel jsonl with an image attachment
        cdir = ddir / "channels" / "11111111"
        cdir.mkdir(parents=True)
        line = json.dumps({"author_id": "u1", "content": "hi",
                           "attachments": [{"content_type": "image/png", "url": "https://d/a.png",
                                            "id": "att1", "filename": "a.png"}]})
        (cdir / "2026-06-01.jsonl").write_text(line + "\n\n", encoding="utf-8")
        # pixiv platform json —— 落点必须由 archive_layout 现算（与写方 backfill_platforms
        # ._archive_items 同一条解析链）。原夹具手写 `platforms/pixiv/`，那是读方当年抄错的
        # 路径、没有任何写方产出；夹具照抄错路径 => 单测把 bug 钉死成契约，线上恒采 0 条。
        # 这正是本夹具上方注释里记的同一类坑（测试要走生产同一条解析链）。
        import archive_layout as _al
        _pf, _rg, _st = _al.resolve_write_layout("pixiv")
        ppath = root / _al.build_relpath(_pf, _rg, _st, "2026-06-01")
        ppath.parent.mkdir(parents=True, exist_ok=True)
        ppath.write_text(
            json.dumps({"items": [{"media_url": "https://p/i.jpg", "author": "pa", "title": "t",
                                   "url": "https://pixiv/1"}]}), encoding="utf-8")
        return root

    def _run(self, argv, env, out_dir, fetch_status="ok", refresh=None):
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(cf.os.environ, env, clear=True), \
                mock.patch.object(cf, "fetch", return_value=fetch_status), \
                mock.patch.object(cf, "refresh_discord", return_value=(refresh or {})), \
                mock.patch.object(cf.time, "sleep"):
            cf.main()

    def test_full_run_no_token(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._build_root(d)
            out = str(Path(d) / "out")
            with mock.patch.dict(cf.os.environ, {"BIAV_SC_DATA_ROOT": d}):
                self._run(["prog", "--date", "2026-06-01", "--out", out],
                          {"BIAV_SC_DATA_ROOT": d}, out)
            manifest = json.loads(Path(out, "gallery_manifest.json").read_text())
            sources = {g["source"] for g in manifest}
            self.assertIn("discord", sources)
            self.assertIn("pixiv", sources)

    def test_cold_layer_gz_archives_are_read(self):
        """冷层（.gz）归档必须照样采到——回归：读方原先只 isfile() 裸文件。

        冷热分层（CLAUDE.md §5.2）把上上个月及更早的 dated 归档压成 .gz。读方若只看
        裸路径，历史日期恒判「无归档」跳过：不报错、不留痕、采集量静默归零。
        """
        import gzip
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = self._build_root(d)
            # 把两个源的当日归档都压成冷层，裸文件删除（模拟月度压冷后的真实形态）
            for raw in (root / "discord" / "channels" / "11111111" / "2026-06-01.jsonl",
                        *root.glob("pixiv/**/2026-06-01.json")):
                with gzip.open(str(raw) + ".gz", "wt", encoding="utf-8") as gz:
                    gz.write(raw.read_text(encoding="utf-8"))
                raw.unlink()
            out = str(Path(d) / "out")
            with mock.patch.dict(cf.os.environ, {"BIAV_SC_DATA_ROOT": d}):
                self._run(["prog", "--date", "2026-06-01", "--out", out],
                          {"BIAV_SC_DATA_ROOT": d}, out)
            manifest = json.loads(Path(out, "gallery_manifest.json").read_text())
            sources = {g["source"] for g in manifest}
            self.assertIn("discord", sources)
            self.assertIn("pixiv", sources)

    def test_full_run_with_token_refreshes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._build_root(d)
            out = str(Path(d) / "out")
            with mock.patch.dict(cf.os.environ, {"BIAV_SC_DATA_ROOT": d}), \
                    mock.patch.object(cf, "refresh_discord",
                                      return_value={"https://d/a.png": "https://d/a.png?new"}) as rd:
                with mock.patch.object(sys, "argv", ["prog", "--date", "2026-06-01", "--out", out]), \
                        mock.patch.dict(cf.os.environ,
                                        {"DISCORD_BOT_TOKEN": "t", "BIAV_SC_DATA_ROOT": d},
                                        clear=True), \
                        mock.patch.object(cf, "fetch", return_value="ok"), \
                        mock.patch.object(cf.time, "sleep"):
                    cf.main()
                rd.assert_called_once()

    def test_run_missing_pixiv_and_jsonl(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "Record" / "Community"
            ddir = root / "discord" / "global"   # 2026-07-10 方案甲区服布局
            ddir.mkdir(parents=True)
            (ddir / "channels").mkdir()          # 标记新布局根存在
            # channel matches fanart but no jsonl file for the date
            idx = {"c1": {"dir": "11111111", "name": "fanart"}}
            (ddir / "channel_index.json").write_text(json.dumps(idx), encoding="utf-8")
            out = str(Path(d) / "out")
            with mock.patch.dict(cf.os.environ, {"BIAV_SC_DATA_ROOT": d}):
                self._run(["prog", "--date", "2026-06-01", "--out", out],
                          {"BIAV_SC_DATA_ROOT": d}, out)
            manifest = json.loads(Path(out, "gallery_manifest.json").read_text())
            self.assertEqual(manifest, [])


if __name__ == "__main__":
    unittest.main()
