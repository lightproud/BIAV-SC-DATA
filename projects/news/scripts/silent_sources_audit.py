#!/usr/bin/env python3
"""
silent_sources_audit.py — 沉默源审计（基于归档历史）

扫描 projects/news/data/platforms/*/*.json 和 data/discord/activity_daily/*.json，
为每个已注册的采集源计算：
  - days_archived：归档天数
  - total_items：累计条目数
  - last_archive_date / first_archive_date：归档窗口
  - silent_days：距最近一次产出的天数
  - level：active / degraded / dormant / never

输出：
  1. 控制台可读报告（按分级排序）
  2. --write：写入 output/source-health.json，供 SilentPlatformTracker 作为种子数据
  3. --suggest-prune：列出建议从 workflow 摘除的源

使用:
    python projects/news/scripts/silent_sources_audit.py
    python projects/news/scripts/silent_sources_audit.py --write
    python projects/news/scripts/silent_sources_audit.py --suggest-prune

注意：归档窗口受 archive_platforms.py 实际运行次数限制。若窗口 < 30 天，
"never" 仅代表"窗口内从未产出"，不等于真正 30 天沉默。
"""

import argparse
import itertools
import json
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import KNOWN_SOURCES, CORE_SOURCES, LEGACY_SOURCES, INDEPENDENT_ARCHIVE_SOURCES
import archive_layout

ARCHIVE_DIR = archive_layout.community_root()  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认
# discord 健康以主服 global 的每日统计为准（原语义不变）；2026-07-10 方案甲布局
# 迁 discord/global/activity_daily/，经 SSOT 解析并回落旧布局（迁移前克隆兼容）。
_DISCORD_ROOT = ARCHIVE_DIR / 'discord'
DISCORD_ARCHIVE_DIR = (archive_layout.discord_region_roots(_DISCORD_ROOT)
                       .get('global', _DISCORD_ROOT / 'global')) / 'activity_daily'
# 跨轮状态（沉默天数 / 校验丢弃计数按轮累计）：2026-08-21 输出展示层删除后落 data/，
# 随 update-news CI 提交回仓——放运行期工作根等于每轮清零，沉默降级永不触发。
HEALTH_PATH = archive_layout.news_state_root() / 'source-health.json'
DROPS_PATH = archive_layout.news_state_root() / 'validation-drops.json'

# 单轮校验丢弃超过该值即告警（P0-3；taptap_review 事故单轮丢 108 条为定标参照，
# 阈值取其一半——半个事故就该响铃，又高于正常脏数据零星过滤的噪声水平）
DROPS_ALARM_THRESHOLD = 50

# 已注册采集源 —— 来自 sources.py 单一真相源（含 discord）
ALL_REGISTERED_SOURCES = list(KNOWN_SOURCES) + list(INDEPENDENT_ARCHIVE_SOURCES)

# 与 data_quality.SilentPlatformTracker 保持一致
DEGRADED_THRESHOLD = 7
DORMANT_THRESHOLD = 30

# 审计窗口下限：weixin 等按内容发布日期落桶的源存在 2016-2020 老文
# （游戏公开前的关键词噪声），会把窗口拉长数千天、稀释覆盖统计。
# 下限取游戏相关内容可信起点（discord 历史最早月 2023-07）。
AUDIT_WINDOW_FLOOR = '2023-07-01'

# 叶级下钻（区服/类型粒度）：平台级审计对区服子层断档不可见——
# appstore/jp 曾连续日更 44 天后骤停 30 天而平台仍显示活跃
# （06-22「平台级看不见分层」事故的同构复发）。叶 = 含日期文件的目录
# （<平台>[/<区服>][/<类型>]）。稀疏叶（jp 系评论等）用近期节拍中位数
# 做自适应告警阈值，避免「评论本来就少」刷屏假警报。
LEAF_CADENCE_WINDOW = 10   # 估节拍用最近 N 个归档日期
LEAF_CADENCE_FACTOR = 3    # stall 阈值 = max(DEGRADED, FACTOR x 节拍中位数)

# 布局知识（折叠映射 / 区服递归 / 日期文件过滤）收编进 archive_layout 单一真相源
# （2026-07-02 P0-1）；本模块只管健康分级，遍历全部委派。
_DATE_STEM = archive_layout.DATE_STEM


def _platform_dir(source: str) -> Path:
    return DISCORD_ARCHIVE_DIR if source == 'discord' else ARCHIVE_DIR / source


def _iter_archive_files(source: str):
    """产出某源的全部归档日期文件——遍历逻辑在 archive_layout（布局单一真相源）。"""
    if source == 'discord':
        yield from DISCORD_ARCHIVE_DIR.glob('*.json')
        yield from DISCORD_ARCHIVE_DIR.glob('*.json.gz')
        return
    yield from archive_layout.iter_source_files(source, ARCHIVE_DIR)


def audit_source(source: str) -> dict:
    """Scan a source's archive directory and return its stats.

    Discord 用独立归档格式：按日聚合 `messages` 计数，而非 news 条目。
    这里对 Discord 做特殊处理，把 messages 当作 total_items 用于活跃度判断。
    """
    result = {
        'source': source,
        'days_archived': 0,
        'total_items': 0,
        'last_archive_date': None,
        'first_archive_date': None,
    }
    files = sorted((f for f in _iter_archive_files(source)
                    if _DATE_STEM.match(archive_layout.date_stem(f))),
                   key=archive_layout.date_stem)
    if not files:
        return result

    total = 0
    for f in files:
        try:
            with archive_layout.open_archive_text(f) as fh:
                data = json.load(fh)
            if source == 'discord':
                # discord_archiver 输出 {messages: [...], ...}，list 长度 = 当日消息数
                msgs = data.get('messages', 0)
                total += len(msgs) if isinstance(msgs, list) else (msgs or 0)
            elif isinstance(data, list):
                # youtube_comments 等日快照为裸列表（无 item_count 包装）
                total += len(data)
            else:
                total += data.get('item_count', 0)
        except Exception as exc:
            # 本模块的职责就是「发现沉默」，自己却静默跳过读不动的档案：损坏档案会让
            # 条目数少算，反而可能把一个真已 degraded 的源判成健康。必须出声。
            print(f'  ! {source}: 跳过不可读归档 {f}: {type(exc).__name__}: {exc}', file=sys.stderr)
            continue

    # 区服分层后同一日期可有多文件（global/jp），归档天数按去重日期计
    result['days_archived'] = len({archive_layout.date_stem(f) for f in files})
    result['total_items'] = total
    result['first_archive_date'] = archive_layout.date_stem(files[0])
    result['last_archive_date'] = archive_layout.date_stem(files[-1])
    return result


def leaf_cadence_days(dates: list[str]) -> int | None:
    """近期归档节拍 = 最近 LEAF_CADENCE_WINDOW 个日期的相邻间隔中位数（天）。

    少于 2 个日期无法估节拍，返回 None（调用方回落 DORMANT 阈值）。
    """
    tail = sorted(set(dates))[-LEAF_CADENCE_WINDOW:]
    if len(tail) < 2:
        return None
    try:
        ds = [datetime.strptime(d, '%Y-%m-%d') for d in tail]
    except ValueError:
        return None
    # strict=False 是刻意的：相邻配对天然差一项，截断即预期行为
    gaps = sorted((b - a).days for a, b in itertools.pairwise(ds))
    return gaps[len(gaps) // 2]


def leaf_stall_threshold(cadence: int | None) -> int:
    """叶级断档告警阈值：日更叶 7 天即报，稀疏叶按 3 倍节拍放宽。"""
    if cadence is None:
        return DORMANT_THRESHOLD
    return max(DEGRADED_THRESHOLD, LEAF_CADENCE_FACTOR * cadence)


def audit_leaves(source: str) -> list[dict]:
    """某源的叶级（目录粒度）统计；单叶源返回空表（与平台级重复，无下钻价值）。"""
    by_dir: dict[Path, list[str]] = {}
    for f in _iter_archive_files(source):
        ds = archive_layout.date_stem(f)
        if _DATE_STEM.match(ds):
            by_dir.setdefault(f.parent, []).append(ds)
    if len(by_dir) <= 1:
        return []
    leaves = []
    for pdir, dates in sorted(by_dir.items()):
        uniq = sorted(set(dates))
        try:
            rel = str(pdir.relative_to(ARCHIVE_DIR))
        except ValueError:
            rel = str(pdir)
        leaves.append({
            'leaf': rel,
            'source': source,
            'days_archived': len(uniq),
            'first_archive_date': uniq[0],
            'last_archive_date': uniq[-1],
            'cadence_days': leaf_cadence_days(uniq),
        })
    return leaves


def build_leaf_report(today: str) -> list[dict]:
    """全部注册源的叶级下钻行（含 stalled 判定）。

    discord 例外：activity_daily 单目录，频道级健康由 archiver 的
    state.json 自理，不在本审计维度。
    """
    rows = []
    for source in ALL_REGISTERED_SOURCES:
        if source == 'discord':
            continue
        for leaf in audit_leaves(source):
            silent = compute_silent_days(leaf['last_archive_date'], today)
            threshold = leaf_stall_threshold(leaf['cadence_days'])
            leaf['silent_days'] = silent
            leaf['stall_threshold'] = threshold
            leaf['stalled'] = silent >= threshold
            rows.append(leaf)
    return rows


def print_leaf_report(rows: list[dict], show_all: bool = False) -> None:
    stalled = [r for r in rows if r['stalled']]
    shown = rows if show_all else stalled
    if not shown:
        if rows:
            print(f'【区服/类型叶级下钻】{len(rows)} 叶全部在各自节拍内，无断档。\n')
        return
    title = '全部叶' if show_all else '断档告警'
    print(f'【区服/类型叶级下钻（{title}）】({len(shown)}/{len(rows)} 叶)')
    for r in sorted(shown, key=lambda x: -x['silent_days']):
        cadence = f'节拍~{r["cadence_days"]}d' if r['cadence_days'] is not None else '节拍未知'
        flag = '⚠ ' if r['stalled'] else '  '
        print(f'  {flag}{r["leaf"]:32s}  {r["days_archived"]:4d}d  '
              f'last={r["last_archive_date"]:10s}  沉默 {r["silent_days"]}d  '
              f'（{cadence}，阈值 {r["stall_threshold"]}d）')
    print()


def compute_silent_days(last_date: str | None, today: str) -> int:
    if not last_date:
        return 9999
    try:
        last = datetime.strptime(last_date, '%Y-%m-%d')
        now = datetime.strptime(today, '%Y-%m-%d')
        return max(0, (now - last).days)
    except Exception:
        return 9999


def classify(silent_days: int, total_items: int) -> str:
    if total_items == 0:
        return 'never'
    if silent_days >= DORMANT_THRESHOLD:
        return 'dormant'
    if silent_days >= DEGRADED_THRESHOLD:
        return 'degraded'
    return 'active'


def build_report() -> dict:
    today = archive_layout.archive_date_str()  # 日期基准 SSOT（北京日期，与归档桶名同源）
    entries = []
    # Audit window 只看 platforms/ 下的非 discord 源；discord 有独立生命周期
    platform_first_dates = []
    for source in ALL_REGISTERED_SOURCES:
        stat = audit_source(source)
        stat['silent_days'] = compute_silent_days(stat['last_archive_date'], today)
        stat['level'] = classify(stat['silent_days'], stat['total_items'])
        entries.append(stat)
        if stat['first_archive_date'] and source != 'discord':
            platform_first_dates.append(stat['first_archive_date'])

    raw_start = min(platform_first_dates) if platform_first_dates else today
    # 内容日期老文（游戏公开前的关键词噪声）不代表采集窗口起点，钳到下限
    window_start = max(raw_start, AUDIT_WINDOW_FLOOR)
    try:
        window_days = (datetime.strptime(today, '%Y-%m-%d')
                       - datetime.strptime(window_start, '%Y-%m-%d')).days + 1
    except Exception:
        window_days = 0

    return {
        'today': today,
        'window_start': window_start,
        'window_clamped': raw_start < AUDIT_WINDOW_FLOOR,
        'window_days': window_days,
        'entries': entries,
        'leaves': build_leaf_report(today),
    }


def print_report(report: dict) -> None:
    entries = report['entries']
    print('\n=== 沉默源审计（基于归档历史）===')
    clamp_note = ('（已钳掉内容日期早于下限的老文长尾）'
                  if report.get('window_clamped') else '')
    print(f'审计窗口: {report["window_start"]} ~ {report["today"]}  '
          f'({report["window_days"]} 天){clamp_note}\n')

    by_level = {'active': [], 'degraded': [], 'dormant': [], 'never': []}
    for e in entries:
        by_level[e['level']].append(e)

    def print_section(title: str, rows: list[dict]) -> None:
        if not rows:
            return
        print(f'【{title}】({len(rows)})')
        rows_sorted = sorted(rows, key=lambda x: (-x['total_items'], x['silent_days']))
        for r in rows_sorted:
            last = r['last_archive_date'] or '从未'
            silent = r['silent_days']
            silent_str = '' if silent >= 9999 else f'沉默 {silent}d'
            print(f'  {r["source"]:18s}  {r["days_archived"]:4d}d  {r["total_items"]:6d} 条  '
                  f'last={last:10s}  {silent_str}')
        print()

    print_section('活跃（近 7 天内有产出）', by_level['active'])
    print_section('⚠  降级（7-30 天沉默）', by_level['degraded'])
    print_section('休眠（>30 天沉默）', by_level['dormant'])
    print_section(
        f'从未产出（审计窗口 {report["window_days"]}d 内 0 条）',
        by_level['never'],
    )

    summary = (
        f'合计 {len(entries)} 源 / '
        f'{len(by_level["active"])} 活跃 / '
        f'{len(by_level["degraded"])} 降级 / '
        f'{len(by_level["dormant"])} 休眠 / '
        f'{len(by_level["never"])} 从未产出'
    )
    print(summary)
    if report['window_days'] < DORMANT_THRESHOLD:
        print(f'⚠  归档窗口仅 {report["window_days"]}d < {DORMANT_THRESHOLD}d，'
              f'"从未产出"仅代表窗口内沉默，不等于真正 30 天 dormant。')
    print()


def write_health(report: dict) -> None:
    """Seed source-health.json 供 SilentPlatformTracker 使用。"""
    # 保留 tracker 写入的 last_check_date：丢失它会让 dormant 源的
    # 「每日一次探测」退化为每小时探测（每次 --write 重置检查记录）。
    existing = {}
    if HEALTH_PATH.exists():
        try:
            existing = json.loads(HEALTH_PATH.read_text(encoding='utf-8')).get('platforms', {})
        except Exception:
            existing = {}
    platforms = {}
    for e in report['entries']:
        # "never" 永远种子化为 active：archive 缺失常常是因为该源还没在产线跑过，
        # 直接标 dormant 会让 SilentPlatformTracker 跳过采集，形成
        # never→dormant→跳过→永远 never 的死循环。让 tracker 基于实时尝试
        # 结果（update_platform_status）自然降级到 degraded/dormant 才是正确路径。
        if e['level'] == 'never':
            level = 'active'
            silent = 0
        else:
            level = e['level']
            silent = e['silent_days'] if e['silent_days'] < 9999 else report['window_days']

        prev = existing.get(e['source'], {})
        entry = {
            'level': level,
            'last_success_date': e['last_archive_date'],
            'last_check_date': prev.get('last_check_date'),
            'consecutive_silent_days': silent,
            'total_items': e['total_items'],
            # errors 原先无条件重置为 []。本步骤在 update-news.yml 里紧跟 aggregator
            # 同一作业运行（第 92 行采集 → 第 107 行 --write），于是 tracker 本轮刚记下的
            # 采集异常当场被抹平——入库的 source-health.json 里每个平台恒为 errors: []，
            # 一个每小时都在抛异常的源与一个从未出过错的源在报表上一模一样。
            # 种子化只该补全归档侧的统计，不该销毁另一写方的错误台账。
            'errors': prev.get('errors', []),
        }
        # 降级原因标注（如「待配 NGA_COOKIE」）同理：由 tracker 写、恢复产出时由 tracker 清，
        # 种子化不是「恢复产出」，不得代它清除。
        if prev.get('note') and level != 'active':
            entry['note'] = prev['note']
        platforms[e['source']] = entry

    payload = {
        'updated_at': datetime.now(UTC).isoformat(),
        'seeded_from': 'silent_sources_audit',
        'audit_window': {
            'start': report['window_start'],
            'end': report['today'],
            'days': report['window_days'],
        },
        # P0-3：静默丢弃是一等指标——校验层扔掉的数据必须出现在健康报表里，
        # 不能只活在 CI 日志的 WARNING（taptap_review 曾因此静默 12 天）。
        'validation_drops': load_validation_drops(),
        # 叶级断档（区服/类型粒度）：平台活跃不代表区服活着（appstore/jp 曾
        # 骤停 30 天不可见），消费方据此下钻而无需翻目录。
        'stalled_leaves': [
            {k: leaf[k] for k in ('leaf', 'source', 'last_archive_date',
                                  'silent_days', 'cadence_days', 'stall_threshold')}
            for leaf in report.get('leaves', []) if leaf['stalled']
        ],
        'platforms': platforms,
    }
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print(f'写入 {HEALTH_PATH.relative_to(_REPO_ROOT)}')


def suggest_prune(report: dict) -> None:
    entries = report['entries']
    never = [e for e in entries if e['level'] == 'never']
    dormant = [e for e in entries if e['level'] == 'dormant']
    degraded = [e for e in entries if e['level'] == 'degraded']

    print('\n=== 清理建议 ===\n')
    if not (never or dormant or degraded):
        print('所有源近期都有产出，无需清理。')
        return

    if never:
        print(f' 候选摘除（窗口 {report["window_days"]}d 内从未产出，共 {len(never)}）:')
        for e in sorted(never, key=lambda x: x['source']):
            print(f'  - {e["source"]}')
        print()
    if dormant:
        print(f'⚠  强制调查（>30d 沉默，共 {len(dormant)}）:')
        for e in dormant:
            print(f'  - {e["source"]}  last={e["last_archive_date"]}  '
                  f'silent={e["silent_days"]}d')
        print()
    if degraded:
        print(f'轻度观察（7-30d 沉默，共 {len(degraded)}）:')
        for e in degraded:
            print(f'  - {e["source"]}  last={e["last_archive_date"]}  '
                  f'silent={e["silent_days"]}d')
        print()

    if report['window_days'] < DORMANT_THRESHOLD:
        print(f'提示：归档窗口仅 {report["window_days"]}d，建议累积 30 天后再做硬性摘除。'
              f'当前阶段可先通过 --write 将它们标记为 degraded，由 tracker 降频采集。')


def scan_unregistered_dirs() -> list[str]:
    """列出 data/platforms/ 下存在、但不在已注册源清单中的目录。

    捕获采集逻辑已移除却仍留有历史归档的遗留源（如 taptap_post），
    否则它们对沉默源审计完全不可见。
    """
    if not ARCHIVE_DIR.exists():
        return []
    registered = set(ALL_REGISTERED_SOURCES)
    found = []
    for d in sorted(ARCHIVE_DIR.iterdir()):
        if d.is_dir() and d.name not in registered:
            found.append(d.name)
    return found


def print_legacy_section(unregistered: list[str]) -> None:
    if not unregistered:
        return
    print('【遗留源（有归档但未注册采集）】')
    for name in unregistered:
        stat = audit_source(name)
        tag = '已知遗留' if name in LEGACY_SOURCES else '未登记'
        last = stat['last_archive_date'] or '从未'
        print(f'  {name:18s}  {stat["days_archived"]:4d}d  {stat["total_items"]:6d} 条  '
              f'last={last:10s}  [{tag}]')
    print()


def core_source_alarms(report: dict) -> list[str]:
    """返回处于 never/dormant 的核心源名（健康门控用）。"""
    alarmed = []
    for e in report['entries']:
        if e['source'] in CORE_SOURCES and e['level'] in ('never', 'dormant'):
            alarmed.append(e['source'])
    return alarmed


# drops 文件视为「本轮」的最大年龄：管线每小时跑、审计紧随 aggregator，
# 超过 2 小时即判定为崩溃轮残留（aggregator 未走到 flush），按零值处理，
# 防止旧计数被当作本轮重复告警（2026-07-02 验证编队 minor）。
DROPS_MAX_AGE_HOURS = 2


def load_validation_drops() -> dict:
    """读 aggregator 落盘的本轮校验丢弃计数；缺失或过期（崩溃轮残留）返回零值形态。"""
    if DROPS_PATH.exists():
        try:
            payload = json.loads(DROPS_PATH.read_text(encoding='utf-8'))
            gen = payload.get('generated_at')
            if gen:
                age = datetime.now(UTC) - datetime.fromisoformat(gen)
                if age > timedelta(hours=DROPS_MAX_AGE_HOURS):
                    return {'generated_at': gen, 'total_dropped': 0, 'by_source': {},
                            'stale_ignored': True}
            return payload
        except Exception as exc:
            # 丢弃台账读不动即等于「本轮零丢弃」——门控（P0-3）会因此放行一次
            # 实际大量丢数的采集轮次。回落值保持不变，但不再无声无息。
            print(f'  ! validation_drops 读取失败（按零丢弃回落）: {type(exc).__name__}: {exc}',
                  file=sys.stderr)
    return {'generated_at': None, 'total_dropped': 0, 'by_source': {}}


def drop_alarms() -> list[str]:
    """单轮丢弃超阈值的源清单（P0-3 门控判据）。"""
    drops = load_validation_drops()
    return [f'{src}({n})' for src, n in sorted(drops.get('by_source', {}).items())
            if n >= DROPS_ALARM_THRESHOLD]


def main() -> None:
    parser = argparse.ArgumentParser(description='沉默源审计（基于归档历史）')
    parser.add_argument('--write', action='store_true',
                        help='写入 output/source-health.json，供 SilentPlatformTracker 种子')
    parser.add_argument('--suggest-prune', action='store_true',
                        help='输出建议清理列表')
    parser.add_argument('--strict', action='store_true',
                        help='核心源处于 never/dormant 时以非零退出（健康门控）')
    parser.add_argument('--leaves', action='store_true',
                        help='叶级下钻显示全部区服/类型叶（默认只列断档告警叶）')
    args = parser.parse_args()

    report = build_report()
    print_report(report)
    print_leaf_report(report['leaves'], show_all=args.leaves)
    print_legacy_section(scan_unregistered_dirs())

    if args.suggest_prune:
        suggest_prune(report)
    if args.write:
        write_health(report)

    alarmed = core_source_alarms(report)
    if alarmed:
        print(f'[健康门控] 核心源零产出 / 长期沉默: {", ".join(alarmed)}')
    dropped = drop_alarms()
    if dropped:
        print(f'[健康门控] 校验层单轮丢弃超阈值（≥{DROPS_ALARM_THRESHOLD} 条/源）: '
              f'{", ".join(dropped)} —— 采集在产出但被校验扔掉，查 VALID_SOURCES/'
              f'必填字段契约是否漂移')
    if (alarmed or dropped) and args.strict:
        sys.exit(1)


if __name__ == '__main__':
    main()
