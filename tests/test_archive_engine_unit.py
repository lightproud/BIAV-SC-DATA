"""archive_engine coverage for the upload/log/index/archive_source/main paths
the existing test leaves uncovered: no-repo guard, release-create failure, the
legacy per-tag upload mode, clean_empty_dirs, load/save_log,
rebuild_releases_index, the full archive_source flow (skip-upload + git_rm), and
main() source resolution. Hermetic: gh/git subprocess mocked, REPO_ROOT/paths
patched into tmp dirs.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_engine as ae


def _touch(p: Path, content: str = "{}\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── upload_to_release branches ───────────────────────────────────────────────

class TestUpload(unittest.TestCase):
    def test_no_repo_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "a.tar.gz"
            archive.write_text("x")
            with mock.patch.dict(ae.os.environ, {}, clear=True):
                ae.os.environ.pop("GITHUB_REPOSITORY", None)
                ok = ae.upload_to_release({"release_tag": "t"}, archive, "g", 1)
            self.assertFalse(ok)

    def test_rolling_create_failure(self):
        cfg = {"release_tag": "community-data"}

        def fake_run(cmd, *a, **k):
            if cmd[:3] == ["gh", "release", "view"]:
                return mock.Mock(returncode=1)
            if cmd[:3] == ["gh", "release", "create"]:
                return mock.Mock(returncode=1, stderr="create boom")
            return mock.Mock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "a.tar.gz"
            archive.write_text("x")
            with mock.patch.dict(ae.os.environ, {"GITHUB_REPOSITORY": "o/r"}), \
                    mock.patch.object(ae.subprocess, "run", side_effect=fake_run):
                ok = ae.upload_to_release(cfg, archive, "2026-01", 1)
        self.assertFalse(ok)

    def test_rolling_upload_failure(self):
        cfg = {"release_tag": "community-data"}

        def fake_run(cmd, *a, **k):
            if cmd[:3] == ["gh", "release", "view"]:
                return mock.Mock(returncode=0)
            if cmd[:3] == ["gh", "release", "upload"]:
                return mock.Mock(returncode=1, stderr="upload boom")
            return mock.Mock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "a.tar.gz"
            archive.write_text("x")
            with mock.patch.dict(ae.os.environ, {"GITHUB_REPOSITORY": "o/r"}), \
                    mock.patch.object(ae.subprocess, "run", side_effect=fake_run):
                ok = ae.upload_to_release(cfg, archive, "2026-01", 1)
        self.assertFalse(ok)

    def test_legacy_tag_mode_success(self):
        cfg = {
            "tag_template": "discord-archive-{group}",
            "title_template": "Archive {group}",
            "notes_template": "{group} {filename} {size_kb} {files}",
        }
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd[:3])
            return mock.Mock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "discord-archive-2026-01.tar.gz"
            archive.write_text("x")
            with mock.patch.dict(ae.os.environ, {"GITHUB_REPOSITORY": "o/r"}), \
                    mock.patch.object(ae.subprocess, "run", side_effect=fake_run):
                ok = ae.upload_to_release(cfg, archive, "2026-01", 3)
        self.assertTrue(ok)
        # legacy mode deletes then creates
        self.assertIn(["gh", "release", "delete"], calls)
        self.assertIn(["gh", "release", "create"], calls)

    def test_legacy_tag_mode_failure(self):
        cfg = {
            "tag_template": "t-{group}", "title_template": "{group}",
            "notes_template": "{group}",
        }

        def fake_run(cmd, *a, **k):
            if cmd[:3] == ["gh", "release", "create"]:
                return mock.Mock(returncode=1, stderr="boom")
            return mock.Mock(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "x.tar.gz"
            archive.write_text("x")
            with mock.patch.dict(ae.os.environ, {"GITHUB_REPOSITORY": "o/r"}), \
                    mock.patch.object(ae.subprocess, "run", side_effect=fake_run):
                ok = ae.upload_to_release(cfg, archive, "2026-01", 1)
        self.assertFalse(ok)


# ── clean_empty_dirs ─────────────────────────────────────────────────────────

class TestCleanEmptyDirs(unittest.TestCase):
    def test_removes_empty_parent(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            sub = base / "2026-05-01"
            f = sub / "x.jpg"
            _touch(f)
            f.unlink()  # leave the dir empty
            ae.clean_empty_dirs(base, [f])
            self.assertFalse(sub.exists())

    def test_keeps_nonempty_parent(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            sub = base / "2026-05-01"
            f = sub / "x.jpg"
            other = sub / "y.jpg"
            _touch(f)
            _touch(other)
            f.unlink()
            ae.clean_empty_dirs(base, [f])
            self.assertTrue(sub.exists())  # y.jpg remains

    def test_does_not_remove_base(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            f = base / "x.jpg"
            ae.clean_empty_dirs(base, [f])
            self.assertTrue(base.exists())


# ── load_log / save_log ──────────────────────────────────────────────────────

class TestLog(unittest.TestCase):
    def test_load_missing(self):
        self.assertEqual(ae.load_log(Path("/nonexistent/log.json")), [])

    def test_load_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.json"
            p.write_text("{bad")
            self.assertEqual(ae.load_log(p), [])

    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "log.json"
            ae.save_log(p, [{"group": "2026-01"}])
            self.assertEqual(ae.load_log(p), [{"group": "2026-01"}])


# ── rebuild_releases_index ───────────────────────────────────────────────────

class TestRebuildIndex(unittest.TestCase):
    def test_builds_index_from_logs(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            base = repo / "data" / "discord"
            _touch(base / "archive-log.json", json.dumps([
                {"source": "discord", "group": "2026-01", "tag": "community-data",
                 "uploaded_to_releases": True, "files": 3, "archive_size_bytes": 100,
                 "archived_at": "2026-01-01"},
            ]))
            registry = {"discord": {"base_dir": "data/discord", "release_tag": "community-data"}}
            out_index = repo / "releases-index.json"
            with mock.patch.object(ae, "REPO_ROOT", repo), \
                    mock.patch.object(ae, "RELEASES_INDEX", out_index):
                ae.rebuild_releases_index(registry)
            data = json.loads(out_index.read_text())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["group"], "2026-01")

    def test_index_legacy_tag_template(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            base = repo / "data" / "legacy"
            _touch(base / "archive-log.json", json.dumps([
                {"month": "2025-12", "uploaded_to_releases": True},
            ]))
            registry = {"legacy": {"base_dir": "data/legacy", "tag_template": "arc-{group}"}}
            out_index = repo / "releases-index.json"
            with mock.patch.object(ae, "REPO_ROOT", repo), \
                    mock.patch.object(ae, "RELEASES_INDEX", out_index):
                ae.rebuild_releases_index(registry)
            data = json.loads(out_index.read_text())
            self.assertEqual(data[0]["tag"], "arc-2025-12")


# ── archive_source full flow ─────────────────────────────────────────────────

class TestArchiveSource(unittest.TestCase):
    def _setup_repo(self, d):
        repo = Path(d)
        base = repo / "data" / "src"
        for ch in ("aaa",):
            _touch(base / "channels" / ch / "2026-01-05.jsonl")
            _touch(base / "channels" / ch / "2026-01-20.jsonl")
        return repo, base

    def _cfg(self, **over):
        cfg = {
            "base_dir": "data/src", "glob": "channels/*/*.jsonl",
            "group_by": "month_from_stem", "cutoff_days": None,
            "release_tag": "community-data",
            "asset_template": "src-{group}.tar.gz",
            "after_archive": "git_rm",
        }
        cfg.update(over)
        return cfg

    def test_skip_upload_with_git_rm(self):
        with tempfile.TemporaryDirectory() as d:
            repo, base = self._setup_repo(d)
            args = mock.Mock(dry_run=False, skip_upload=True, force_group=["2026-01"])
            with mock.patch.object(ae, "REPO_ROOT", repo), \
                    mock.patch.object(ae, "git_rm_files", return_value=2) as grm:
                ae.archive_source("src", self._cfg(), args)
            grm.assert_called_once()
            log = json.loads((base / "archive-log.json").read_text())
            self.assertEqual(log[0]["group"], "2026-01")
            self.assertFalse(log[0]["uploaded_to_releases"])

    def test_upload_success_then_log(self):
        with tempfile.TemporaryDirectory() as d:
            repo, base = self._setup_repo(d)
            args = mock.Mock(dry_run=False, skip_upload=False, force_group=["2026-01"])
            with mock.patch.object(ae, "REPO_ROOT", repo), \
                    mock.patch.object(ae, "upload_to_release", return_value=True), \
                    mock.patch.object(ae, "git_rm_files", return_value=2):
                ae.archive_source("src", self._cfg(), args)
            log = json.loads((base / "archive-log.json").read_text())
            self.assertTrue(log[0]["uploaded_to_releases"])

    def test_upload_failure_keeps_files(self):
        with tempfile.TemporaryDirectory() as d:
            repo, base = self._setup_repo(d)
            args = mock.Mock(dry_run=False, skip_upload=False, force_group=["2026-01"])
            with mock.patch.object(ae, "REPO_ROOT", repo), \
                    mock.patch.object(ae, "upload_to_release", return_value=False), \
                    mock.patch.object(ae, "git_rm_files") as grm:
                ae.archive_source("src", self._cfg(), args)
            grm.assert_not_called()  # upload failed → no git_rm
            # original files still present
            self.assertTrue((base / "channels" / "aaa" / "2026-01-05.jsonl").exists())

    def test_nothing_to_archive(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            args = mock.Mock(dry_run=False, skip_upload=True, force_group=[])
            cfg = self._cfg(cutoff_days=60)
            with mock.patch.object(ae, "REPO_ROOT", repo):
                # base dir missing → discover returns {} → early return
                ae.archive_source("src", cfg, args)

    def test_clean_empty_dirs_invoked(self):
        with tempfile.TemporaryDirectory() as d:
            repo, base = self._setup_repo(d)
            args = mock.Mock(dry_run=False, skip_upload=True, force_group=["2026-01"])
            cfg = self._cfg(clean_empty_dirs=True, after_archive="git_rm")

            def fake_git_rm(files):
                for f in files:
                    f.unlink(missing_ok=True)
                return len(files)

            with mock.patch.object(ae, "REPO_ROOT", repo), \
                    mock.patch.object(ae, "git_rm_files", side_effect=fake_git_rm):
                ae.archive_source("src", cfg, args)
            # channel dir emptied & cleaned
            self.assertFalse((base / "channels" / "aaa").exists())


# ── main() ───────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def test_unknown_source(self):
        with mock.patch.object(ae, "load_registry", return_value={"discord": {}}):
            # returns None without raising
            self.assertIsNone(ae.main(["--source", "nope"]))

    def test_single_source_dry_run_skips_index(self):
        with mock.patch.object(ae, "load_registry", return_value={"discord": {"base_dir": "x"}}), \
                mock.patch.object(ae, "archive_source") as asrc, \
                mock.patch.object(ae, "rebuild_releases_index") as rri:
            ae.main(["--source", "discord", "--dry-run"])
        asrc.assert_called_once()
        rri.assert_not_called()

    def test_all_sources_rebuilds_index(self):
        registry = {"a": {"base_dir": "a"}, "b": {"base_dir": "b"}}
        with mock.patch.object(ae, "load_registry", return_value=registry), \
                mock.patch.object(ae, "archive_source") as asrc, \
                mock.patch.object(ae, "rebuild_releases_index") as rri:
            ae.main(["--source", "all"])
        self.assertEqual(asrc.call_count, 2)
        rri.assert_called_once()


if __name__ == "__main__":
    unittest.main()
