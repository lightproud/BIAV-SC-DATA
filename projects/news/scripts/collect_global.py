#!/usr/bin/env python3
"""
collect_global.py — 全球社区采集桥接脚本

调用 global_collectors.py 的 21 个采集器，
合并 aggregator.py 的输出，生成统一的 news.json。

运行方式:
  python projects/news/scripts/collect_global.py

工作流程:
  1. 运行零成本采集器（不需要 API Key 的那些）
  2. 读取 aggregator.py 已有的 news.json（如果存在）
  3. 合并、去重、排序
  4. 写回 news.json
"""

import json
import os
import sys
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, UTC
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Ensure sibling scripts dir is importable (works both as script and module)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_layout  # noqa: E402  运行期工作根单一真相源

# 运行期工作根（不进 git，2026-08-21 输出展示层删除）：路径 SSOT 见 archive_layout。
OUTPUT_PATH = archive_layout.news_run_root() / 'news.json'
RAW_OUTPUT_PATH = archive_layout.news_run_root() / 'news-raw.json'


# 核心源 + 需 secret 的源元数据统一取自 sources.py（单一真相源，杜绝硬编码漂移）。
# 核心源失败（含静默吐 0）须以非零退出暴露（§4.2 R1：任一核心源失败即整次失败）。
from sources import CORE_SOURCES, AUTH_GATED
import news_common  # 原子写单一真源（dump_json_atomic）


# ── Source mapping: collector source names → aggregator source names ──
SOURCE_MAP = {
    'bilibili': 'bilibili',
    'reddit': 'reddit',
    'youtube': 'youtube',
    'taptap': 'taptap',
    # 'steam' 不再重映射：steam 三源采集器（2026-08-22 自 AC 栈迁入）各自直接产
    # steam / steam_review / steam_discussion，旧的 steam→steam_review 映射会把官方
    # 新闻错标成评价，落档就进错桶。
    'steam': 'steam',
    'steam_review': 'steam_review',
    'steam_discussion': 'steam_discussion',
    'weibo': 'weibo',
    'bahamut': 'bahamut',
    'appstore': 'appstore',
    'google_play': 'google_play',
    'pixiv': 'pixiv',
    'note_com': 'note_com',
    'ruliweb': 'ruliweb',
    'stopgame': 'stopgame',
    'weixin': 'weixin',
    'discord': 'discord',
}


def convert_item(item: dict) -> dict:
    """Convert a report-system item to aggregator format."""
    source = SOURCE_MAP.get(item.get('source', ''), item.get('source', 'unknown'))
    converted = {
        'title': item.get('title', ''),
        'summary': item.get('summary', ''),
        'source': source,
        'time': item.get('time', ''),
        'url': item.get('url', ''),
        'engagement': item.get('engagement', 0),
        'is_hot': item.get('is_hot', False),
        'author': item.get('author', ''),
        'tags': item.get('tags', []),
        'lang': item.get('lang', ''),
        'platform_region': item.get('platform_region', ''),
    }
    # Preserve media fields for image archival
    if item.get('media_url'):
        converted['media_url'] = item['media_url']
        converted['content_type'] = item.get('content_type', 'image')
    # Preserve metadata (comments, play counts, reactions, etc.)
    if item.get('metadata') and isinstance(item['metadata'], dict):
        converted['metadata'] = item['metadata']
    # 甲方案归档分层字段（2026-06-21 采集源命名规范）：采集器标了才透传，
    # archive_platforms 据此分桶 <平台>/<区服>/<类型>/；缺省不落字段 → 回落扁平。
    if item.get('region'):
        converted['region'] = item['region']
    if item.get('archive_subtype'):
        converted['archive_subtype'] = item['archive_subtype']
    return converted


def dedup_key(item: dict) -> str:
    """Generate dedup key for an item. URL-first, title fallback — aligned with aggregator."""
    url = (item.get('url', '') or '').replace('http://', 'https://').strip().rstrip('/')
    if url:
        return url
    return f"{item.get('title', '')[:60]}|{item.get('source', '')}|{item.get('author', '')}"


def run_zero_cost_collectors() -> list[dict]:
    """Run all collectors that don't require API keys."""
    items = []

    try:
        import global_collectors as c
        c._refresh_cutoff()
    except ImportError:
        # exception() 而非 error()：核心采集模块导入失败是排障起点，丢掉 traceback
        # 就只剩一句「Cannot import」，看不出到底是哪一层依赖缺失。
        # exception() 已自带完整 traceback（含异常本身），再把 {e} 拼进消息是重复。
        logger.exception("Cannot import global_collectors module")
        return items, []

    # 数据质量追踪器：更新各源状态，长期沉默的源自动 dormant 跳过
    tracker = None
    try:
        sys.path.insert(0, str(_REPO_ROOT / 'projects' / 'news' / 'scripts'))
        from data_quality import SilentPlatformTracker
        tracker = SilentPlatformTracker()
    except Exception as e:
        logger.debug(f'SilentPlatformTracker not available: {e}')

    # Playwright fallback: platforms where HTTP fails but browser works
    PW_FALLBACK: dict[str, str] = {
        # name → playwright_collectors function name
        'Ruliweb':     'fetch_ruliweb_playwright',
        'Bahamut':     'fetch_bahamut_playwright',
        'Weibo':       'fetch_weibo_playwright',
    }

    # Load playwright_collectors module once
    pw_mod = None
    try:
        import playwright_collectors as pw_mod
    except ImportError:
        logger.debug('playwright_collectors not available')

    # NOTE: ARCH-01 收敛（2026-06-20）曾把 reddit / bilibili / taptap 的权威实现放在 AC 栈
    # （aggregator 富数据版），GC 退出对它们的调度。**2026-08-22 守密人裁定「采集 → 直接
    # 入湖」后 AC 栈整体退役**，三源的权威实现回落本栈（GC 版：无评论/媒体富采集，条目本身
    # 照采照归档——这是裁定的已知代价，如实记录）；steam 三源为 AC 独有，其实现已整段迁入
    # global_collectors，覆盖面不缩窄。discord 仍由 discord_archiver 独立采集直落数据湖，
    # 本栈不调度（原 AC fetch_discord_local 只为把归档条目并进新闻流，流已退役即无用）。
    # Zero-cost collectors (no API key / no cookie required)
    zero_cost_fetchers = [
        ('Reddit', c.fetch_reddit),
        ('Bilibili', c.fetch_bilibili),
        ('TapTap', c.fetch_taptap),
        ('Steam News', c.fetch_steam_news),
        ('Steam Reviews', c.fetch_steam_reviews),
        ('Steam Discussions', c.fetch_steam_discussions),
        ('Weibo', c.fetch_weibo),
        ('App Store', c.fetch_appstore_reviews),
        ('Pixiv', c.fetch_pixiv),
        ('Note.com', c.fetch_note_com),
        ('Ruliweb', c.fetch_ruliweb),
        ('StopGame', c.fetch_stopgame),
        ('搜狗微信', c.fetch_weixin),
        # Twitter 已退出编排（守密人 2026-07-30 裁定摘除注册表；fetch_twitter 保留在 GC）
    ]

    # Collectors that may use API keys when available, fall back to public endpoints otherwise
    api_fetchers = [
        ('YouTube', c.fetch_youtube),
        ('Bahamut', c.fetch_bahamut),
        # Arca.live 已退出编排（守密人 2026-08-16 裁定摘除注册表；CF 封死 Actions
        # 机房 IP，采集器与 collect_arca_daily.py 单脚本均保留在树上待复用）
        ('Google Play', c.fetch_google_play),
    ]

    all_fetchers = zero_cost_fetchers + api_fetchers

    # 显示名 → source_id（与 archive/split 对齐）
    NAME_TO_SOURCE_ID = {
        'Reddit': 'reddit', 'Bilibili': 'bilibili', 'TapTap': 'taptap',
        'Steam News': 'steam', 'Steam Reviews': 'steam_review',
        'Steam Discussions': 'steam_discussion',
        'Weibo': 'weibo', 'App Store': 'appstore',
        'Pixiv': 'pixiv', 'Note.com': 'note_com', 'Ruliweb': 'ruliweb',
        'StopGame': 'stopgame', '搜狗微信': 'weixin',
        'YouTube': 'youtube',
        'Bahamut': 'bahamut',
        'Google Play': 'google_play',
    }

    # 各采集器互相独立、采集前无共享状态 → 用线程并行（阻塞 requests 用线程即可）。
    # 每个 worker 只返回自身结果，不触碰共享 items；合并在主线程按 all_fetchers 顺序进行，
    # 保证去重/排序结果与串行一致（PERF-02）。
    def _collect_one(name, fn):
        """Run one collector (with Playwright fallback). Returns (name, source_id, result, error)."""
        source_id = NAME_TO_SOURCE_ID.get(name, name.lower())
        try:
            result = fn()
            # Playwright fallback: if HTTP returned 0/empty and we have a PW fallback
            if not result and name in PW_FALLBACK and PW_FALLBACK[name] and pw_mod:
                pw_fn = getattr(pw_mod, PW_FALLBACK[name], None)
                if pw_fn:
                    logger.info(f"  ↻ {name}: HTTP empty, trying Playwright fallback...")
                    result = pw_fn()
            return name, source_id, (result or []), None
        except Exception as e:
            # Playwright fallback on exception too
            if name in PW_FALLBACK and PW_FALLBACK[name] and pw_mod:
                pw_fn = getattr(pw_mod, PW_FALLBACK[name], None)
                if pw_fn:
                    logger.info(f"  ↻ {name}: HTTP crashed, trying Playwright fallback...")
                    try:
                        result = pw_fn()
                        if result:
                            logger.info(f"  {name} (PW): recovered via Playwright")
                            return name, source_id, result, None
                    except Exception as pw_e:
                        logger.warning(f"  {name} Playwright also failed: {pw_e}")
            return name, source_id, [], str(e)

    # 提交所有非 dormant 采集器到线程池
    pending = []
    for name, fn in all_fetchers:
        source_id = NAME_TO_SOURCE_ID.get(name, name.lower())
        if tracker and tracker.should_skip_platform(source_id):
            logger.info(f"   {name}: dormant, skipping")
            continue
        pending.append((name, fn))

    results_by_name = {}
    with ThreadPoolExecutor(max_workers=min(8, len(pending) or 1)) as ex:
        future_to_name = {ex.submit(_collect_one, name, fn): name for name, fn in pending}
        for fut in future_to_name:
            name, source_id, result, error = fut.result()
            results_by_name[name] = (source_id, result, error)

    # 按 all_fetchers 稳定顺序合并，保持结果确定性
    succeeded = []
    failed = []
    empty = []
    core_failures = []
    for name, _ in all_fetchers:
        if name not in results_by_name:
            continue
        source_id, result, error = results_by_name[name]
        if error is not None:
            failed.append((name, error[:120]))
            logger.warning(f"  {name} FAILED: {error}")
            if tracker:
                tracker.update_platform_status(source_id, 0, error=error)
            # §4.2 R1: 核心源失败即整次失败
            if source_id in CORE_SOURCES:
                core_failures.append((source_id, error))
            continue
        if result:
            items.extend(result)
            succeeded.append((name, len(result)))
            logger.info(f"  {name}: +{len(result)} items")
            if tracker:
                tracker.update_platform_status(source_id, len(result))
        else:
            empty.append(name)
            gate_env = AUTH_GATED.get(source_id)
            if gate_env and not os.environ.get(gate_env):
                # 优雅降级：需 cookie/key 的源未配置 secret → 预期 0 产出，标注待配，不计采集故障
                logger.info(f"  · {name}: 0 items (待配 {gate_env}，已降级)")
                if tracker:
                    tracker.update_platform_status(source_id, 0, note=f"待配 {gate_env}")
            else:
                # 核心源静默吐 0：从 INFO 提升为 WARNING，不再悄悄溜过（§4.2 R1 告警）。
                # 不做硬失败：部分核心源（如 taptap）本就低频，0 产出不应阻断管线；
                # 持久信号交由健康层按 consecutive_silent_days 自动 degraded/dormant。
                # 硬失败（非零退出）仍只保留给抛异常的核心源（见 core_failures 主路径）。
                if source_id in CORE_SOURCES:
                    logger.warning(f"  {name}: CORE source 0 items — 已告警，健康层据连续沉默天数降级 (§4.2 R1)")
                else:
                    logger.info(f"  · {name}: 0 items")
                if tracker:
                    tracker.update_platform_status(source_id, 0)

    # Diagnostic summary
    logger.info("=== 采集诊断 ===")
    logger.info(f"成功 ({len(succeeded)}): {', '.join(f'{n}({c})' for n, c in succeeded)}")
    if empty:
        logger.info(f"空结果 ({len(empty)}): {', '.join(empty)}")
    if failed:
        logger.warning(f"失败 ({len(failed)}):")
        for name, err in failed:
            logger.warning(f"  {name}: {err}")

    return items, core_failures


# load_existing_news() 已退役（2026-08-22）：它读的是 aggregator 先写下的 news.json，
# 而 AC 栈随「采集 → 直接入湖」裁定整体删除，本模块成为唯一采集入口——没有"上一段"
# 的产物可并，existing 恒为空列表。保留合并函数本身（merge_and_dedup 仍做窗口过滤与去重）。


# Adaptive: match the lookback window used by collectors
try:
    from collection_state import get_lookback_hours
    MAX_AGE_HOURS = news_common.env_int('MAX_AGE_HOURS', 0) or get_lookback_hours()
except ImportError:
    MAX_AGE_HOURS = news_common.env_int('MAX_AGE_HOURS', 24)

# 稀疏源使用更宽时间窗口（SPARSE_SOURCES 来自 sources.py 单一真相源）
SPARSE_MAX_AGE_HOURS = news_common.env_int('SPARSE_MAX_AGE_HOURS', 30 * 24)
from sources import SPARSE_SOURCES


def _is_recent(time_str: str, source: str = '') -> bool:
    """Check if a timestamp is within (source-specific) max_hours of now."""
    if not time_str:
        return False
    max_hours = SPARSE_MAX_AGE_HOURS if source in SPARSE_SOURCES else MAX_AGE_HOURS
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt) < timedelta(hours=max_hours)
    except (ValueError, TypeError):
        return False


def merge_and_dedup(existing: list[dict], new_items: list[dict],
                    apply_recency_filter: bool = True) -> list[dict]:
    """Merge and deduplicate items, keeping higher-engagement version.
    Filters out items older than per-source max_age (sparse sources use 30d,
    others 24h). Pass apply_recency_filter=False to keep the full unfiltered
    set (用于 news-raw.json 全量归档源)。"""
    seen: dict[str, dict] = {}

    # Existing items first (they're already validated)
    for item in existing:
        if apply_recency_filter and not _is_recent(item.get('time', ''), item.get('source', '')):
            continue
        key = dedup_key(item)
        if key not in seen or item.get('engagement', 0) > seen[key].get('engagement', 0):
            seen[key] = item

    # New items (from global collectors)
    for item in new_items:
        converted = convert_item(item)
        if apply_recency_filter and not _is_recent(converted.get('time', ''), converted.get('source', '')):
            continue
        key = dedup_key(converted)
        if key not in seen or converted.get('engagement', 0) > seen[key].get('engagement', 0):
            seen[key] = converted

    # Sort by engagement descending
    return sorted(seen.values(), key=lambda x: x.get('engagement', 0), reverse=True)


def build_summary(items: list[dict]) -> str:
    """Build a summary string from top items."""
    top = items[:5]
    titles = [item.get('title', '')[:30] for item in top if item.get('title')]
    return '；'.join(titles) + '。' if titles else ''


# 失败哨兵（2026-08-22 自退役的 aggregator.py 迁入）：固定落 projects/news/，
# 与运行期工作根解耦（工作根可经 env 改道，哨兵位置不跟着漂）。
FAILURE_FLAG = Path(__file__).resolve().parent.parent / 'collect-failure.flag'


def _flag_failure(summary: str) -> None:
    try:
        FAILURE_FLAG.write_text(news_common.redact_secrets(summary), encoding='utf-8')
    except OSError as exc:
        logger.error(f'failed to write failure sentinel: {exc}')


def _mark_collected(item_count: int) -> None:
    """推进「采集到此为止」水位（collection_state.last_collected_at）。"""
    try:
        from collection_state import mark_collection_done
    except ImportError:
        return
    mark_collection_done(item_count=item_count)


def main():
    logger.info('=== 社区采集开始（唯一入口）===')
    FAILURE_FLAG.unlink(missing_ok=True)  # 清掉上次运行残留的哨兵

    # Step 1: Run global collectors
    global_items, core_failures = run_zero_cost_collectors()
    logger.info(f'全球采集完成: {len(global_items)} items')

    # Empty-data protection (lesson #2): all collectors empty means a failed run.
    # Never rewrite news.json on a blank run; exit non-zero so the workflow signals failure.
    if not global_items:
        logger.error('全部采集器返回空，疑似全线失败；保留 news.json 原样，非零退出。')
        sys.exit(1)

    # Step 2: 校验 + 清洗（2026-08-22 自退役的 aggregator_base 迁入 news_common）。
    # 原先只有 AC 栈走校验、GC 侧直通；唯一入口后全部条目一律校验，丢弃计数落盘供
    # silent_sources_audit --strict 门控。
    global_items = news_common.validate_all_news(global_items)

    existing: list[dict] = []  # AC 栈已退役，无上游产物可并（见 load_existing_news 退役说明）

    # Step 3: Merge and dedup
    merged = merge_and_dedup(existing, global_items)
    logger.info(f'合并去重后: {len(merged)} items')

    # Step 4: Write back
    # `sources_run` 原先赋的是 **条目数**（`len(global_items)`）——落进入库的 news.json
    # 就是 `"sources_run": 1439`，而全球采集器统共只有二十来个。一个名字说「跑了几个源」、
    # 值却是「收了几条」的字段，读者按字面读到的是一个不存在的规模。名字归名字，数归数：
    # 前者报本轮真正产出条目的去重源数，条目数另立 `items_collected`。
    producing_sources = {i.get('source') for i in global_items if i.get('source')}
    output = {
        'updated_at': datetime.now(UTC).isoformat(),
        'summary': build_summary(merged),
        'sources_run': len(producing_sources),
        'items_collected': len(global_items),
        'news': merged,
    }

    news_common.dump_json_atomic(OUTPUT_PATH, output)

    # 全量归档源：未经时窗过滤的合并集，供 archive_platforms 落档（真·全量层）。
    # news.json 仍是滚动窗口快照（保持有界），raw 只多保留被时窗砍掉的新鲜条目。
    raw_merged = merge_and_dedup(existing, global_items, apply_recency_filter=False)
    news_common.dump_json_atomic(RAW_OUTPUT_PATH, {
        'updated_at': output['updated_at'],
        'source': 'news-raw',
        'news': raw_merged,
    })
    logger.info(f'全量层写入: {len(raw_merged)} items → {RAW_OUTPUT_PATH.name}')

    # Stats
    sources = {}
    for item in merged:
        src = item.get('source', 'unknown')
        sources[src] = sources.get(src, 0) + 1

    logger.info('=== 数据源统计 ===')
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        logger.info(f'  {src}: {count}')
    logger.info(f'=== 全球采集完成: {len(merged)} items → {OUTPUT_PATH} ===')

    # 校验丢弃计数落盘（零丢弃也写零值文件，供健康侧稳定消费）。
    try:
        payload = news_common.write_validation_drops()
        if payload['total_dropped']:
            logger.warning(f"Validation drops this run: {payload['by_source']}")
    except Exception as exc:
        logger.error(f'failed to write validation-drops.json: {exc}')

    # §4.2 R1: 输出已落盘保全数据。核心源失败 → 写哨兵 + 0 退出（**不非零退出**）：
    # 非零会让 workflow 跳过后续 archive/repair/health 步骤，本轮成功源的数据随 runner
    # 一起销毁（H9 当年正是为此把 aggregator 改成哨兵制）。CI 末步检测哨兵再标红。
    if core_failures:
        names = ', '.join(f'{s} ({err[:80]})' for s, err in core_failures)
        _flag_failure(f'Core source(s) failed per §4.2 R1: {names}')
        logger.error(f'Core source(s) failed: {names}. Sentinel written; pipeline continues.')
        return

    # 水位只在整轮干净时推进（原 aggregator.mark_collected 的说明照搬）：某轮有源没采到
    # 却照样把水位推到「现在」，那段没采到的时间就再也不会被后续窗口覆盖——窗口被水位
    # 一路顶着，永远只回看 24 小时。
    _mark_collected(len(merged))


if __name__ == '__main__':
    main()
