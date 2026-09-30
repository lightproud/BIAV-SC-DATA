"""discord_list_guilds._get / main() coverage — the existing test only hits the
pure classify_guilds. Here we mock requests + the SEEN_PATH write to exercise
every branch of main() (token missing, 401, non-200, bad shape, success with
0/1/N unregistered) and the 429 retry in _get. Hermetic: no network, tmp path.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import discord_list_guilds as dlg
from discord_archiver import GLOBAL_GUILD_ID


class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self.text = text

    def json(self):
        return self._json


def _patch_seen(tmp):
    # SEEN_PATH now derives from archive_layout.discord_root() (分仓桥接); main()
    # prints it directly (no relative_to), so patching SEEN_PATH alone redirects
    # the write into the temp sandbox.
    root = Path(tmp)
    return mock.patch.object(dlg, "SEEN_PATH", root / "data" / "guilds_seen.json")


class TestGet(unittest.TestCase):
    def test_get_success(self):
        fake_requests = mock.Mock()
        fake_requests.get.return_value = _Resp(200, [])
        with mock.patch.dict(sys.modules, {"requests": fake_requests}):
            resp = dlg._get("/users/@me/guilds", {"Authorization": "Bot x"})
        self.assertEqual(resp.status_code, 200)

    def test_get_429_then_success(self):
        fake_requests = mock.Mock()
        rl = _Resp(429, {"retry_after": 0.01})
        ok = _Resp(200, [])
        fake_requests.get.side_effect = [rl, ok]
        with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
                mock.patch.object(dlg.time, "sleep"):
            resp = dlg._get("/x", {})
        self.assertEqual(resp.status_code, 200)


class TestMain(unittest.TestCase):
    def test_no_token(self):
        with mock.patch.dict(dlg.os.environ, {}, clear=True):
            self.assertEqual(dlg.main(), 1)

    def test_no_response(self):
        with mock.patch.dict(dlg.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                mock.patch.object(dlg, "_get", return_value=None):
            self.assertEqual(dlg.main(), 1)

    def test_401(self):
        with mock.patch.dict(dlg.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                mock.patch.object(dlg, "_get", return_value=_Resp(401)):
            self.assertEqual(dlg.main(), 1)

    def test_non_200(self):
        with mock.patch.dict(dlg.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                mock.patch.object(dlg, "_get", return_value=_Resp(503, text="oops")):
            self.assertEqual(dlg.main(), 1)

    def test_bad_shape(self):
        with mock.patch.dict(dlg.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                mock.patch.object(dlg, "_get", return_value=_Resp(200, {"not": "list"})):
            self.assertEqual(dlg.main(), 1)

    def test_success_no_unregistered(self):
        guilds = [{"id": GLOBAL_GUILD_ID, "name": "Official"},
                  {"id": dlg.VOLUNTEER_GUILD_ID, "name": "Volunteer"}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(dlg.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                mock.patch.object(dlg, "_get", return_value=_Resp(200, guilds)):
            with _patch_seen(tmp):
                self.assertEqual(dlg.main(), 0)
                self.assertTrue((Path(tmp) / "data" / "guilds_seen.json").exists())

    def test_success_one_unregistered(self):
        guilds = [{"id": GLOBAL_GUILD_ID, "name": "Official"},
                  {"id": "9999", "name": "JP"}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(dlg.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                mock.patch.object(dlg, "_get", return_value=_Resp(200, guilds)):
            with _patch_seen(tmp):
                self.assertEqual(dlg.main(), 0)

    def test_success_many_unregistered(self):
        guilds = [{"id": "8888", "name": "A"}, {"id": "9999", "name": "B"}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(dlg.os.environ, {"DISCORD_BOT_TOKEN": "t"}, clear=True), \
                mock.patch.object(dlg, "_get", return_value=_Resp(200, guilds)):
            with _patch_seen(tmp):
                self.assertEqual(dlg.main(), 0)


if __name__ == "__main__":
    unittest.main()
