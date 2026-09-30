"""Mutation-testing harness for archive_layout (see setup.cfg [mutmut]).

archive_layout 是归档布局单一真相源（P0-1，2026-07-02）：某源数据落在哪、怎么找，
全仓只有它回答；写方读方一律 import 它。折叠映射 / 默认区服 / 认领子目录 / discord
guild 注册表任何一处被扰动，都是「写方落的路径读方找不回」级别的事故（6 源假
degraded 的病根形态）。逻辑密、常量表小且每条都语义关键——符合变异区收录标准
（守密人 2026-07-11 裁定扩员）。行覆盖由 test_archive_layout.py 契约测试提供；
本档职责是让 mutmut 翻转任何映射条目 / 分支条件时测试必然变红。

Imports via PACKAGE path (`projects.news.scripts.archive_layout`) so mutmut's
runtime trampoline keys line up with the file-path-derived keys. Also a normal,
fast pytest module under plain `pytest tests/`.
"""
from pathlib import Path

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

from projects.news.scripts.archive_layout import (  # noqa: E402
    CLAIMED_SUBTYPES,
    DATE_STEM,
    DEFAULT_REGION,
    DEFAULT_SUBTYPE,
    DISCORD_GUILD_REGIONS,
    DISCORD_REGIONS,
    FOLDED_SOURCE_LAYOUT,
    build_relpath,
    dated_files,
    discord_region_dir,
    discord_region_roots,
    iter_discord_message_files,
    iter_source_files,
    resolve_write_layout,
)

import pytest  # noqa: E402


# ── 常量表逐条钉死（表小且每条都是路径语义，不属「数据噪声」）────────────────

def test_folded_source_layout_exact():
    assert FOLDED_SOURCE_LAYOUT == {
        'steam': ('steam', 'review'),
        'official': ('steam', 'news'),
        'steam_discussion': ('steam', 'discussion'),
        'taptap_review': ('taptap', 'review'),
    }


def test_claimed_subtypes_excludes_self_host():
    # steam 源自身宿主即 steam（_src == _plat），其 review 子目录不入认领表；
    # 认领表只挡「别的折叠源」认走的子目录，防宿主递归双计
    assert {
        'steam': {'news', 'discussion'},
        'taptap': {'review'},
    } == CLAIMED_SUBTYPES


def test_default_region_and_subtype_exact():
    assert DEFAULT_REGION == {
        'steam': 'global', 'appstore': 'global', 'google_play': 'global',
        'youtube': 'global', 'taptap': 'cn',
    }
    assert DEFAULT_SUBTYPE == {'youtube': 'video', 'taptap': 'post'}


def test_date_stem_regex():
    assert DATE_STEM.match('2026-07-11')
    assert not DATE_STEM.match('state')
    assert not DATE_STEM.match('2026-7-1')       # 必须补零
    assert not DATE_STEM.match('2026-07-11x')    # 全串锚定


# ── build_relpath：维度按需展开 ──────────────────────────────────────────────

def test_build_relpath_all_dims():
    assert build_relpath('steam', 'global', 'review', '2026-07-01') == \
        Path('steam/global/review/2026-07-01.json')


def test_build_relpath_flat_and_region_only():
    assert build_relpath('bilibili', None, None, '2026-07-01') == \
        Path('bilibili/2026-07-01.json')
    assert build_relpath('appstore', 'global', None, '2026-07-01') == \
        Path('appstore/global/2026-07-01.json')


# ── resolve_write_layout：写方唯一落点 ───────────────────────────────────────

@pytest.mark.parametrize(('source', 'expected'), [
    ('steam', ('steam', 'global', 'review')),
    ('official', ('steam', 'global', 'news')),
    ('steam_discussion', ('steam', 'global', 'discussion')),
    ('taptap_review', ('taptap', 'cn', 'review')),
    ('taptap', ('taptap', 'cn', 'post')),
    ('youtube', ('youtube', 'global', 'video')),
    ('appstore', ('appstore', 'global', None)),
    ('google_play', ('google_play', 'global', None)),
    ('bilibili', ('bilibili', None, None)),   # 未分层平台平铺即规范
])
def test_resolve_write_layout_defaults(source, expected):
    assert resolve_write_layout(source) == expected


def test_resolve_write_layout_explicit_overrides():
    assert resolve_write_layout('steam', region='jp') == ('steam', 'jp', 'review')
    assert resolve_write_layout('taptap', subtype='review') == ('taptap', 'cn', 'review')


def test_resolve_write_layout_subtype_forces_region():
    # 有类型必有区服：防 <平台>/<类型>/ 畸形层级
    assert resolve_write_layout('bilibili', subtype='clip') == ('bilibili', 'global', 'clip')


# ── iter_source_files / dated_files：读方唯一遍历 ────────────────────────────

@pytest.fixture
def tree(tmp_path):
    def mk(rel, content='[]'):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return p
    return tmp_path, mk


def test_iter_folded_source_merges_legacy_and_hosted(tree):
    root, mk = tree
    legacy = mk('official/2026-06-01.json')
    hosted = mk('steam/global/news/2026-07-01.json')
    mk('steam/global/review/2026-07-01.json')       # 他源文件不得混入
    got = set(iter_source_files('official', root))
    assert got == {legacy, hosted}


def test_iter_steam_walks_folded_branch(tree):
    # steam 自身即折叠源（宿主=自己）：走折叠分支 = 平级旧档 + */review/，
    # 绝不递归吞并 news/discussion（那是 official / steam_discussion 的地盘）
    root, mk = tree
    own = mk('steam/global/review/2026-07-01.json')
    flat = mk('steam/2026-06-01.json')
    mk('steam/global/news/2026-07-01.json')
    mk('steam/global/discussion/2026-07-01.json')
    got = set(iter_source_files('steam', root))
    assert got == {own, flat}


def test_iter_normal_source_skips_claimed_subdirs(tree):
    # 认领跳过逻辑的唯一现实走线：taptap 非折叠源（走递归分支），但其
    # */review/ 被折叠源 taptap_review 认领——宿主递归必须绕开防双计
    root, mk = tree
    post = mk('taptap/cn/post/2026-07-01.json')
    flat = mk('taptap/2026-06-01.json')
    mk('taptap/cn/review/2026-07-01.json')           # taptap_review 认领，须跳过
    got = set(iter_source_files('taptap', root))
    assert got == {post, flat}


def test_iter_missing_dirs_yield_nothing(tree):
    root, _ = tree
    assert list(iter_source_files('bilibili', root)) == []
    assert list(iter_source_files('official', root)) == []


def test_dated_files_sorted_and_filtered(tree):
    root, mk = tree
    b = mk('bilibili/2026-07-02.json')
    a = mk('bilibili/2026-07-01.json')
    mk('bilibili/state.json')                        # 非日期文件须滤除
    assert dated_files('bilibili', root) == [a, b]


def test_dated_files_sorts_by_stem_across_directories(tree):
    # 折叠源横跨旧平级目录与宿主分层目录：路径字典序（official/... < steam/...）
    # 与日期序相反——必须按 stem 排，防「按路径排碰巧对」的假绿
    root, mk = tree
    newer_legacy = mk('official/2026-07-02.json')          # 路径序在前、日期序在后
    older_hosted = mk('steam/global/news/2026-07-01.json')
    assert dated_files('official', root) == [older_hosted, newer_legacy]


# ── discord 布局：guild 注册表 + 区服根解析 ─────────────────────────────────

def test_discord_guild_registry_exact():
    assert DISCORD_GUILD_REGIONS == {
        '1131791637933199470': 'global',
        '1377475512716234902': 'jp',
        '1402537664619479100': 'volunteer',
    }
    assert DISCORD_REGIONS == ('global', 'jp', 'volunteer')  # sorted 去重元组


def test_discord_region_dir_known_and_unregistered(tmp_path):
    assert discord_region_dir(tmp_path, '1377475512716234902') == tmp_path / 'jp'
    # int 形态 guild_id 也须命中（内部 str() 归一）
    assert discord_region_dir(tmp_path, 1131791637933199470) == tmp_path / 'global'
    with pytest.raises(KeyError, match='999'):
        discord_region_dir(tmp_path, '999')


def test_discord_region_roots_new_layout_and_state_only(tmp_path):
    (tmp_path / 'global' / 'channels').mkdir(parents=True)
    (tmp_path / 'jp').mkdir()
    (tmp_path / 'jp' / 'state.json').write_text('{}', encoding='utf-8')
    roots = discord_region_roots(tmp_path)
    assert roots == {'global': tmp_path / 'global', 'jp': tmp_path / 'jp'}
    # volunteer 新旧皆无 → 不含该区服（而非空目录占位）


def test_discord_region_roots_legacy_fallback(tmp_path):
    legacy = tmp_path / 'guilds' / '1377475512716234902' / 'channels'
    legacy.mkdir(parents=True)
    roots = discord_region_roots(tmp_path)
    assert roots['jp'] == legacy.parent.resolve()
    # global 旧布局 = 挂根（'.'）：根下无 channels 时不得误报
    assert 'global' not in roots


def test_iter_discord_message_files_region_filter(tmp_path):
    g = tmp_path / 'global' / 'channels' / '0470'
    j = tmp_path / 'jp' / 'channels' / '4902'
    g.mkdir(parents=True)
    j.mkdir(parents=True)
    gf = g / '2026-07-01.jsonl'
    jf = j / '2026-07-01.jsonl'
    gf.write_text('', encoding='utf-8')
    jf.write_text('', encoding='utf-8')
    assert set(iter_discord_message_files(tmp_path)) == {gf, jf}
    assert list(iter_discord_message_files(tmp_path, region='jp')) == [jf]
