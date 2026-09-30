"""sources.py 采集源单一真相源的契约不变量测试（P0）。

sources.py 是 7 个生产脚本共享的源清单 SSOT，其存在理由就是「杜绝清单漂移」
（文件头自述：历史上各脚本各存一份清单，漂移导致「采了不归档 / 归档了不审计」盲区）。
模块文档化了一批集合关系，但此前无任何测试钉住它们——有人改一个清单破坏不变量，
全绿照过。本测试把那些口头契约升级为可执行断言。

覆盖两层：
  1) 模块内部不变量（KNOWN 去重 / ARCHIVE = KNOWN - discord / 子集关系 / 别名闭包 …）
  2) 跨模块同步（sources.BACKFILL_PLATFORMS == backfill_platforms.BACKFILL_REGISTRY.keys()）
"""
import unittest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）
import sources  # noqa: E402


class TestKnownSources(unittest.TestCase):
    def test_no_duplicates(self):
        self.assertEqual(len(sources.KNOWN_SOURCES), len(set(sources.KNOWN_SOURCES)),
                         'KNOWN_SOURCES 含重复项')

    def test_all_lowercase_nonempty(self):
        for s in sources.KNOWN_SOURCES:
            self.assertTrue(s and s == s.lower(), f'源名应非空小写: {s!r}')


class TestArchivePlatforms(unittest.TestCase):
    def test_equals_known_minus_discord(self):
        # ARCHIVE_PLATFORMS 文档定义 = KNOWN - discord（discord 走独立归档器）
        expected = [s for s in sources.KNOWN_SOURCES if s != 'discord']
        self.assertEqual(sources.ARCHIVE_PLATFORMS, expected)

    def test_discord_excluded(self):
        self.assertNotIn('discord', sources.ARCHIVE_PLATFORMS)


class TestSubsetRelations(unittest.TestCase):
    def test_core_subset_of_known(self):
        self.assertTrue(set(sources.CORE_SOURCES) <= set(sources.KNOWN_SOURCES),
                        f'CORE 越界: {set(sources.CORE_SOURCES) - set(sources.KNOWN_SOURCES)}')

    def test_r1_hard_fail_strict_subset_of_core(self):
        # 文档：R1_HARD_FAIL 是 CORE 的严格子集（单次跑命脉源，不含可降级的 youtube/discord）
        r1, core = sources.R1_HARD_FAIL_SOURCES, set(sources.CORE_SOURCES)
        self.assertTrue(r1 <= core, f'R1 越界: {r1 - core}')
        self.assertTrue(r1 < core, 'R1 应为 CORE 的严格子集')

    def test_auth_gated_keys_in_known(self):
        self.assertTrue(set(sources.AUTH_GATED) <= set(sources.KNOWN_SOURCES),
                        f'AUTH_GATED 含未知源: {set(sources.AUTH_GATED) - set(sources.KNOWN_SOURCES)}')

    def test_sparse_subset_of_known(self):
        self.assertTrue(set(sources.SPARSE_SOURCES) <= set(sources.KNOWN_SOURCES),
                        f'SPARSE 含未知源: {set(sources.SPARSE_SOURCES) - set(sources.KNOWN_SOURCES)}')


class TestAliases(unittest.TestCase):
    def test_alias_targets_are_known(self):
        # 别名必须归一化到一个真实的 KNOWN 源
        for raw, canon in sources.SOURCE_ALIASES.items():
            self.assertIn(canon, sources.KNOWN_SOURCES,
                          f'别名 {raw!r} 指向未知规范源 {canon!r}')

    def test_alias_keys_not_themselves_known(self):
        # 别名键是「原始变体名」，不应同时是规范 KNOWN 源（否则归一化语义自相矛盾）
        for raw in sources.SOURCE_ALIASES:
            self.assertNotIn(raw, sources.KNOWN_SOURCES,
                             f'别名键 {raw!r} 不应同时出现在 KNOWN_SOURCES')

    def test_normalize_idempotent(self):
        for raw in list(sources.SOURCE_ALIASES) + list(sources.KNOWN_SOURCES):
            once = sources.normalize_source(raw)
            self.assertEqual(sources.normalize_source(once), once,
                             f'normalize 非幂等: {raw!r}')


class TestArchivePlatformFold(unittest.TestCase):
    def test_fold_keys_and_values_resolve_to_known(self):
        for raw, folded in sources.ARCHIVE_PLATFORM_FOLD.items():
            self.assertIn(sources.normalize_source(raw), sources.KNOWN_SOURCES,
                          f'折叠键 {raw!r} 无法归一到 KNOWN')
            self.assertIn(folded, sources.KNOWN_SOURCES,
                          f'折叠目标 {folded!r} 非 KNOWN 源')

    def test_archive_platform_idempotent(self):
        samples = list(sources.KNOWN_SOURCES) + list(sources.SOURCE_ALIASES) \
            + list(sources.ARCHIVE_PLATFORM_FOLD)
        for s in samples:
            once = sources.archive_platform(s)
            self.assertEqual(sources.archive_platform(once), once,
                             f'archive_platform 非幂等: {s!r}')

    def test_archive_platform_folds_steam_family(self):
        self.assertEqual(sources.archive_platform('official'), 'steam')
        self.assertEqual(sources.archive_platform('steam_discussion'), 'steam')
        self.assertEqual(sources.archive_platform('steam_review'), 'steam')  # 经别名 + 折叠


class TestBackfillPlatforms(unittest.TestCase):
    def test_each_backfill_resolves_to_known(self):
        # BACKFILL 项允许是别名键（如 steam_review），但归一化后必须落在 KNOWN
        for p in sources.BACKFILL_PLATFORMS:
            self.assertIn(sources.normalize_source(p), sources.KNOWN_SOURCES,
                          f'回溯平台 {p!r} 无法归一到 KNOWN')

    def test_sync_with_backfill_registry(self):
        # 跨模块漂移守卫：sources.BACKFILL_PLATFORMS 与 backfill_platforms 的实际注册表必须一致
        import backfill_platforms
        self.assertEqual(
            set(sources.BACKFILL_PLATFORMS),
            set(backfill_platforms.BACKFILL_REGISTRY),
            'sources.BACKFILL_PLATFORMS 与 backfill_platforms.BACKFILL_REGISTRY 已漂移')


class TestSeparateRegistries(unittest.TestCase):
    def test_independent_archive_disjoint_from_known(self):
        # 独立归档源不得进 KNOWN（否则 split_output 会切出空 latest 文件，见 sources.py 注释）
        self.assertEqual(set(sources.INDEPENDENT_ARCHIVE_SOURCES) & set(sources.KNOWN_SOURCES), set())

    def test_legacy_disjoint_from_known(self):
        # 遗留源采集逻辑已移除，不应仍在活跃 KNOWN 清单里
        self.assertEqual(set(sources.LEGACY_SOURCES) & set(sources.KNOWN_SOURCES), set())


class TestRegionRegistries(unittest.TestCase):
    def test_region_apps_platforms_known(self):
        for platform, regions in sources.REGION_APPS.items():
            self.assertIn(platform, sources.KNOWN_SOURCES,
                          f'REGION_APPS 平台 {platform!r} 非 KNOWN 源')
            self.assertIn('global', regions, f'{platform} 缺 global 区服')
            self.assertIn('jp', regions, f'{platform} 缺 jp 区服')

    def test_discord_guilds_have_three_regions(self):
        self.assertEqual(set(sources.DISCORD_GUILDS), {'global', 'jp', 'volunteer'})

    def test_taptap_cn_apps_present(self):
        self.assertEqual(set(sources.TAPTAP_CN_APPS), {'reserve', 'cbt'})


if __name__ == '__main__':
    unittest.main()
