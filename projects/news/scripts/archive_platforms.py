#!/usr/bin/env python3
"""
多平台按日归档脚本 — 将 news.json（merged 全量层）按每条目真实日期落全量档案层。

存储结构（落点由 archive_layout 单一真相源算出，本文档只描述形态）:
  <数据湖>/Record/Community/          # 根 = env BIAV_SC_DATA_ROOT 或在树 Public-Info-Pool/
  ├── bilibili/YYYY-MM-DD.json        # 无区服维度的平台平铺
  ├── reddit/YYYY-MM-DD.json
  └── steam/<区服>/<类型>/YYYY-MM-DD.json   # 分层平台（steam 家族/appstore/google_play/youtube）
  冷热分层：上上个月及更早为 .json.gz（读方一律经 archive_layout.open_archive_text）。

  旧文档在此画的 `projects/news/data/platforms/` 树已不是任何真实落点——2026-06-21
  平台摊平进 Record/Community、T62 §7甲 又整体迁出 code 仓；照旧文档写读方即扫空屋。

日期语义：桶名是**北京日期**（UTC+8），基准取 archive_layout.archive_date_str/archive_today。

Discord 不在此处理（已有 discord_archiver.py 独立归档）。

每日文件格式:
  {
    "date": "YYYY-MM-DD",
    "archived_at": "ISO 8601",
    "source": "steam",
    "item_count": 5,
    "items": [ ... ]
  }

运行方式:
  python projects/news/scripts/archive_platforms.py              # 归档当天
  python projects/news/scripts/archive_platforms.py --date 2026-04-03  # 归档指定日期
  python projects/news/scripts/archive_platforms.py --stats      # 显示归档统计

去重: 同一天重复运行会合并条目（按 url 或 title+time 去重）。
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import ARCHIVE_PLATFORMS, normalize_source
import archive_layout
import news_common  # 原子写单一真源（dump_json_atomic）

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# 运行期工作根（不进 git）：本步读的是**同一轮 CI 内**上游刚写下的中间态。
OUTPUT_DIR = archive_layout.news_run_root()
# 平台摊平到 BPT 4R Record/Community 下，与 discord 同级（2026-06-21 迁移）
ARCHIVE_DIR = archive_layout.community_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认

# 全量层数据源：优先读 news-raw.json（collect_global 写的未过滤合并集），
# 回退 news.json。两者都绕开 split 的展示层时窗过滤；raw 进一步绕开 collect_global
# 的滚动窗口过滤，让被时窗砍掉的新鲜条目也能落档（真·全量层）。
RAW_NEWS = OUTPUT_DIR / 'news-raw.json'
INPUT_NEWS = OUTPUT_DIR / 'news.json'

# Discord 有独立归档器（discord_archiver.py），此处跳过
PLATFORMS = ARCHIVE_PLATFORMS


# URL 不可作去重键的源：拿到的不是文章直链，而是**每次请求都重新签发**的跳转链接，
# 同一篇文章每轮采集都得到一个全新 URL —— 用 URL 做键 = 永远判不出重复。
# weixin 走搜狗中转（/link?url=…&token=…）：实测同一篇公告在单日档里攒了 114 份、
# 另一篇 144 份，114 个 URL 无一相同（连 url 参数本身都轮换，只有 type 固定）。
# 这类源改用 `标题|时间|作者`——实测 time 字段稳定，正是能把它们收敛成 1 条的那一维。
_UNSTABLE_URL_SOURCES = {'weixin'}


def item_key(item: dict) -> str:
    """Generate a dedup key for an item."""
    source = normalize_source(item.get('source', '') or '')
    url = item.get('url', '').strip()
    if url and source not in _UNSTABLE_URL_SOURCES:
        return url
    # time_is_approximate = 采集器没能解析出真实发布时间、填的是 now()。那种时间
    # 每轮都不一样，放进键里等于给同一条内容每轮发一个新身份——与上面 URL 轮换
    # 是同一个坑。近似时间一律不入键。
    time_part = '' if item.get('time_is_approximate') else item.get('time', '')
    return f"{item.get('title', '')}|{time_part}|{item.get('author', '')}"


def load_news() -> list[dict]:
    """Load the full-layer archive source: news-raw.json if present, else news.json."""
    path = RAW_NEWS if RAW_NEWS.exists() else INPUT_NEWS
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as f:
        return json.load(f).get('news', [])


def item_date_utc8(item: dict, fallback: str) -> str:
    """Return the item's own date (UTC+8). Falls back when timestamp absent."""
    t = item.get('time', '')
    if not t:
        return fallback
    try:
        # 换算走 archive_layout（日期基准 SSOT）；原手写 `dt + timedelta(hours=8)` 还有个
        # 隐患：对已带非 UTC 偏移的 dt 也一律加 8 小时，等于把偏移算了两遍。
        return archive_layout.archive_date_str(datetime.fromisoformat(t))
    except (ValueError, TypeError):
        return fallback


def archive_path(platform: str, region: str | None, subtype: str | None, date_str: str) -> Path:
    """归档落点：``<平台>[/<区服>][/<类型>]/YYYY-MM-DD.json``。

    路径构造逻辑收编进 archive_layout（布局单一真相源，2026-07-02 P0-1）；
    本函数保留签名，仅拼接本模块的归档根。"""
    return ARCHIVE_DIR / archive_layout.build_relpath(platform, region, subtype, date_str)


def load_existing_archive(platform: str, region: str | None, subtype: str | None, date_str: str) -> dict:
    """Load existing archive file if it exists."""
    path = archive_path(platform, region, subtype, date_str)
    if not path.exists():
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def merge_items(existing_items: list[dict], new_items: list[dict]) -> list[dict]:
    """Merge new items into existing, deduplicating by key."""
    seen = set()
    merged = []

    for item in existing_items:
        key = item_key(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)

    for item in new_items:
        key = item_key(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)

    # Sort by engagement descending
    merged.sort(key=lambda x: x.get('engagement', 0), reverse=True)
    return merged


def write_archive(platform: str, region: str | None, subtype: str | None,
                  date_str: str, new_items: list[dict]) -> int:
    """Merge new_items into the (platform, region, subtype, date) archive. Returns final count."""
    path = archive_path(platform, region, subtype, date_str)
    # 冷层已有本日 .gz 时，裸文件只是**旁车**：写进去的必须是 .gz 里没有的增量。
    # 不减去冷层条目，同一条会在 .gz 与旁车各存一份，而读方（dated_files 冷热并出）
    # 两个都读 —— 全量档案层直接双计，直到下次月度压冷并轨才自愈。
    cold_keys = {item_key(i) for i in archive_layout.read_cold_doc(path).get('items', [])
                 if isinstance(i, dict)}
    if cold_keys:
        new_items = [i for i in new_items if item_key(i) not in cold_keys]

    existing = load_existing_archive(platform, region, subtype, date_str)
    merged = merge_items(existing.get('items', []), new_items)
    if not merged:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)

    archive_data = {
        'date': date_str,
        'archived_at': datetime.now(UTC).isoformat(),
        'source': platform,
        'item_count': len(merged),
        'items': merged,
    }
    if region:
        archive_data['region'] = region
    if subtype:
        archive_data['content_subtype'] = subtype
    # 原子替换：全量档案层的日文件动辄数 MB，直写若在 json.dump 中途被中断
    # （CI runner 回收 / OOM）就留下半截 JSON——读方一律 `except JSONDecodeError: continue`,
    # 于是这一天的归档静默消失，且下轮写方读不出旧条目、合并基线也跟着丢。
    news_common.dump_json_atomic(path, archive_data)
    return len(merged)


def archive_all(target_date: str | None, fallback_date: str) -> dict[str, int]:
    """Archive every item in news.json under (normalized source, its own UTC+8 date).

    Reading the merged news.json (not the time-window-filtered *-latest.json) keeps
    the archive a true full layer, and per-item date bucketing removes the midnight
    boundary loss + mislabeled-date fallback of the old single-date logic.

    target_date 非空时只归档该日；为空时归档 news.json 内出现的全部日期。
    """
    groups: dict[tuple[str, str | None, str | None, str], list[dict]] = defaultdict(list)
    for raw in load_news():
        norm = normalize_source(raw.get('source', 'unknown'))
        if norm == 'discord':  # 独立归档器处理
            continue
        # 落点解析走布局单一真相源：折叠（official→steam/news 等）须吃**折叠前**
        # 源名才能给对默认类型；无 region 字段的分层平台补默认区服，防止回填/
        # 缺字段条目在历史迁移后又长出平级文件（lesson #42）。
        src, region, subtype = archive_layout.resolve_write_layout(
            norm, raw.get('region') or None, raw.get('archive_subtype') or None)
        d = item_date_utc8(raw, fallback_date)
        if target_date and d != target_date:
            continue
        groups[(src, region, subtype, d)].append(raw)

    totals: dict[str, int] = defaultdict(int)
    for (src, region, subtype, d), items in groups.items():
        write_archive(src, region, subtype, d, items)
        totals[src] += len(items)
    return totals


def show_stats():
    """Display archive statistics for all platforms."""
    print('=== 平台归档统计 ===\n')
    total_files = 0
    total_items = 0

    for platform in PLATFORMS:
        platform_dir = ARCHIVE_DIR / platform
        if not platform_dir.exists():
            print(f'  {platform:12s}  (无归档)')
            continue

        # 遍历经 archive_layout（分层 + 冷热双扩展名一并产出）。原写法 rglob('*.json')
        # 只吃裸文件，冷层 .json.gz 一个不数；且 `f.stem` 对 `2026-04-01.json.gz` 返回
        # '2026-04-01.json'，排序后印出来的首末日期直接带上 .json 后缀。
        files = archive_layout.dated_files(platform, ARCHIVE_DIR)
        if not files:
            print(f'  {platform:12s}  (无归档)')
            continue

        file_count = len(files)
        item_count = 0
        dates = sorted(archive_layout.date_stem(f) for f in files)
        first_date = dates[0]
        last_date = dates[-1]

        for f in files:
            try:
                with archive_layout.open_archive_text(f) as fh:
                    data = json.load(fh)
                item_count += data.get('item_count', 0)
            except Exception as exc:
                # 静默 pass 会让损坏档案把条目数悄悄少算，统计看着正常实则缩水
                print(f'    ! 跳过不可读归档 {f.name}: {type(exc).__name__}: {exc}', file=sys.stderr)

        print(f'  {platform:12s}  {file_count:3d} 天  {item_count:5d} 条  ({first_date} ~ {last_date})')
        total_files += file_count
        total_items += item_count

    # Discord stats (from its own archive)
    # 落点问 archive_layout。原写法钉死在迁移前的在树旧根
    # `projects/news/data/discord/activity_daily`——T62 §7甲 把数据湖迁出 code 仓后
    # 该目录恒不存在，discord 那一行于是从统计里静默消失（既不报错也不显示「无归档」）。
    dc_files: list[Path] = []
    droot = archive_layout.discord_root()
    for region_dir in archive_layout.discord_region_roots(droot).values():
        stats_dir = region_dir / 'activity_daily'
        if stats_dir.is_dir():
            dc_files.extend(f for f in stats_dir.iterdir()
                            if archive_layout.DATE_STEM.match(archive_layout.date_stem(f)))
    if dc_files:
        dc_dates = sorted(archive_layout.date_stem(f) for f in dc_files)
        print(f'  {"discord":12s}  {len(dc_files):3d} 天         ({dc_dates[0]} ~ {dc_dates[-1]})  [独立归档]')
        total_files += len(dc_files)

    print(f'\n  合计：{total_files} 天 / {total_items} 条目')


def main():
    parser = argparse.ArgumentParser(description='多平台按日归档')
    parser.add_argument('--date', type=str, default=None,
                        help='归档日期 YYYY-MM-DD（默认今天 UTC+8）')
    parser.add_argument('--stats', action='store_true',
                        help='显示归档统计')
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    today = archive_layout.archive_date_str()

    if args.date:
        print(f'归档日期（限定）：{args.date}')
    else:
        print('归档日期：news.json 内全部日期（全量分桶）')
    print(f'归档目录：{ARCHIVE_DIR}/\n')

    totals = archive_all(target_date=args.date, fallback_date=today)
    if not totals:
        print('  (news.json 无可归档数据)')
    for platform in sorted(totals):
        print(f'  {platform:12s}  {totals[platform]} 条')

    print(f'\n完成，共归档 {sum(totals.values())} 条，覆盖 {len(totals)} 个源。')


if __name__ == '__main__':
    main()
