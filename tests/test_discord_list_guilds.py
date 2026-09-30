import unittest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

from discord_archiver import GLOBAL_GUILD_ID
from discord_list_guilds import KNOWN_GUILDS, VOLUNTEER_GUILD_ID, classify_guilds


class TestClassifyGuilds(unittest.TestCase):
    """guild 分类纯函数：把 bot 加入的服务器对照已登记清单打标，
    高亮未登记者（新接入的日服走这条路被发现）。"""

    def test_flags_unregistered_guild(self):
        guilds = [
            {"id": GLOBAL_GUILD_ID, "name": "Official"},
            {"id": VOLUNTEER_GUILD_ID, "name": "Volunteer"},
            {"id": "9999999999", "name": "Morimens 日本"},
        ]
        rows, unregistered = classify_guilds(guilds, KNOWN_GUILDS)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(unregistered), 1)
        self.assertEqual(unregistered[0]["id"], "9999999999")
        self.assertEqual(unregistered[0]["name"], "Morimens 日本")

    def test_registered_guilds_have_role(self):
        guilds = [{"id": GLOBAL_GUILD_ID, "name": "Official"}]
        rows, unregistered = classify_guilds(guilds, KNOWN_GUILDS)
        self.assertTrue(rows[0]["registered"])
        self.assertIsNotNone(rows[0]["role"])
        self.assertEqual(unregistered, [])

    def test_unregistered_sorted_first(self):
        # 未登记排在前，便于在 workflow 日志里一眼定位候选。
        guilds = [
            {"id": GLOBAL_GUILD_ID, "name": "ZZZ Official"},
            {"id": "9999999999", "name": "AAA New"},
        ]
        rows, _ = classify_guilds(guilds, KNOWN_GUILDS)
        self.assertFalse(rows[0]["registered"])
        self.assertEqual(rows[0]["id"], "9999999999")

    def test_int_ids_are_stringified(self):
        # Discord API 可能回数值型 id；分类后应统一为字符串以便和已登记清单比对。
        guilds = [{"id": int(GLOBAL_GUILD_ID), "name": "Official"}]
        rows, unregistered = classify_guilds(guilds, KNOWN_GUILDS)
        self.assertEqual(rows[0]["id"], GLOBAL_GUILD_ID)
        self.assertTrue(rows[0]["registered"])
        self.assertEqual(unregistered, [])

    def test_empty(self):
        rows, unregistered = classify_guilds([], KNOWN_GUILDS)
        self.assertEqual(rows, [])
        self.assertEqual(unregistered, [])


if __name__ == "__main__":
    unittest.main()
