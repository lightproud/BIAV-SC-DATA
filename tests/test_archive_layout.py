"""archive_layout.py 契约测试：写方落的路径，读方必能找回来。

这是「归档布局单一真相源」（2026-07-02 P0-1）的核心不变量——此前布局知识散落
在写方与读方各自代码里，分层实施后互相失联（6 源假 degraded / 断档检测扫空屋 /
回填写平级与主线写分层对冲）。本测试锁定：任一注册源经 resolve_write_layout +
build_relpath 写下的文件，iter_source_files/dated_files 以同一源名必能读回，
且不会被别的源读走（跨源隔离）。
"""

import json
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_layout as al  # noqa: E402


def _write_via_layout(root: Path, source: str, date_str: str, payload=None) -> Path:
    platform, region, subtype = al.resolve_write_layout(source)
    path = root / al.build_relpath(platform, region, subtype, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or {"item_count": 1, "items": []}), encoding="utf-8")
    return path


# ── 写读往返：每个代表性源 ──────────────────────────────────────────────────

@pytest.mark.parametrize("source", [
    "steam", "official", "steam_discussion", "taptap_review", "reddit_comment",
    "appstore", "google_play", "youtube", "bilibili", "weibo",
])
def test_write_read_roundtrip(tmp_path, source):
    written = _write_via_layout(tmp_path, source, "2026-07-01")
    found = al.dated_files(source, tmp_path)
    assert written in found, (
        f"{source}: 写方落在 {written.relative_to(tmp_path)}，读方没找回来")


# ── 具体落点断言（规范形态锁定） ─────────────────────────────────────────────

def test_layout_targets_match_spec(tmp_path):
    expect = {
        "steam": "steam/global/review/2026-07-01.json",
        "official": "steam/global/news/2026-07-01.json",
        "steam_discussion": "steam/global/discussion/2026-07-01.json",
        "taptap_review": "taptap/cn/review/2026-07-01.json",
        "taptap": "taptap/cn/post/2026-07-01.json",
        "reddit_comment": "reddit/global/comment/2026-07-01.json",
        "appstore": "appstore/global/2026-07-01.json",
        "google_play": "google_play/global/2026-07-01.json",
        "youtube": "youtube/global/video/2026-07-01.json",
        "bilibili": "bilibili/2026-07-01.json",  # 单子类平台平铺即规范形态
    }
    for source, rel in expect.items():
        written = _write_via_layout(tmp_path, source, "2026-07-01")
        assert str(written.relative_to(tmp_path)) == rel


# ── 跨源隔离：宿主不吞折叠源，折叠源不吞宿主 ─────────────────────────────────

def test_cross_source_isolation(tmp_path):
    p_steam = _write_via_layout(tmp_path, "steam", "2026-07-01")
    p_news = _write_via_layout(tmp_path, "official", "2026-07-01")
    p_disc = _write_via_layout(tmp_path, "steam_discussion", "2026-07-01")

    steam_files = al.dated_files("steam", tmp_path)
    assert p_steam in steam_files
    assert p_news not in steam_files and p_disc not in steam_files

    official_files = al.dated_files("official", tmp_path)
    assert official_files == [p_news]


def test_host_platform_skips_claimed_subtype_dirs(tmp_path):
    """taptap 自身递归遍历须避开 taptap_review 认领的 review/ 子目录。"""
    p_review = _write_via_layout(tmp_path, "taptap_review", "2026-07-01")
    flat = tmp_path / "taptap" / "2026-06-30.json"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text("{}", encoding="utf-8")

    taptap_files = al.dated_files("taptap", tmp_path)
    assert flat in taptap_files and p_review not in taptap_files
    assert al.dated_files("taptap_review", tmp_path) == [p_review]


# ── 旧平级兼容：迁移前的历史文件仍可读 ──────────────────────────────────────

def test_legacy_flat_files_still_readable(tmp_path):
    legacy = tmp_path / "official" / "2026-06-22.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    layered = _write_via_layout(tmp_path, "official", "2026-07-01")
    found = al.dated_files("official", tmp_path)
    assert legacy in found and layered in found
    assert [f.stem for f in found] == ["2026-06-22", "2026-07-01"]  # 按日期升序


# ── 非日期文件过滤 ──────────────────────────────────────────────────────────

def test_non_date_files_filtered(tmp_path):
    pdir = tmp_path / "appstore" / "global"
    pdir.mkdir(parents=True)
    (pdir / "state.json").write_text("{}", encoding="utf-8")
    (pdir / "2026-07-01.json").write_text("{}", encoding="utf-8")
    assert [f.name for f in al.dated_files("appstore", tmp_path)] == ["2026-07-01.json"]


# ── discord 布局（2026-07-10 方案甲收编 SSOT）───────────────────────────────

def _write_discord_msg(root: Path, region: str, ch: str, date: str) -> Path:
    p = root / region / "channels" / ch / f"{date}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"id":"1","content":"x"}\n', encoding="utf-8")
    return p


def test_discord_write_read_roundtrip(tmp_path):
    """归档器经 discord_region_dir 落的文件，iter_discord_message_files 必能读回。"""
    for gid, region in al.DISCORD_GUILD_REGIONS.items():
        d = al.discord_region_dir(tmp_path, gid)
        assert d == tmp_path / region
        f = d / "channels" / "12345678" / "2026-07-10.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{"id":"1"}\n', encoding="utf-8")
    found = set(al.iter_discord_message_files(tmp_path))
    assert len(found) == 3
    jp_only = set(al.iter_discord_message_files(tmp_path, region="jp"))
    assert jp_only == {tmp_path / "jp" / "channels" / "12345678" / "2026-07-10.jsonl"}


def test_discord_unregistered_guild_fails_loud(tmp_path):
    """未登记 guild 归档必须响亮失败，杜绝匿名新服静默落根。"""
    with pytest.raises(KeyError, match="unregistered discord guild"):
        al.discord_region_dir(tmp_path, "9999999999")


def test_discord_legacy_layout_fallback(tmp_path):
    """未迁移克隆（Global 挂根、其余在 guilds/）读方仍能找回全部三服。"""
    (tmp_path / "channels" / "aaaa1111").mkdir(parents=True)
    (tmp_path / "channels" / "aaaa1111" / "2026-07-01.jsonl").write_text("{}\n")
    for gid in ("1377475512716234902", "1402537664619479100"):
        d = tmp_path / "guilds" / gid / "channels" / "bbbb2222"
        d.mkdir(parents=True)
        (d / "2026-07-01.jsonl").write_text("{}\n")
    roots = al.discord_region_roots(tmp_path)
    assert set(roots) == {"global", "jp", "volunteer"}
    assert roots["global"] == tmp_path.resolve()
    assert len(list(al.iter_discord_message_files(tmp_path))) == 3


def test_discord_new_layout_wins_over_legacy(tmp_path):
    """新旧布局并存时（迁移过渡期）以新布局为准，不双计。"""
    _write_discord_msg(tmp_path, "global", "cccc3333", "2026-07-02")
    (tmp_path / "channels" / "cccc3333").mkdir(parents=True)
    (tmp_path / "channels" / "cccc3333" / "2026-07-01.jsonl").write_text("{}\n")
    files = list(al.iter_discord_message_files(tmp_path, region="global"))
    assert files == [tmp_path / "global" / "channels" / "cccc3333" / "2026-07-02.jsonl"]


# ── 数据湖根解析（分仓桥接 SSOT，T62 方案 B）────────────────────────────────

def test_data_root_default_in_tree(monkeypatch):
    """env 未设 → 回落现仓在树 <repo>/Public-Info-Pool（分仓前行为）。"""
    monkeypatch.delenv("BIAV_SC_DATA_ROOT", raising=False)
    assert al.pool_root() == al._REPO_ROOT / "Public-Info-Pool"
    assert al.community_root() == al._REPO_ROOT / "Public-Info-Pool" / "Record" / "Community"
    assert al.discord_root() == al.community_root() / "discord"


def test_data_root_default_matches_legacy_hardcode(monkeypatch):
    """向后兼容硬约束：默认根须与迁移前各脚本硬编码路径逐字节相等。"""
    monkeypatch.delenv("BIAV_SC_DATA_ROOT", raising=False)
    legacy = al._REPO_ROOT / "Public-Info-Pool" / "Record" / "Community"
    assert al.community_root() == legacy
    assert al.discord_root() == legacy / "discord"


def test_data_root_env_override(monkeypatch, tmp_path):
    """env BIAV_SC_DATA_ROOT 设定 → 数据根整体换位至 BIAV-SC-DATA checkout。"""
    monkeypatch.setenv("BIAV_SC_DATA_ROOT", str(tmp_path / "biav-sc-data"))
    assert al.pool_root() == tmp_path / "biav-sc-data"
    assert al.community_root() == tmp_path / "biav-sc-data" / "Record" / "Community"
    assert al.discord_root() == tmp_path / "biav-sc-data" / "Record" / "Community" / "discord"


def test_data_root_env_expanduser(monkeypatch):
    """env 支持 ~ 展开（兄弟 checkout 常在家目录并列）。"""
    monkeypatch.setenv("BIAV_SC_DATA_ROOT", "~/biav-sc-data")
    assert str(al.pool_root()).startswith(str(Path("~").expanduser()))


def test_resolve_pool_pointer_record_follows_data_root(monkeypatch, tmp_path):
    """逻辑指针 Public-Info-Pool/Record/... 随数据根换位；非 Record 段仍落仓根。"""
    monkeypatch.setenv("BIAV_SC_DATA_ROOT", str(tmp_path / "data"))
    assert al.resolve_pool_pointer("/Public-Info-Pool/Record/Community/steam/") == (
        tmp_path / "data" / "Record" / "Community" / "steam")
    assert al.resolve_pool_pointer("Public-Info-Pool/Resource/x.md") == (
        al._REPO_ROOT / "Public-Info-Pool" / "Resource" / "x.md")
    monkeypatch.delenv("BIAV_SC_DATA_ROOT")
    assert al.resolve_pool_pointer("Public-Info-Pool/Record/Community/steam") == (
        al.community_root() / "steam")


# ── 归档日期基准（北京日期 SSOT，扫描修复 2026-07-28）──────────────────────────
# 归档桶名自始是北京日期（写方 archive_platforms / backfill_platforms / backfill_gap
# 各自手写 `+ timedelta(hours=8)` 落桶）。三处手写都有同一个洞：对**已带非 UTC 偏移**
# 的时间戳也一律加 8 小时 = 把偏移算了两遍；jp 源（+09:00）晚间条目因此整批落进次日桶。
# 基准收敛进 archive_layout 后由下列用例钉死。

def test_archive_date_str_naive_treated_as_utc():
    """无时区时间戳按 UTC 解读，再折算北京日期（与旧行为等价）。"""
    # 裸 datetime 是本用例的**被测对象**（无时区输入按 UTC 解读），不可加 tzinfo
    assert al.archive_date_str(datetime(2026, 4, 13, 20, 0, 0)) == "2026-04-14"  # noqa: DTZ001
    assert al.archive_date_str(datetime(2026, 4, 13, 15, 59, 0)) == "2026-04-13"  # noqa: DTZ001


def test_archive_date_str_utc_offset_explicit():
    """显式 +00:00 与 naive 同解。"""
    assert al.archive_date_str(
        datetime(2026, 4, 13, 20, 0, 0, tzinfo=UTC)) == "2026-04-14"


def test_archive_date_str_non_utc_offset_not_double_counted():
    """回归：+09:00 的 23:30 真实北京时是同日 22:30，不得被算进次日桶。

    旧手写换算 `(dt + timedelta(hours=8)).strftime(...)` 会得到 2026-06-02——
    对 jp 区服全体晚间条目系统性偏一天。
    """
    jst = timezone(timedelta(hours=9))
    assert al.archive_date_str(datetime(2026, 6, 1, 23, 30, tzinfo=jst)) == "2026-06-01"
    # 西向偏移同样不得反向偏：UTC-07:00 的 10:00 = 北京次日 01:00
    pdt = timezone(timedelta(hours=-7))
    assert al.archive_date_str(datetime(2026, 6, 1, 10, 0, tzinfo=pdt)) == "2026-06-02"


def test_archive_today_is_beijing_date():
    """「今天」= 北京日期，不随容器本地时区漂移（CI 为 UTC，每天差 8 小时的窗口）。"""
    now_bj = datetime.now(timezone(timedelta(hours=8))).date()
    assert al.archive_today() == now_bj
    assert al.archive_date_str() == now_bj.isoformat()
