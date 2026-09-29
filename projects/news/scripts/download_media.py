#!/usr/bin/env python3
"""
download_media.py — 全平台媒体资源下载器

扫描 news.json 中带 media_url 的条目，下载图片/视频封面到本地。
当本地缓存超过阈值时，打包上传到 GitHub Release。

用法:
  python projects/news/scripts/download_media.py
  python projects/news/scripts/download_media.py --archive   # 超阈值时打包归档
"""

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # SSRF 守卫 + safe_get 单一真源（SEC-02 / R2-H2 / R2-M1）
import archive_layout  # 运行期工作根单一真相源

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
NEWS_JSON = archive_layout.news_run_root() / 'news.json'  # 运行期中间态（不进 git）
MEDIA_DIR = _REPO_ROOT / 'projects' / 'news' / 'data' / 'media'
MANIFEST_PATH = MEDIA_DIR / 'manifest.json'

# Max total size before archiving to GitHub Release (100 MB)
ARCHIVE_THRESHOLD_MB = 100
MAX_FILE_SIZE_MB = 20  # Skip files larger than this
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.3  # seconds between downloads


def url_to_filename(url: str, source: str) -> str:
    """Generate a stable filename from URL: {source}_{hash}.{ext}"""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    parsed = urlparse(url)
    path = parsed.path
    # Extract extension from URL path
    ext_match = re.search(r'\.(jpg|jpeg|png|gif|webp|avif|mp4|webm|svg)(\?|$)', path, re.IGNORECASE)
    ext = ext_match.group(1).lower() if ext_match else 'jpg'
    return f'{source}_{url_hash}.{ext}'


def load_manifest() -> dict:
    """Load download manifest (tracks what's been downloaded)."""
    if MANIFEST_PATH.exists():
        try:
            with open(MANIFEST_PATH, encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {'downloaded': {}, 'failed': {}, 'archived': []}


def save_manifest(manifest: dict):
    """Save download manifest."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def download_file(url: str, dest: Path) -> bool:
    """Download a file to dest. Returns True on success.

    经 news_common.safe_get：禁用自动重定向、手动逐跳重校验 SSRF 守卫并 pin IP
    （R2-M1），同时正确跟随 30x（B站/YouTube/Pixiv 等 CDN 普遍以 30x 下发资源，
    R2-H2 回归修复）。3xx 缺/坏 Location 或不安全跳 → 抛 ValueError，不落盘垃圾。
    """
    try:
        resp = news_common.safe_get(url, timeout=REQUEST_TIMEOUT, stream=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': urlparse(url).scheme + '://' + urlparse(url).netloc + '/',
        })
        resp.raise_for_status()

        # Check content length
        content_length = int(resp.headers.get('Content-Length', 0))
        if content_length > MAX_FILE_SIZE_MB * 1024 * 1024:
            logger.warning(f'跳过过大文件 ({content_length / 1024 / 1024:.1f}MB): {url[:80]}')
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with open(dest, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                total += len(chunk)
                if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                    logger.warning(f'文件下载中超限 ({total / 1024 / 1024:.1f}MB)，停止: {url[:80]}')
                    dest.unlink(missing_ok=True)
                    return False

        logger.info(f'  {dest.name} ({total / 1024:.0f} KB)')
        return True
    except ValueError as e:
        # safe_get 拒绝（不安全 URL / 坏重定向）——不落盘垃圾文件
        logger.warning(f'  拒绝不安全 URL: {e}')
        dest.unlink(missing_ok=True)
        return False
    except requests.RequestException as e:
        logger.warning(f'  下载失败: {e}')
        dest.unlink(missing_ok=True)
        return False


def collect_media_urls() -> list[dict]:
    """Collect all items with media_url from news.json."""
    if not NEWS_JSON.exists():
        logger.warning(f'news.json 不存在: {NEWS_JSON}')
        return []
    try:
        with open(NEWS_JSON, encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # exception() 保留 traceback：JSONDecodeError 只印一句消息时看不出出错位置
        logger.exception('读取 news.json 失败')
        return []

    items = []
    for item in data.get('news', []):
        media_url = (item.get('media_url') or '').strip()
        if not media_url:
            continue
        items.append({
            'url': media_url,
            'source': item.get('source', 'unknown'),
            'title': item.get('title', '')[:100],
            'content_type': item.get('content_type', 'image'),
            'time': item.get('time', ''),
        })
    return items


def download_new_media(items: list[dict], manifest: dict, max_downloads: int = 300) -> int:
    """Download media files not yet in manifest. Returns count of new downloads."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    downloaded = manifest.get('downloaded', {})
    failed = manifest.get('failed', {})

    new_count = 0
    skip_count = 0

    for item in items:
        url = item['url']
        if url in downloaded or url in failed:
            skip_count += 1
            continue
        if new_count >= max_downloads:
            logger.info(f'已达本次下载上限 ({max_downloads})')
            break

        filename = url_to_filename(url, item['source'])
        dest = MEDIA_DIR / filename

        if dest.exists():
            # Already downloaded but not in manifest
            downloaded[url] = {
                'filename': filename,
                'source': item['source'],
                'title': item['title'],
                'time': item['time'],
                'downloaded_at': datetime.now(UTC).isoformat(),
            }
            new_count += 1
            continue

        time.sleep(REQUEST_DELAY)
        if download_file(url, dest):
            downloaded[url] = {
                'filename': filename,
                'source': item['source'],
                'title': item['title'],
                'time': item['time'],
                'downloaded_at': datetime.now(UTC).isoformat(),
            }
            new_count += 1
        else:
            failed[url] = {
                'source': item['source'],
                'reason': 'download_failed',
                'attempted_at': datetime.now(UTC).isoformat(),
            }

    manifest['downloaded'] = downloaded
    manifest['failed'] = failed
    logger.info(f'媒体下载完成: 新增 {new_count}, 跳过 {skip_count}, 失败记录 {len(failed)}')
    return new_count


def get_media_dir_size_mb() -> float:
    """Calculate total size of media directory in MB."""
    total = 0
    if MEDIA_DIR.exists():
        for f in MEDIA_DIR.iterdir():
            if f.is_file() and f.name != 'manifest.json':
                total += f.stat().st_size
    return total / (1024 * 1024)


def archive_to_release(manifest: dict):
    """
    Archive media files to a GitHub Release when directory exceeds threshold.
    Uses `gh` CLI if available, otherwise creates a tar.gz for manual upload.
    """
    size_mb = get_media_dir_size_mb()
    if size_mb < ARCHIVE_THRESHOLD_MB:
        logger.info(f'媒体目录 {size_mb:.1f}MB < {ARCHIVE_THRESHOLD_MB}MB 阈值，无需归档')
        return

    logger.info(f'媒体目录 {size_mb:.1f}MB >= {ARCHIVE_THRESHOLD_MB}MB，开始归档...')

    today = datetime.now(UTC).strftime('%Y-%m-%d')
    archive_name = f'media-archive-{today}.tar.gz'
    archive_path = MEDIA_DIR.parent / archive_name

    # Create tar.gz of media files (excluding manifest)
    media_files = [f for f in MEDIA_DIR.iterdir()
                   if f.is_file() and f.name != 'manifest.json']

    if not media_files:
        logger.info('没有文件需要归档')
        return

    # Create archive
    import tarfile
    with tarfile.open(archive_path, 'w:gz') as tar:
        for f in media_files:
            tar.add(f, arcname=f'media/{f.name}')
    logger.info(f'已创建归档: {archive_name} ({archive_path.stat().st_size / 1024 / 1024:.1f}MB)')

    # Try to upload via gh CLI
    try:
        tag = f'media-{today}'
        # create-or-clobber（与 archive_engine.upload_to_release 同一纪律）：裸 create
        # 对已存在的同名 tag 必然报「release already exists」而整块落进下面的 except
        # ——本地文件于是永远不被清理，媒体目录只涨不落，当天此后每一次运行都撞同一堵墙。
        # 触发不需要什么奇景：上传成功后、unlink 循环前进程被杀（作业超时 / runner 回收）
        # 就已经是这个局面，光重跑修不回来。create 失败即退回 upload --clobber 覆盖同名资产。
        try:
            subprocess.run(
                ['gh', 'release', 'create', tag, str(archive_path),
                 '--title', f'Media Archive {today}',
                 '--notes', f'Auto-archived {len(media_files)} media files ({size_mb:.0f}MB)'],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError:
            subprocess.run(
                ['gh', 'release', 'upload', tag, str(archive_path), '--clobber'],
                check=True, capture_output=True, text=True,
            )
        logger.info(f'已上传到 GitHub Release: {tag}')

        # Clean up local files after successful upload
        for f in media_files:
            f.unlink(missing_ok=True)
        archive_path.unlink(missing_ok=True)

        # Update manifest —— 每个 tag 一条：同日二次归档（--clobber 覆盖同名资产）
        # 若照旧 append，台账里会出现两条同 tag 记录，读者无从判断哪条对应现存资产。
        record = {
            'tag': tag,
            'date': today,
            'files': len(media_files),
            'size_mb': round(size_mb, 1),
        }
        archived = manifest.setdefault('archived', [])
        for i, prev in enumerate(archived):
            if prev.get('tag') == tag:
                archived[i] = record
                break
        else:
            archived.append(record)
        logger.info(f'已清理 {len(media_files)} 个本地文件')
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f'gh CLI 上传失败 ({e})，归档文件保留在: {archive_path}')
        logger.info('请手动上传: gh release create <tag> <archive_path>')


def main():
    parser = argparse.ArgumentParser(description='下载全平台媒体资源')
    parser.add_argument('--max-downloads', type=int, default=300,
                        help='单次最多下载数（默认 300）')
    parser.add_argument('--archive', action='store_true',
                        help='超阈值时打包归档到 GitHub Release')
    args = parser.parse_args()

    logger.info('=== 全平台媒体下载开始 ===')

    # Collect URLs from news.json
    items = collect_media_urls()
    logger.info(f'发现 {len(items)} 条带媒体的条目')

    if not items:
        logger.info('没有需要下载的媒体')
        return

    # Load manifest & download
    manifest = load_manifest()
    new_count = download_new_media(items, manifest, max_downloads=args.max_downloads)
    save_manifest(manifest)

    # Stats
    size_mb = get_media_dir_size_mb()
    logger.info(f'媒体目录当前大小: {size_mb:.1f}MB')

    # Archive if requested and over threshold
    if args.archive:
        archive_to_release(manifest)
        save_manifest(manifest)

    # Source breakdown
    sources = {}
    for info in manifest.get('downloaded', {}).values():
        src = info.get('source', 'unknown')
        sources[src] = sources.get(src, 0) + 1
    if sources:
        logger.info('=== 媒体来源统计 ===')
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            logger.info(f'  {src}: {count}')

    logger.info(f'=== 媒体下载完成: 本次新增 {new_count} ===')


if __name__ == '__main__':
    main()
