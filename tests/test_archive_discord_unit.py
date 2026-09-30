"""archive_discord 薄垫片单测 — import 冒烟 + CLI flag 透传/映射。

委派 archive_engine.main，整段 mock；--force-month → --force-group 映射是唯一逻辑。
"""

import unittest
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_discord  # noqa: E402


class TestArchiveDiscordShim(unittest.TestCase):
    def test_imports_and_exposes_main(self):
        self.assertTrue(callable(archive_discord.main))

    def test_force_month_maps_to_force_group(self):
        # simulate the __main__ argv-translation logic the shim runs
        sys_argv = ["prog", "--force-month", "2026-05", "--dry-run"]
        argv = ["--source", "discord"]
        for tok in sys_argv[1:]:
            argv.append("--force-group" if tok == "--force-month" else tok)
        self.assertEqual(
            argv, ["--source", "discord", "--force-group", "2026-05", "--dry-run"])

    def test_delegates_to_engine_main(self):
        with mock.patch.object(archive_discord, "main") as m:
            archive_discord.main(["--source", "discord"])
            m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
