#!/usr/bin/env python3
"""
通用归档引擎 — 声明式来源注册表驱动，打包冷数据 → GitHub Releases → 可选从 git 删除

设计目标（守密人 2026-06-21 裁定 A + 合并）：
  - 把「每种归档来源的差异」抽进 archive_sources.json 配置表，引擎读表干活。
  - 加新归档来源 = 在注册表加一段配置，零新代码。
  - 收编原 archive_discord.py（迁进引擎，标签命名与删数据路径逐行等价）。

用法:
  python archive_engine.py --source discord              # 归档单个来源（按其 cutoff 策略）
  python archive_engine.py --source all                  # 归档注册表全部来源
  python archive_engine.py --source discord --dry-run    # 仅分析，不做任何修改
  python archive_engine.py --source discord --skip-upload
  python archive_engine.py --source discord --force-group 2026-01 [--force-group ...]

注册表: projects/news/scripts/archive_sources.json
每来源日志: <base_dir>/archive-log.json（含 source/tag 字段，向后兼容旧 Discord 日志）
统一索引: projects/news/data/releases-index.json（自动生成，治「Release 好难认」）

来源配置字段: base_dir(来源根目录) / glob(文件匹配，单模式或模式列表——冷热分层来源
须同时列 `*.jsonl` 与 `*.jsonl.gz`) / group_by(分桶:
month_from_stem 按文件名 YYYY-MM-DD 取 YYYY-MM | month_from_parent_dir 按父目录名
YYYY-MM-DD 取 YYYY-MM（日期在目录名，如 fanart）| single) / group_label(single 桶名) /
cutoff_days(仅归档早于 N 天; null=不限龄) / after_archive(git_rm|keep) /
clean_empty_dirs(归档后清理空目录)。
上传两选一：
  - 滚动单 release（推荐）: release_tag(固定 Release 标签) + asset_template(每桶资产文件名,
    占位 {group}) + release_title/release_notes(Release 级文案)。所有桶归入同一 release，
    每桶一资产，--clobber 仅替换本桶不动其它桶。
  - 旧版每桶一 tag（向后兼容）: tag_template/title_template/notes_template
    (占位 {group}/{filename}/{size_kb}/{files})。
"""

import argparse
import json
import logging
import os
import subprocess
import tarfile
from collections import defaultdict
from datetime import datetime, timedelta, UTC
from pathlib import Path

import archive_layout  # 分仓桥接：社区数据根 SSOT（同目录）

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
REGISTRY_PATH = Path(__file__).resolve().parent / 'archive_sources.json'
RELEASES_INDEX = REPO_ROOT / 'projects' / 'news' / 'data' / 'releases-index.json'


def _resolve_base_dir(base_dir_rel: str) -> Path:
    """来源根解析（分仓桥接）：数据湖内来源（`Public-Info-Pool/...`）经 archive_layout
    数据根解析（env BIAV_SC_DATA_ROOT 或在树默认）；code 仓内来源（如 fanart staging
    `projects/news/data/fanart`）仍相对 REPO_ROOT。env 未设时与旧 `REPO_ROOT / base_dir`
    逐字节相等（行为中性）。"""
    prefix = 'Public-Info-Pool/'
    if base_dir_rel.startswith(prefix):
        return archive_layout.pool_root() / base_dir_rel[len(prefix):]
    return REPO_ROOT / base_dir_rel


# ---------- 注册表 ----------

def load_registry() -> dict:
    with open(REGISTRY_PATH, encoding='utf-8') as f:
        return json.load(f)


# ---------- 分桶策略 ----------

def group_of(path: Path, group_by: str, group_label: str) -> str:
    """文件归属哪个桶。"""
    if group_by == 'month_from_stem':
        # 日期茎经布局 SSOT：冷层 `2026-04-01.jsonl.gz` 的 Path.stem 残留 '.jsonl'
        return archive_layout.date_stem(path)[:7]  # YYYY-MM-DD -> YYYY-MM
    if group_by == 'month_from_parent_dir':
        return path.parent.name[:7]  # 父目录 YYYY-MM-DD -> YYYY-MM（日期在目录名，如 fanart）
    if group_by == 'single':
        return group_label
    raise ValueError(f'unknown group_by: {group_by}')


def is_eligible(path: Path, group_by: str, cutoff_date: str | None) -> bool:
    """文件是否够龄可归档。cutoff_date 为 None 表示不限龄。"""
    if cutoff_date is None:
        return True
    if group_by == 'month_from_stem':
        # date_stem 对裸 .jsonl 与 Path.stem 逐字节等价，另把冷层 .gz 的双后缀剥干净
        return archive_layout.date_stem(path) < cutoff_date
    if group_by == 'month_from_parent_dir':
        return path.parent.name < cutoff_date  # 父目录 YYYY-MM-DD 逐字节比较
    # 无日期语义的来源不应配 cutoff_days；保守判为可归档
    return True


def _iter_candidates(cfg: dict, base_dir: Path):
    """按注册表 glob 产出候选文件。glob 可为单模式或模式列表（冷热双扩展名）。

    冷热分层后 dated 归档一半是 `.jsonl.gz`：单一 `*.jsonl` 模式把整个冷层扫不到,
    而 cutoff_days 恰恰只放行冷层年龄的文件——两者相乘等于「一个文件都不归档」。
    """
    patterns = cfg['glob']
    if isinstance(patterns, str):
        patterns = [patterns]
    seen: set[Path] = set()
    for pattern in patterns:
        for f in base_dir.glob(pattern):
            if f not in seen:
                seen.add(f)
                yield f


def discover(cfg: dict, base_dir: Path, force_groups: list[str]) -> dict[str, list[Path]]:
    """扫描来源，返回 {group: [Path]}（按 group 排序）。"""
    group_by = cfg['group_by']
    group_label = cfg.get('group_label', 'all')
    groups: dict[str, list[Path]] = defaultdict(list)
    if not base_dir.exists():
        return {}

    if force_groups:
        wanted = set(force_groups)
        for f in _iter_candidates(cfg, base_dir):
            if not f.is_file():  # glob 可能命中目录（如 fanart 的 thumbs/），只归档文件
                continue
            g = group_of(f, group_by, group_label)
            if g in wanted:
                groups[g].append(f)
    else:
        cutoff_date = None
        cutoff_days = cfg.get('cutoff_days')
        if cutoff_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=cutoff_days)
            cutoff_date = cutoff.strftime('%Y-%m-%d')
            logger.info(f'Cutoff date: {cutoff_date} ({cutoff_days} days ago)')
        for f in _iter_candidates(cfg, base_dir):
            if not f.is_file():  # glob 可能命中目录（如 fanart 的 thumbs/），只归档文件
                continue
            if is_eligible(f, group_by, cutoff_date):
                groups[group_of(f, group_by, group_label)].append(f)
    return dict(sorted(groups.items()))


# ---------- 打包 / 上传 / 删除 ----------

def asset_name_of(cfg: dict, group: str) -> str:
    """归档资产文件名。滚动模式用 asset_template；旧模式沿用 {tag}.tar.gz（向后兼容）。"""
    if cfg.get('asset_template'):
        return cfg['asset_template'].format(group=group)
    return f"{cfg['tag_template'].format(group=group)}.tar.gz"


def create_tarball(cfg: dict, base_dir: Path, group: str, files: list[Path]) -> tuple[Path, int]:
    """打 tar.gz；arcname 相对 base_dir（与原 archive_discord 等价）。"""
    archive_path = base_dir / asset_name_of(cfg, group)
    with tarfile.open(archive_path, 'w:gz') as tar:
        for f in sorted(files):
            tar.add(f, arcname=str(f.relative_to(base_dir)))
    size = archive_path.stat().st_size
    logger.info(f'Created {archive_path.name}: {len(files)} files, {size // 1024} KB')
    return archive_path, size


def upload_to_release(cfg: dict, archive_path: Path, group: str, file_count: int) -> bool:
    """经 gh CLI 上传到 GitHub Releases。

    两种模式：
    - 滚动单 release（配 release_tag）：所有桶归入同一个 release，每桶一资产文件；
      上传用 `--clobber` 仅替换本桶资产，不动同 release 内其它桶（修「每月一 tag」散乱）。
    - 旧版每桶一 tag（配 tag_template）：幂等先删同名 release/tag 再 create（向后兼容）。
    """
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if not repo:
        logger.error('GITHUB_REPOSITORY not set, cannot upload')
        return False

    release_tag = cfg.get('release_tag')
    if release_tag:
        # 滚动模式：确保 release 存在，再以 --clobber 追加/替换本桶单资产。
        exists = subprocess.run(
            ['gh', 'release', 'view', release_tag, '--repo', repo],
            cwd=REPO_ROOT, capture_output=True,
        ).returncode == 0
        if not exists:
            title = cfg.get('release_title', release_tag)
            notes = cfg.get(
                'release_notes',
                'Rolling archive release. Each asset is one bucket; auto-managed by archive_engine.py.',
            )
            created = subprocess.run([
                'gh', 'release', 'create', release_tag,
                '--title', title, '--notes', notes, '--repo', repo,
            ], cwd=REPO_ROOT, capture_output=True, text=True)
            if created.returncode != 0:
                logger.error(f'Release create failed: {created.stderr}')
                return False
        uploaded = subprocess.run([
            'gh', 'release', 'upload', release_tag, str(archive_path),
            '--clobber', '--repo', repo,
        ], cwd=REPO_ROOT, capture_output=True, text=True)
        if uploaded.returncode == 0:
            logger.info(f'Uploaded asset {archive_path.name} to rolling release {release_tag}')
            return True
        logger.error(f'Asset upload failed: {uploaded.stderr}')
        return False

    # 旧版每桶一 tag（向后兼容）
    tag = cfg['tag_template'].format(group=group)
    size_kb = archive_path.stat().st_size // 1024
    title = cfg['title_template'].format(group=group)
    notes = cfg['notes_template'].format(
        group=group, filename=archive_path.name, size_kb=size_kb, files=file_count,
    )

    subprocess.run(
        ['gh', 'release', 'delete', tag, '--yes', '--cleanup-tag'],
        cwd=REPO_ROOT, capture_output=True,
    )
    result = subprocess.run([
        'gh', 'release', 'create', tag, str(archive_path),
        '--title', title, '--notes', notes, '--repo', repo,
    ], cwd=REPO_ROOT, capture_output=True, text=True)

    if result.returncode == 0:
        logger.info(f'Uploaded to GitHub Releases: {tag}')
        return True
    logger.error(f'Release upload failed: {result.stderr}')
    return False


def git_rm_files(files: list[Path]) -> int:
    """从 git 删除文件（未跟踪则直接 unlink）。返回删除计数。"""
    removed = 0
    for f in files:
        try:
            subprocess.run(
                ['git', 'rm', '-f', '--quiet', str(f)],
                cwd=REPO_ROOT, check=True, capture_output=True,
            )
            removed += 1
        except subprocess.CalledProcessError:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def clean_empty_dirs(base_dir: Path, files: list[Path]):
    """归档后清理变空的父目录（仅 rmdir 空目录，限 base_dir 内）。"""
    parents = {f.parent for f in files}
    for d in parents:
        try:
            if d != base_dir and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


# ---------- 日志 / 索引 ----------

def load_log(log_path: Path) -> list[dict]:
    if log_path.exists():
        try:
            with open(log_path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return []


def save_log(log_path: Path, log: list[dict]):
    # 原子替换（同目录临时文件 + os.replace，与 community_cold_compress /
    # discord_reconcile 同一手法；本模块刻意只吃标准库——collect-fanart /
    # recover-fanart 两个工作流不装 requirements，引 news_common 会当场 import 失败）。
    # 这份日志是「哪一桶已经传上 Releases」的**唯一**记录。直写若在中途被杀就留下半截 JSON，而
    # load_log 对 JSONDecodeError 一律 `return []`——整份归档史当场归零：
    # 下一轮成功归档只写回本轮那几桶，rebuild_releases_index 据此重建的
    # releases-index.json 就把此前所有月桶条目一并抹掉（藏宝图上的坑全没了,
    # 而 Release 里的资产还在，只是没人再指得到），且 discord 归档器的
    # 「该月已归档 → 跳过重抓」守卫同时失效。
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = log_path.with_name(log_path.name + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        os.replace(tmp, log_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def rebuild_releases_index(registry: dict):
    """汇总各来源 archive-log 成统一 releases-index.json（治「好难认」）。"""
    index = []
    for source_id, cfg in registry.items():
        base_dir = _resolve_base_dir(cfg['base_dir'])
        log = load_log(base_dir / 'archive-log.json')
        for entry in log:
            group = entry.get('group') or entry.get('month', '')
            default_tag = cfg.get('release_tag') or (
                cfg['tag_template'].format(group=group) if cfg.get('tag_template') else group
            )
            index.append({
                'source': entry.get('source', source_id),
                'group': group,
                'tag': entry.get('tag') or default_tag,
                'asset': entry.get('asset'),
                'files': entry.get('files'),
                'archive_size_bytes': entry.get('archive_size_bytes'),
                'uploaded_to_releases': entry.get('uploaded_to_releases'),
                'archived_at': entry.get('archived_at'),
            })
    index.sort(key=lambda e: (e['source'], e['group']))
    RELEASES_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with open(RELEASES_INDEX, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    logger.info(f'releases-index.json: {len(index)} entries')


# ---------- 主流程 ----------

def archive_source(source_id: str, cfg: dict, args) -> None:
    logger.info(f'==== source: {source_id} ====')
    base_dir = _resolve_base_dir(cfg['base_dir'])
    by_group = discover(cfg, base_dir, args.force_group)
    if not by_group:
        logger.info(f'[{source_id}] nothing to archive')
        return

    total_files = sum(len(fs) for fs in by_group.values())
    total_size = sum(f.stat().st_size for fs in by_group.values() for f in fs)
    logger.info(f'[{source_id}] {total_files} files across {len(by_group)} groups ({total_size // 1048576} MB)')
    for group, files in by_group.items():
        logger.info(f'  {group}: {len(files)} files, {sum(f.stat().st_size for f in files) // 1024} KB')

    if args.dry_run:
        logger.info('DRY RUN — no changes made')
        return

    log_path = base_dir / 'archive-log.json'
    log = load_log(log_path)

    # 收尾那行 `complete: N groups, M files` 原先直接打 discover 的总数——而下面的循环
    # 在上传失败时 `continue`，那一桶既没上传也没落日志。上传失败（gh 未登录 / release
    # create 403 / 网络断）时，日志照旧宣布「complete: 3 groups, 500 files」，
    # 而实际归档 0 桶：报告说做完了，状态是一件没做。故只计真正走完的桶。
    archived_groups = 0
    archived_files = 0
    failed_groups: list[str] = []

    for group, files in by_group.items():
        logger.info(f'--- archiving [{source_id}] {group} ---')
        archive_path, archive_size = create_tarball(cfg, base_dir, group, files)

        uploaded = False
        if not args.skip_upload:
            uploaded = upload_to_release(cfg, archive_path, group, len(files))
            if not uploaded:
                logger.error(f'Upload failed for {group}, keeping files')
                archive_path.unlink(missing_ok=True)
                failed_groups.append(group)
                continue

        entry = {
            'source': source_id,
            'group': group,
            'tag': cfg.get('release_tag') or cfg['tag_template'].format(group=group),
            'asset': archive_path.name,
            'files': len(files),
            'archive_size_bytes': archive_size,
            'uploaded_to_releases': uploaded,
            'archived_at': datetime.now(UTC).isoformat(),
        }
        # 每桶一条：**替换**同 (source, group) 旧条目，不追加。上传本身是 --clobber
        # 幂等的，日志却是纯 append——而重跑是常态：`after_archive: keep` 的来源
        # （discord）文件归档后原地留存，下一轮 cutoff 又把同一批月桶全部重新发现；
        # fanart 的 recover 工作流也按月 `--force-group` 重传。追加式日志于是让
        # archive-log.json 每轮为每个桶再长一条，rebuild_releases_index 逐条产出索引行
        # → releases-index.json 里同一个月桶出现 N 份（files/size 各不相同），
        # 「统一索引治 Release 好难认」反而变成认不出哪份是现行的。
        for i, prev in enumerate(log):
            if (prev.get('source', source_id) == source_id
                    and (prev.get('group') or prev.get('month')) == group):
                log[i] = entry
                break
        else:
            log.append(entry)
        # 先记账，再删源文件。原顺序是 git_rm → 删本地 tarball → 写日志：上传
        # 已经成功、源文件也已 git_rm 掉，进程若在这之后、写日志之前被杀
        # （monthly-cleanup 工作流跑几百 MB tar，被杀是真会发生的），资产躺在
        # Release 里，本地源没了，而日志里没有这一桶——重跑时 discover 已经找不到
        # 任何文件，这条记录**永远补不回来**：releases-index.json 从此漏掉一个
        # 真实存在的月桶（藏宝图缺了一个坑），discord 归档器的「已归档 → 跳过重抓」
        # 也认不出它。日志先落盘则最坏只是多留一份源文件，下轮重删即可（幂等）。
        save_log(log_path, log)

        if cfg.get('after_archive') == 'git_rm':
            removed = git_rm_files(files)
            logger.info(f'Removed {removed} files from git for {group}')

        if uploaded:
            archive_path.unlink(missing_ok=True)

        archived_groups += 1
        archived_files += len(files)

    if cfg.get('clean_empty_dirs'):
        clean_empty_dirs(base_dir, [f for fs in by_group.values() for f in fs])
    logger.info(
        f'[{source_id}] complete: {archived_groups}/{len(by_group)} groups, '
        f'{archived_files}/{total_files} files'
    )
    if failed_groups:
        logger.error(
            f'[{source_id}] {len(failed_groups)} group(s) NOT archived (upload failed): '
            f'{", ".join(failed_groups)}'
        )


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description='Declarative archive engine → GitHub Releases')
    parser.add_argument('--source', required=True, help="source id from registry, or 'all'")
    parser.add_argument('--dry-run', action='store_true', help='Analyze only, no changes')
    parser.add_argument('--skip-upload', action='store_true', help='Skip GitHub Releases upload')
    parser.add_argument(
        '--force-group', action='append', default=[], metavar='GROUP',
        help='Force-archive specific group(s), bypassing cutoff. Repeatable.',
    )
    args = parser.parse_args(argv)

    registry = load_registry()
    if args.source == 'all':
        targets = list(registry.items())
    elif args.source in registry:
        targets = [(args.source, registry[args.source])]
    else:
        logger.error(f'unknown source: {args.source} (known: {", ".join(registry)})')
        return

    for source_id, cfg in targets:
        archive_source(source_id, cfg, args)
    if not args.dry_run:
        rebuild_releases_index(registry)


if __name__ == '__main__':
    main()
