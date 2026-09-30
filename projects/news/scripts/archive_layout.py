#!/usr/bin/env python3
"""archive_layout.py — 归档布局单一真相源（SSOT）。

背景（2026-07-02 体质改进 P0-1）：Record/Community 的目录布局知识此前散落在
写方（archive_platforms / backfill_platforms）与读方（silent_sources_audit /
repair_gaps / build_community_index）各自的代码里，2026-06-22 区服/类型分层
实施后读写认知漂移，造成 6 个采集源被误判沉默 10+ 天（同类病根还包括
collect_video_comments 写旧读新、backfill 写平级与分层写方对冲的隐患）。

本模块之后：**某源的数据落在哪、怎么找，全仓只有这里回答。**
写方与读方均 import 本模块；`tests/test_archive_layout.py` 以读写往返
契约测试锁定「写方落的路径，读方必能找回来」。

布局规范（甲方案，守密人 2026-06-21 裁定）：
  <平台>/<区服>/<类型>/YYYY-MM-DD.json —— 维度按需展开，单子类平台保持裸名平铺
  （bilibili/reddit/weibo/... 无区服维度，平级即规范形态，不是遗留）。

约定：本模块只提供**相对路径与遍历逻辑**，归档根目录由调用方持有并传入
（便于单测 monkeypatch 调用方自己的 ARCHIVE_DIR，不与本模块耦合）。
"""
from __future__ import annotations

import gzip
import json
import os
import re
from datetime import date, datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import TextIO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# 归档文件名 = 日期；state.json / manifest 等辅助文件不参与日期语义
DATE_STEM = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# ── 归档日期基准（北京时 UTC+8）────────────────────────────────────────────────
# 归档分桶用的「日期」自始就是**北京日期**：archive_platforms / backfill_platforms /
# backfill_gap 各自手写 `(dt + timedelta(hours=8)).strftime('%Y-%m-%d')` 落桶。
# 但「今天是哪天」在若干读方处是 `date.today()`——那取的是**容器本地日期**，CI 容器
# 为 UTC，于是每天有 8 小时（UTC 16:00–24:00 = 北京次日 00:00–08:00）算出的「今天」
# 比归档桶名整整早一天：缺口检测把已归档的当天误报为缺、冷压月界算错一个月。
# 日期基准自此只有这里一个答案，与 date_stem / build_relpath 同处布局 SSOT。
BEIJING_TZ = timezone(timedelta(hours=8))


def archive_today() -> date:
    """归档语义下的「今天」= 北京日期（与写方分桶基准同源）。"""
    return datetime.now(BEIJING_TZ).date()


def archive_date_str(when: datetime | None = None) -> str:
    """把时刻折算成归档桶名 YYYY-MM-DD（北京日期）；when=None 取当下。"""
    if when is None:
        return archive_today().isoformat()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(BEIJING_TZ).strftime('%Y-%m-%d')

# ── 数据湖根解析（分仓桥接 SSOT，T62 方案 B「兄弟 checkout + 环境根」）─────────
# 分仓（BIAV-SC-CODE + BIAV-SC-DATA）后数据湖 Public-Info-Pool/Record 迁出至
# BIAV-SC-DATA 仓。为让在树直读脚本零逻辑改动地跟随数据换位，Record 数据根统一经
# 下列函数解析：环境变量 BIAV_SC_DATA_ROOT 设定则用之（指向 BIAV-SC-DATA checkout
# 内扮演 Public-Info-Pool 角色的目录），否则回落现仓在树默认——**分仓前行为，
# 完全向后兼容**（env 未设 = 与旧硬编码路径逐字节相等）。
# 只覆盖随分仓移动的 **Record 数据湖**；Resource（策展产物）留 code 仓、不经此。
# 与「归档根由调用方传入」契约不冲突：遍历函数仍收 archive_dir 参数，本组只提供
# 调用方可选用的**规范默认根**，取代各自 `_REPO_ROOT / 'Public-Info-Pool' / ...` 硬编码。
_DATA_ROOT_ENV = 'BIAV_SC_DATA_ROOT'
# archive_layout.py 位于 <repo>/projects/news/scripts/ → parents[3] = 仓根
_REPO_ROOT = Path(__file__).resolve().parents[3]


def pool_root() -> Path:
    """数据湖池根（扮演 Public-Info-Pool 角色的目录）。env BIAV_SC_DATA_ROOT 或在树默认。"""
    env = os.environ.get(_DATA_ROOT_ENV)
    return Path(env).expanduser() if env else _REPO_ROOT / 'Public-Info-Pool'


def community_root() -> Path:
    """社区全量档案根：<pool>/Record/Community（discord + 16+ 平台摊平）。"""
    return pool_root() / 'Record' / 'Community'


_RECORD_POINTER_PREFIX = 'Public-Info-Pool/Record/'


def resolve_pool_pointer(rel: str, pool: Path | None = None) -> Path:
    """仓内逻辑指针（repo-relative，如 OKF ``resource`` 去掉前导 ``/``）→ 物理路径。

    指针字符串保留逻辑前缀 ``Public-Info-Pool/Record/...``（分仓前布局），本体已随数据湖
    迁 BIAV-SC-DATA：Record 段重定向到 ``pool_root() / 'Record'``（env 感知），
    其余路径（Resource / Reference 等留 code 仓者）仍落仓根。``pool`` 缺省取
    ``pool_root()``；调用方持有先前快照的数据根时显式传入。
    """
    rel = rel.lstrip('/')
    if rel.startswith(_RECORD_POINTER_PREFIX):
        return (pool or pool_root()) / 'Record' / rel[len(_RECORD_POINTER_PREFIX):]
    return _REPO_ROOT / rel


def discord_root() -> Path:
    """discord 归档根：<community>/discord。"""
    return community_root() / 'discord'


def media_manifest_path() -> Path:
    """媒体补录台账：<pool>/Record/media/backfill_manifest.json。

    守密人 2026-07-30 裁定（归档完整性审计待裁项①）：manifest 随数据湖迁 BIAV-SC-DATA
    数据仓——原 code 仓路径 projects/news/data/media/ 已被 .gitignore 整目录忽略（#876），
    CI 末步 git add 被拒致 backfill-media 自 2026-06-21 起 38 天无一次成功。
    台账是数据湖的操作记录，与 Record 同仓同推；media 二进制本体仍走 Releases 不变。
    放 Record/media/（Community 的兄弟目录）而非 Community 内，防被平台目录扫描器
    （silent_sources_audit / OKF pointer layers 等按 Community/* 枚举平台）误认成源。
    """
    return pool_root() / 'Record' / 'media' / 'backfill_manifest.json'


# ── 采集运行期工作根（2026-08-21 守密人裁定：输出展示层整层删除）──────────────
# 原 projects/news/output/ 是「输出展示层」：25 个快照文件随每轮采集提交进 git。
# 展示消费面（/news/ 页面、feed.xml RSS）下线后，它只剩管线内部中间态的职能——
# aggregator 写 news.json / news-raw.json，（原 split_output 拆 *-latest.json，08-22 随新闻流退役删除）
# archive_platforms 读它们写数据湖。这些文件**同一轮内产生、同一轮内吃掉**，
# 进 git 只会让仓库长期驻留一份「看起来是当前热点、实际取决于上次 CI 何时跑」的快照。
# 故整层改落**运行期工作目录**：不进 git（.gitignore），每轮现造现用。
# 唯一例外是**跨轮状态**（source-health.json 的沉默天数、validation-drops.json 的
# 校验丢弃计数）——它们靠读上一轮自己的落盘值累计，一旦随工作目录蒸发，7 天沉默降级 /
# 30 天休眠判定每轮从零重建，等于永不触发。这两份改落 news_state_root()（进 git）。
_RUN_ROOT_ENV = 'BIAV_NEWS_RUN_DIR'


def news_run_root() -> Path:
    """采集运行期工作根：env BIAV_NEWS_RUN_DIR 或在树默认 projects/news/run/（不进 git）。

    只放**单轮内产生、单轮内消费**的中间产物；任何需要跨轮累计的状态一律走
    news_state_root()。目录不在此创建——写方经 news_common.dump_json_atomic
    （自建父目录）或显式 mkdir 落盘，读方须容忍不存在（首轮 / 干净检出即空）。
    """
    env = os.environ.get(_RUN_ROOT_ENV)
    return Path(env).expanduser() if env else _REPO_ROOT / 'projects' / 'news' / 'run'


def news_state_root() -> Path:
    """采集跨轮状态根：projects/news/data/（进 git，随 update-news CI 提交回仓）。"""
    return _REPO_ROOT / 'projects' / 'news' / 'data'

# ── 折叠映射：源 → (宿主平台, 类型子目录) ────────────────────────────────────
# steam 家族三子类共享宿主 steam；taptap 评论流归 taptap/*/review。
# taptap 族采集器已删（守密人 2026-09-30 裁定），但本映射与下方 taptap 默认落点**保留**：
# 读方遍历历史档 taptap/ 时须靠 taptap_review 认领 review/ 子目录防双计，删了读侧即错。
# 与 sources.SOURCE_ALIASES / ARCHIVE_PLATFORM_FOLD 语义对齐（那边管「叫什么」，
# 这边管「放哪里」）。
FOLDED_SOURCE_LAYOUT: dict[str, tuple[str, str]] = {
    'steam':            ('steam', 'review'),
    'official':         ('steam', 'news'),
    'steam_discussion': ('steam', 'discussion'),
    'taptap_review':    ('taptap', 'review'),
}

# 宿主平台下被折叠源认领的类型子目录（宿主默认递归遍历时须避开，防双计）
CLAIMED_SUBTYPES: dict[str, set[str]] = {}
for _src, (_plat, _sub) in FOLDED_SOURCE_LAYOUT.items():
    if _src != _plat:
        CLAIMED_SUBTYPES.setdefault(_plat, set()).add(_sub)

# ── 写方默认落点：item 未携带 region/archive_subtype 字段时的兜底 ─────────────
# 只为「新数据已走分层」的平台设默认——防止无字段条目（如 backfill 回填的
# 历史条目）在迁移后又长出平级文件（lesson #42 对冲永动机）。
# 未列平台（bilibili 等单子类）保持平铺，属规范形态。
DEFAULT_REGION: dict[str, str] = {
    'steam': 'global',
    'appstore': 'global',
    'google_play': 'global',
    'youtube': 'global',
    # taptap 区服 = cn（守密人 2026-06-21 裁定⑧：国服预约+测试服合并 taptap/cn/，
    # 条目内 app_id 字段区分；非 global）
    'taptap': 'cn',
}
DEFAULT_SUBTYPE: dict[str, str] = {
    'youtube': 'video',
    # taptap 多子类显式（裁定⑤）：帖子 post / 评论 review（review 归 taptap_review 源）
    'taptap': 'post',
}


def build_relpath(platform: str, region: str | None, subtype: str | None,
                  date_str: str) -> Path:
    """归档相对路径（不含归档根）：<平台>[/<区服>][/<类型>]/YYYY-MM-DD.json。"""
    parts = [platform]
    if region:
        parts.append(region)
    if subtype:
        parts.append(subtype)
    return Path(*parts) / f'{date_str}.json'


def resolve_write_layout(source: str, region: str | None = None,
                         subtype: str | None = None) -> tuple[str, str | None, str | None]:
    """写方唯一落点解析：源名 → (宿主平台, 区服, 类型)。

    折叠源套 FOLDED_SOURCE_LAYOUT 给出宿主与类型；缺 region 的分层平台补
    DEFAULT_REGION；缺 subtype 的补 DEFAULT_SUBTYPE。未分层平台原样返回
    （region/subtype 保持 None → 平铺）。
    """
    if source in FOLDED_SOURCE_LAYOUT:
        platform, folded_subtype = FOLDED_SOURCE_LAYOUT[source]
        subtype = subtype or folded_subtype
    else:
        platform = source
    region = region or DEFAULT_REGION.get(platform)
    subtype = subtype or DEFAULT_SUBTYPE.get(platform)
    # 有类型必有区服（规范：区服上、类型下），防写出 <平台>/<类型>/ 畸形层级
    if subtype and not region:
        region = 'global'
    return platform, region, subtype


def date_stem(path: Path | str) -> str:
    """归档文件的日期茎：剥掉 .json/.jsonl 及冷压 .gz 双重后缀。

    冷热分层后 `2026-04-01.json.gz` 的 Path.stem 是 '2026-04-01.json'——
    直接拿 stem 解析日期会把整个冷层误判成非日期文件 / 缺口，日期一律经本函数。
    """
    name = Path(path).name
    for suf in ('.json.gz', '.jsonl.gz', '.json', '.jsonl'):
        if name.endswith(suf):
            return name[:-len(suf)]
    return Path(path).stem


def iter_source_files(source: str, archive_dir: Path) -> Iterator[Path]:
    """读方唯一遍历：产出某源的全部归档日期文件（平铺旧布局 + 分层新布局）。

    折叠源：本源旧平级目录 + 宿主平台 <任意区服>/<类型>/ 下的文件。
    普通源：源目录递归，但跳过被其他折叠源认领的类型子目录。
    discord 不经本函数（独立归档器与目录语义，调用方自理）。
    冷热分层（2026-07-12 甲案推广）：冷月为 .json.gz，与裸文件一并产出。
    """
    if source in FOLDED_SOURCE_LAYOUT:
        legacy = archive_dir / source
        if legacy.exists():
            yield from legacy.glob('*.json')
            yield from legacy.glob('*.json.gz')
        platform, subtype = FOLDED_SOURCE_LAYOUT[source]
        base = archive_dir / platform
        if base.exists():
            yield from base.glob(f'*/{subtype}/*.json')
            yield from base.glob(f'*/{subtype}/*.json.gz')
        return
    pdir = archive_dir / source
    if not pdir.exists():
        return
    claimed = CLAIMED_SUBTYPES.get(source, set())
    for pattern in ('*.json', '*.json.gz'):
        for f in pdir.rglob(pattern):
            if f.parent.name in claimed:
                continue
            yield f


def dated_files(source: str, archive_dir: Path) -> list[Path]:
    """某源全部日期文件，按日期升序；过滤 state/manifest 类非日期文件。"""
    return sorted((f for f in iter_source_files(source, archive_dir)
                   if DATE_STEM.match(date_stem(f))),
                  key=date_stem)


# ── discord 布局（守密人 2026-07-10 批准方案甲，收编 SSOT）───────────────────
# 三服统一 discord/{global,jp,volunteer}/（根特例消灭：原 Global 挂根、其余在
# guilds/{guild_id}/ 的双轨布局于同日迁移归位）。每区服目录内部结构不变：
# channels/{id_suffix}/{date}.jsonl + activity_daily/ + state.json 等五件套。
# guild_id → 区服名注册表是唯一映射源；新 guild 接入必须先登记，未登记归档
# 一律响亮失败——杜绝匿名新服静默落根（旧根特例的病根形态）。

DISCORD_GUILD_REGIONS: dict[str, str] = {
    '1131791637933199470': 'global',      # Global 官方服
    '1377475512716234902': 'jp',          # 日服（AltPlus）
    '1402537664619479100': 'volunteer',   # 志愿者服
}
DISCORD_REGIONS = tuple(sorted(set(DISCORD_GUILD_REGIONS.values())))

# 旧布局回落映射（迁移过渡期 / 未迁移克隆）：区服 → 旧路径（相对 discord 根）
_DISCORD_LEGACY_SUBDIR: dict[str, str] = {
    'global': '.',
    'jp': 'guilds/1377475512716234902',
    'volunteer': 'guilds/1402537664619479100',
}


def discord_region_dir(discord_root: Path, guild_id: str) -> Path:
    """写方唯一落点：guild_id → discord/<区服>/。未登记 guild 响亮失败。"""
    region = DISCORD_GUILD_REGIONS.get(str(guild_id))
    if region is None:
        raise KeyError(
            f'unregistered discord guild {guild_id}: '
            f'register it in archive_layout.DISCORD_GUILD_REGIONS before archiving'
        )
    return discord_root / region


def discord_region_roots(discord_root: Path) -> dict[str, Path]:
    """读方唯一根解析：区服 → 数据目录（新布局优先，回落旧布局；两者皆无则不含该区服）。"""
    roots: dict[str, Path] = {}
    for region in DISCORD_REGIONS:
        new = discord_root / region
        if (new / 'channels').exists() or (new / 'state.json').exists():
            roots[region] = new
            continue
        legacy = (discord_root / _DISCORD_LEGACY_SUBDIR[region]).resolve()
        if (legacy / 'channels').exists():
            roots[region] = legacy
    return roots


def open_archive_text(path: Path | str, mode: str = 'rt') -> TextIO:
    """归档文本统一开档：裸 .jsonl/.json 与 .jsonl.gz/.json.gz 透明双开。

    冷热分层（守密人 2026-07-12 甲案裁定）：上上个月及更早的 discord dated 文件
    按月压成 .gz（`discord_cold_compress.py` 月度压冷），当月 + 上月保持裸文本热层。
    读方一律经本函数开档，冷热无感；gzip 为标准库，零新依赖。
    """
    p = str(path)
    if p.endswith('.gz'):
        return gzip.open(p, mode if 't' in mode else mode + 't', encoding='utf-8')
    return open(p, mode.replace('t', '') or 'r', encoding='utf-8')


def read_cold_doc(path: Path | str) -> dict:
    """同日期冷层文档：裸路径 `<date>.json` → 读 `<date>.json.gz`；无冷层 / 不可读返回 {}。

    冷月被回填追加时写方会在 .gz 旁落一个裸旁车，读方（`dated_files` 冷热并出）
    两个都读即全量——所以写方**必须先看冷层里已有什么**，只把增量写进旁车。
    否则同一条目在 .gz 与旁车里各存一份，读方双计（lesson #30 同型的抽样失真）。
    """
    gz = Path(str(path) + '.gz')
    if not gz.exists():
        return {}
    try:
        with open_archive_text(gz) as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def iter_discord_message_files(discord_root: Path,
                               region: str | None = None) -> Iterator[Path]:
    """读方唯一遍历：channels/{id_suffix}/{date}.jsonl[.gz]；region=None 遍历全部区服。

    冷热分层后裸与 .gz 并存皆产出；同日期「冷 .gz + 裸旁车」并存（冷月被历史回填
    追加时产生）由写方 gz 感知去重保证无重复行，读方两个都读即全量。
    """
    for r, root in discord_region_roots(discord_root).items():
        if region is not None and r != region:
            continue
        base = root / 'channels'
        if base.exists():
            yield from base.glob('*/*.jsonl')
            yield from base.glob('*/*.jsonl.gz')
