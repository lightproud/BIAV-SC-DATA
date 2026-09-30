"""download_media 补充覆盖：main() 编排路径。

补 tests/test_download_media.py 未触及的 main()（参数解析 + 各阶段编排）。
所有 I/O / 网络 / subprocess 全打桩，绝不触网、绝不污染真实 data/media。
"""

import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import download_media  # noqa: E402


class TestMain(unittest.TestCase):
    def test_no_items_returns_early(self):
        # collect_media_urls 返回空 → 提前 return，不调用下载 (285-287)
        argv = ["download_media.py"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(download_media, "collect_media_urls", return_value=[]), \
                mock.patch.object(download_media, "load_manifest") as lm, \
                mock.patch.object(download_media, "download_new_media") as dl:
            download_media.main()
            dl.assert_not_called()
            lm.assert_not_called()

    def test_full_pipeline_without_archive(self):
        # 有条目 → 加载 manifest、下载、保存、统计来源 (288-313)
        items = [{"url": "https://x/a.png", "source": "reddit", "title": "t", "time": ""}]
        manifest = {"downloaded": {"https://x/a.png": {"source": "reddit"}}, "failed": {}}
        argv = ["download_media.py", "--max-downloads", "5"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(download_media, "collect_media_urls", return_value=items), \
                mock.patch.object(download_media, "load_manifest", return_value=manifest), \
                mock.patch.object(download_media, "download_new_media", return_value=1) as dl, \
                mock.patch.object(download_media, "save_manifest") as sm, \
                mock.patch.object(download_media, "get_media_dir_size_mb", return_value=3.0), \
                mock.patch.object(download_media, "archive_to_release") as arch:
            download_media.main()
            dl.assert_called_once()
            sm.assert_called()
            arch.assert_not_called()  # 未传 --archive

    def test_pipeline_with_archive_flag(self):
        # --archive → 调用 archive_to_release + 二次 save_manifest (299-301)
        items = [{"url": "https://x/a.png", "source": "weibo", "title": "t", "time": ""}]
        manifest = {"downloaded": {}, "failed": {}}
        argv = ["download_media.py", "--archive"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(download_media, "collect_media_urls", return_value=items), \
                mock.patch.object(download_media, "load_manifest", return_value=manifest), \
                mock.patch.object(download_media, "download_new_media", return_value=0), \
                mock.patch.object(download_media, "save_manifest") as sm, \
                mock.patch.object(download_media, "get_media_dir_size_mb", return_value=10.0), \
                mock.patch.object(download_media, "archive_to_release") as arch:
            download_media.main()
            arch.assert_called_once()
            # 下载后 + 归档后各保存一次
            self.assertGreaterEqual(sm.call_count, 2)


if __name__ == "__main__":
    unittest.main()
