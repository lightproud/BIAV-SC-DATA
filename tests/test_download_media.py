"""download_media 纯函数 + I/O 打桩单测。

所有网络 (news_common.safe_get)、文件系统写入 (monkeypatch 模块级路径常量到
tmp 目录)、subprocess (gh CLI) 一律 mock，绝不污染真实 data/media 目录、绝不触网。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import download_media  # noqa: E402


class TestUrlToFilename(unittest.TestCase):
    def test_stable_hash(self):
        a = download_media.url_to_filename("https://x.com/a.png", "reddit")
        b = download_media.url_to_filename("https://x.com/a.png", "reddit")
        self.assertEqual(a, b)

    def test_extension_extracted(self):
        fn = download_media.url_to_filename("https://x.com/pic.webp", "weibo")
        self.assertTrue(fn.startswith("weibo_"))
        self.assertTrue(fn.endswith(".webp"))

    def test_extension_with_query(self):
        fn = download_media.url_to_filename("https://x.com/v.mp4?token=1", "yt")
        self.assertTrue(fn.endswith(".mp4"))

    def test_default_extension_jpg(self):
        fn = download_media.url_to_filename("https://x.com/noext", "src")
        self.assertTrue(fn.endswith(".jpg"))

    def test_source_prefix(self):
        fn = download_media.url_to_filename("https://x.com/a.gif", "bilibili")
        self.assertTrue(fn.startswith("bilibili_"))


class TestManifestIO(unittest.TestCase):
    def test_load_missing_returns_default(self):
        with mock.patch.object(download_media, "MANIFEST_PATH", Path("/nope/manifest.json")):
            m = download_media.load_manifest()
        self.assertEqual(m, {"downloaded": {}, "failed": {}, "archived": []})

    def test_load_bad_json_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "manifest.json"
            p.write_text("{broken", encoding="utf-8")
            with mock.patch.object(download_media, "MANIFEST_PATH", p):
                m = download_media.load_manifest()
        self.assertEqual(m["downloaded"], {})

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "manifest.json"
            with mock.patch.object(download_media, "MANIFEST_PATH", p):
                download_media.save_manifest({"downloaded": {"u": 1}, "failed": {}, "archived": []})
                m = download_media.load_manifest()
        self.assertEqual(m["downloaded"], {"u": 1})


class TestCollectMediaUrls(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with mock.patch.object(download_media, "NEWS_JSON", Path("/nope/news.json")):
            self.assertEqual(download_media.collect_media_urls(), [])

    def test_bad_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "news.json"
            p.write_text("{bad", encoding="utf-8")
            with mock.patch.object(download_media, "NEWS_JSON", p):
                self.assertEqual(download_media.collect_media_urls(), [])

    def test_filters_items_without_media(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "news.json"
            p.write_text(json.dumps({"news": [
                {"media_url": "https://x/a.png", "source": "reddit", "title": "t", "time": "2026"},
                {"media_url": "", "source": "x"},
                {"source": "y"},
            ]}), encoding="utf-8")
            with mock.patch.object(download_media, "NEWS_JSON", p):
                out = download_media.collect_media_urls()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://x/a.png")
        self.assertEqual(out[0]["content_type"], "image")

    def test_title_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "news.json"
            p.write_text(json.dumps({"news": [
                {"media_url": "https://x/a.png", "title": "z" * 200},
            ]}), encoding="utf-8")
            with mock.patch.object(download_media, "NEWS_JSON", p):
                out = download_media.collect_media_urls()
        self.assertEqual(len(out[0]["title"]), 100)


def _resp(status=200, content_length=10, chunks=(b"data",)):
    r = mock.MagicMock()
    r.status_code = status
    r.headers = {"Content-Length": str(content_length)}
    r.raise_for_status.return_value = None
    r.iter_content.return_value = list(chunks)
    return r


class TestDownloadFile(unittest.TestCase):
    def test_success_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.png"
            with mock.patch.object(download_media.news_common, "safe_get",
                                   return_value=_resp(chunks=(b"abc",))):
                ok = download_media.download_file("https://x/a.png", dest)
            self.assertTrue(ok)
            self.assertEqual(dest.read_bytes(), b"abc")

    def test_oversize_by_content_length_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.png"
            big = download_media.MAX_FILE_SIZE_MB * 1024 * 1024 + 1
            with mock.patch.object(download_media.news_common, "safe_get",
                                   return_value=_resp(content_length=big)):
                ok = download_media.download_file("https://x/a.png", dest)
        self.assertFalse(ok)
        self.assertFalse(dest.exists())

    def test_oversize_during_stream_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.png"
            huge = b"x" * (download_media.MAX_FILE_SIZE_MB * 1024 * 1024 + 10)
            with mock.patch.object(download_media.news_common, "safe_get",
                                   return_value=_resp(content_length=0, chunks=(huge,))):
                ok = download_media.download_file("https://x/a.png", dest)
        self.assertFalse(ok)
        self.assertFalse(dest.exists())

    def test_valueerror_from_safe_get_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.png"
            with mock.patch.object(download_media.news_common, "safe_get",
                                   side_effect=ValueError("unsafe url")):
                ok = download_media.download_file("https://x/a.png", dest)
        self.assertFalse(ok)

    def test_request_exception_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.png"
            with mock.patch.object(download_media.news_common, "safe_get",
                                   side_effect=download_media.requests.RequestException("boom")):
                ok = download_media.download_file("https://x/a.png", dest)
        self.assertFalse(ok)


class TestDownloadNewMedia(unittest.TestCase):
    def test_skips_already_known_urls(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(download_media, "MEDIA_DIR", Path(d)):
                manifest = {"downloaded": {"https://x/a.png": {}}, "failed": {"https://x/b.png": {}}}
                items = [
                    {"url": "https://x/a.png", "source": "s", "title": "t", "time": ""},
                    {"url": "https://x/b.png", "source": "s", "title": "t", "time": ""},
                ]
                n = download_media.download_new_media(items, manifest)
        self.assertEqual(n, 0)

    def test_existing_file_recorded_without_download(self):
        with tempfile.TemporaryDirectory() as d:
            mdir = Path(d)
            url = "https://x/c.png"
            fn = download_media.url_to_filename(url, "s")
            (mdir / fn).write_bytes(b"already")
            with mock.patch.object(download_media, "MEDIA_DIR", mdir):
                manifest = {"downloaded": {}, "failed": {}}
                items = [{"url": url, "source": "s", "title": "t", "time": ""}]
                with mock.patch.object(download_media.news_common, "safe_get") as sg:
                    n = download_media.download_new_media(items, manifest)
                    sg.assert_not_called()
        self.assertEqual(n, 1)
        self.assertIn(url, manifest["downloaded"])

    def test_successful_download_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(download_media, "MEDIA_DIR", Path(d)), \
                    mock.patch.object(download_media.time, "sleep"), \
                    mock.patch.object(download_media, "download_file", return_value=True):
                manifest = {"downloaded": {}, "failed": {}}
                items = [{"url": "https://x/d.png", "source": "s", "title": "t", "time": ""}]
                n = download_media.download_new_media(items, manifest)
        self.assertEqual(n, 1)
        self.assertIn("https://x/d.png", manifest["downloaded"])

    def test_failed_download_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(download_media, "MEDIA_DIR", Path(d)), \
                    mock.patch.object(download_media.time, "sleep"), \
                    mock.patch.object(download_media, "download_file", return_value=False):
                manifest = {"downloaded": {}, "failed": {}}
                items = [{"url": "https://x/e.png", "source": "s", "title": "t", "time": ""}]
                n = download_media.download_new_media(items, manifest)
        self.assertEqual(n, 0)
        self.assertIn("https://x/e.png", manifest["failed"])

    def test_respects_max_downloads(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(download_media, "MEDIA_DIR", Path(d)), \
                    mock.patch.object(download_media.time, "sleep"), \
                    mock.patch.object(download_media, "download_file", return_value=True):
                manifest = {"downloaded": {}, "failed": {}}
                items = [{"url": f"https://x/{i}.png", "source": "s", "title": "t", "time": ""}
                         for i in range(5)]
                n = download_media.download_new_media(items, manifest, max_downloads=2)
        self.assertEqual(n, 2)


class TestGetMediaDirSize(unittest.TestCase):
    def test_missing_dir_zero(self):
        with mock.patch.object(download_media, "MEDIA_DIR", Path("/nope/media")):
            self.assertEqual(download_media.get_media_dir_size_mb(), 0)

    def test_sums_files_excluding_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            mdir = Path(d)
            (mdir / "a.png").write_bytes(b"x" * (1024 * 1024))
            (mdir / "manifest.json").write_bytes(b"x" * (1024 * 1024))
            with mock.patch.object(download_media, "MEDIA_DIR", mdir):
                size = download_media.get_media_dir_size_mb()
        self.assertAlmostEqual(size, 1.0, places=2)


class TestArchiveToRelease(unittest.TestCase):
    def test_below_threshold_no_archive(self):
        with mock.patch.object(download_media, "get_media_dir_size_mb", return_value=1):
            with mock.patch.object(download_media.subprocess, "run") as run:
                download_media.archive_to_release({})
                run.assert_not_called()

    def test_no_media_files_returns_early(self):
        with tempfile.TemporaryDirectory() as d:
            mdir = Path(d)
            (mdir / "manifest.json").write_bytes(b"{}")
            with mock.patch.object(download_media, "MEDIA_DIR", mdir), \
                    mock.patch.object(download_media, "get_media_dir_size_mb",
                                      return_value=download_media.ARCHIVE_THRESHOLD_MB + 1), \
                    mock.patch.object(download_media.subprocess, "run") as run:
                download_media.archive_to_release({})
                run.assert_not_called()

    def test_success_uploads_and_cleans(self):
        with tempfile.TemporaryDirectory() as d:
            mdir = Path(d) / "media"
            mdir.mkdir()
            (mdir / "a.png").write_bytes(b"data")
            manifest = {}
            with mock.patch.object(download_media, "MEDIA_DIR", mdir), \
                    mock.patch.object(download_media, "get_media_dir_size_mb",
                                      return_value=download_media.ARCHIVE_THRESHOLD_MB + 1), \
                    mock.patch.object(download_media.subprocess, "run") as run:
                run.return_value = mock.MagicMock()
                download_media.archive_to_release(manifest)
            run.assert_called_once()
            self.assertFalse((mdir / "a.png").exists())  # cleaned after upload
            self.assertEqual(len(manifest["archived"]), 1)

    def test_gh_failure_keeps_archive(self):
        with tempfile.TemporaryDirectory() as d:
            mdir = Path(d) / "media"
            mdir.mkdir()
            (mdir / "a.png").write_bytes(b"data")
            manifest = {}
            with mock.patch.object(download_media, "MEDIA_DIR", mdir), \
                    mock.patch.object(download_media, "get_media_dir_size_mb",
                                      return_value=download_media.ARCHIVE_THRESHOLD_MB + 1), \
                    mock.patch.object(download_media.subprocess, "run",
                                      side_effect=FileNotFoundError("no gh")):
                download_media.archive_to_release(manifest)
            self.assertNotIn("archived", manifest)
            self.assertTrue((mdir / "a.png").exists())  # not cleaned on failure


if __name__ == "__main__":
    unittest.main()
