import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_engine as ae


def _touch(p: Path, content: str = "{}\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class RegistryInvariants(unittest.TestCase):
    """锁住 Discord 滚动单 release 设计 + 不漂移的 cutoff/删数据策略。"""

    def test_discord_entry_rolling_release(self):
        cfg = ae.load_registry()["discord"]
        # 滚动单 release：所有月份归入固定标签 community-data
        self.assertEqual(cfg["release_tag"], "community-data")
        self.assertEqual(cfg["release_title"], "Community Archive Data")
        # 资产文件名仍按月，向后兼容旧 discord-archive-YYYY-MM.tar.gz（迁移来的资产同名）
        self.assertEqual(cfg["asset_template"], "discord-archive-{group}.tar.gz")
        self.assertEqual(
            ae.asset_name_of(cfg, "2026-01"), "discord-archive-2026-01.tar.gz"
        )
        # 不漂移的策略
        self.assertEqual(cfg["cutoff_days"], 60)
        self.assertEqual(cfg["group_by"], "month_from_stem")
        # 2026-06-21 de-tier：discord 全量永驻 git，归档不再驱逐（keep）
        self.assertEqual(cfg["after_archive"], "keep")
        self.assertEqual(cfg["base_dir"], "Public-Info-Pool/Record/Community/discord")

    def test_fanart_entry_rolling_release(self):
        cfg = ae.load_registry()["fanart"]
        # 守密人 2026-06-21 裁定：60 天 cutoff + git_rm 删原图
        self.assertEqual(cfg["base_dir"], "projects/news/data/fanart")
        self.assertEqual(cfg["glob"], "20*/*")
        self.assertEqual(cfg["group_by"], "month_from_parent_dir")
        self.assertEqual(cfg["cutoff_days"], 60)
        self.assertEqual(cfg["after_archive"], "git_rm")
        # 图片资产归「社区归档资产」滚动 release（与 discord 文本数据的 community-data 分开）
        self.assertEqual(cfg["release_tag"], "community-assets")
        self.assertEqual(cfg["asset_template"], "fanart-archive-{group}.tar.gz")
        self.assertEqual(ae.asset_name_of(cfg, "2026-05"), "fanart-archive-2026-05.tar.gz")


class GroupingAndCutoff(unittest.TestCase):
    def test_group_of_month_from_stem(self):
        p = Path("channels/abc/2026-01-15.jsonl")
        self.assertEqual(ae.group_of(p, "month_from_stem", "all"), "2026-01")

    def test_group_of_single(self):
        p = Path("media/x.png")
        self.assertEqual(ae.group_of(p, "single", "v1"), "v1")

    def test_is_eligible_cutoff_compare(self):
        # 与原 archive_discord 的字符串日期比较逐字节等价
        self.assertTrue(ae.is_eligible(Path("a/2026-01-15.jsonl"), "month_from_stem", "2026-04-22"))
        self.assertFalse(ae.is_eligible(Path("a/2026-05-01.jsonl"), "month_from_stem", "2026-04-22"))
        # 边界：等于 cutoff 当日不归档（< 严格小于）
        self.assertFalse(ae.is_eligible(Path("a/2026-04-22.jsonl"), "month_from_stem", "2026-04-22"))

    def test_is_eligible_no_cutoff(self):
        self.assertTrue(ae.is_eligible(Path("a/whatever.jsonl"), "month_from_stem", None))

    def test_group_of_month_from_parent_dir(self):
        # 日期在父目录名（如 fanart：2026-05-29/pixiv_x.jpg），文件名 stem 无日期
        p = Path("fanart/2026-05-29/pixiv_efad328a8f.jpg")
        self.assertEqual(ae.group_of(p, "month_from_parent_dir", "all"), "2026-05")

    def test_is_eligible_parent_dir_cutoff(self):
        old = Path("fanart/2026-03-01/x.jpg")
        new = Path("fanart/2026-06-10/x.jpg")
        self.assertTrue(ae.is_eligible(old, "month_from_parent_dir", "2026-04-22"))
        self.assertFalse(ae.is_eligible(new, "month_from_parent_dir", "2026-04-22"))


class Discover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        for ch in ("aaa", "bbb"):
            _touch(self.base / "channels" / ch / "2026-01-05.jsonl")
            _touch(self.base / "channels" / ch / "2026-01-20.jsonl")
            _touch(self.base / "channels" / ch / "2026-04-30.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **over):
        cfg = {"glob": "channels/*/*.jsonl", "group_by": "month_from_stem", "cutoff_days": None}
        cfg.update(over)
        return cfg

    def test_no_cutoff_groups_all_by_month(self):
        groups = ae.discover(self._cfg(), self.base, [])
        self.assertEqual(set(groups), {"2026-01", "2026-04"})
        self.assertEqual(len(groups["2026-01"]), 4)  # 2 channels x 2 files
        self.assertEqual(len(groups["2026-04"]), 2)

    def test_force_group_bypasses_cutoff(self):
        groups = ae.discover(self._cfg(cutoff_days=60), self.base, ["2026-04"])
        self.assertEqual(set(groups), {"2026-04"})
        self.assertEqual(len(groups["2026-04"]), 2)

    def test_missing_base_dir_returns_empty(self):
        self.assertEqual(ae.discover(self._cfg(), self.base / "nope", []), {})


class ParentDirLayoutAndFileGuard(unittest.TestCase):
    """fanart 式布局：日期在目录名 + thumbs/ 子目录须被 is_file 护栏滤掉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        for day in ("2026-05-12", "2026-06-01"):
            _touch(self.base / day / "pixiv_a.jpg")
            _touch(self.base / day / "discord_b.png")
            _touch(self.base / day / "thumbs" / "pixiv_a.jpg")  # 缩略图不该被归档

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **over):
        cfg = {"glob": "20*/*", "group_by": "month_from_parent_dir", "cutoff_days": None}
        cfg.update(over)
        return cfg

    def test_groups_by_parent_dir_and_excludes_thumbs(self):
        groups = ae.discover(self._cfg(), self.base, [])
        self.assertEqual(set(groups), {"2026-05", "2026-06"})
        # 每月仅 2 个顶层图；thumbs/ 子目录文件深一层不被 glob 命中
        self.assertEqual(len(groups["2026-05"]), 2)
        self.assertEqual({p.name for p in groups["2026-05"]}, {"pixiv_a.jpg", "discord_b.png"})

    def test_glob_yielding_dir_is_skipped(self):
        # glob '20*/*' 会命中 thumbs 目录本身，is_file 护栏须跳过它
        self.assertTrue(any(p.is_dir() for p in self.base.glob("20*/*")))
        groups = ae.discover(self._cfg(), self.base, [])
        self.assertTrue(all(p.is_file() for ps in groups.values() for p in ps))

    def test_parent_dir_cutoff_filters_recent(self):
        # cutoff 仅留早于阈值的目录（2026-06-01 比 2026-05-12 新，被滤）
        groups = ae.discover(self._cfg(cutoff_days=None), self.base, [])
        self.assertEqual(set(groups), {"2026-05", "2026-06"})
        # 用 is_eligible 直接验证父目录比较
        f_old = self.base / "2026-05-12" / "pixiv_a.jpg"
        f_new = self.base / "2026-06-01" / "pixiv_a.jpg"
        self.assertTrue(ae.is_eligible(f_old, "month_from_parent_dir", "2026-05-30"))
        self.assertFalse(ae.is_eligible(f_new, "month_from_parent_dir", "2026-05-30"))


class Tarball(unittest.TestCase):
    def test_arcname_relative_to_base_and_filename(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            f = base / "channels" / "xx" / "2026-01-01.jsonl"
            _touch(f)
            cfg = {"tag_template": "discord-archive-{group}"}
            path, size = ae.create_tarball(cfg, base, "2026-01", [f])
            # 文件名向后兼容
            self.assertEqual(path.name, "discord-archive-2026-01.tar.gz")
            self.assertGreater(size, 0)
            # tar 内路径相对 base_dir（与原 archive_discord 一致）
            with tarfile.open(path, "r:gz") as tar:
                self.assertEqual(tar.getnames(), ["channels/xx/2026-01-01.jsonl"])


class RollingUpload(unittest.TestCase):
    """滚动单 release：不得删整个 release，只 --clobber 替换本桶资产。"""

    def _run_upload(self, view_returncode):
        cfg = {
            "release_tag": "community-data",
            "release_title": "Community Archive Data",
            "asset_template": "discord-archive-{group}.tar.gz",
        }
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:3] == ["gh", "release", "view"]:
                return mock.Mock(returncode=view_returncode)
            return mock.Mock(returncode=0, stderr="", stdout="")

        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "discord-archive-2026-05.tar.gz"
            archive.write_text("x")
            with mock.patch.dict(ae.os.environ, {"GITHUB_REPOSITORY": "o/r"}), \
                 mock.patch.object(ae.subprocess, "run", side_effect=fake_run):
                ok = ae.upload_to_release(cfg, archive, "2026-05", 3)
        return ok, calls

    def test_existing_release_clobbers_asset_no_delete(self):
        ok, calls = self._run_upload(view_returncode=0)  # release 已存在
        self.assertTrue(ok)
        verbs = [c[:3] for c in calls]
        self.assertIn(["gh", "release", "view"], verbs)
        self.assertIn(["gh", "release", "upload"], verbs)
        # 关键：绝不删整个 release（否则会连带删掉其它月份资产）
        self.assertNotIn(["gh", "release", "delete"], verbs)
        upload_cmd = next(c for c in calls if c[:3] == ["gh", "release", "upload"])
        self.assertIn("--clobber", upload_cmd)

    def test_missing_release_is_created_then_uploaded(self):
        ok, calls = self._run_upload(view_returncode=1)  # release 不存在
        self.assertTrue(ok)
        verbs = [c[:3] for c in calls]
        self.assertIn(["gh", "release", "create"], verbs)
        self.assertIn(["gh", "release", "upload"], verbs)
        self.assertNotIn(["gh", "release", "delete"], verbs)


class GitRm(unittest.TestCase):
    def test_git_rm_called_per_file(self):
        files = [Path("/repo/a.jsonl"), Path("/repo/b.jsonl")]
        with mock.patch.object(ae.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            removed = ae.git_rm_files(files)
        self.assertEqual(removed, 2)
        self.assertEqual(run.call_count, 2)
        # 确实调用的是 git rm -f
        first = run.call_args_list[0].args[0]
        self.assertEqual(first[:3], ["git", "rm", "-f"])

    def test_git_rm_untracked_falls_back_to_unlink(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.jsonl"
            f.write_text("x")
            err = ae.subprocess.CalledProcessError(1, "git")
            with mock.patch.object(ae.subprocess, "run", side_effect=err):
                removed = ae.git_rm_files([f])
            self.assertEqual(removed, 1)
            self.assertFalse(f.exists())  # 已 unlink


class DryRun(unittest.TestCase):
    def test_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            base = repo / "projects" / "news" / "data" / "discord"
            _touch(base / "channels" / "xx" / "2026-01-01.jsonl")
            cfg = ae.load_registry()["discord"]
            args = mock.Mock(dry_run=True, skip_upload=False, force_group=["2026-01"])
            with mock.patch.object(ae, "REPO_ROOT", repo):
                ae.archive_source("discord", cfg, args)
            # dry-run 不得产出任何 tarball
            self.assertEqual(list(base.glob("*.tar.gz")), [])


if __name__ == "__main__":
    unittest.main()
