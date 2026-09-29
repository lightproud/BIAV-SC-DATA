#!/usr/bin/env python3
"""媒体补录器 — 扫全量归档，把仍存活的图片全部下载归档，二进制走 Releases（决策 038/059）。

关键认知（守密人 2026-06-02 指正）：归档丢的只是带签名的 CDN 链接，**附件本体仍在 Discord**。
Discord 附件 URL 的过期只在查询参数（ex/is/hm）；用 Bot Token 调
`POST /attachments/refresh-urls` 可把过期链接换成新签名链接，再下载。所以 Discord 图几乎
全部可补，不是只有最近 1 天。持久平台源（YouTube/Bilibili/Reddit/Pixiv）带 Referer 直接下。

流程：
  1. 扫 platforms/*/*.json 的 media_url + discord/channels/*/*.jsonl 的 image 附件（全历史）；
  2. Discord 过期 URL 经 refresh-urls 批量刷新（需 DISCORD_BOT_TOKEN，bot 须能访问该频道）；
  3. 按源带正确 Referer 下载到 media/files/（gitignore，不进 git）；
  4. 写 <pool>/Record/media/backfill_manifest.json（文本台账，随数据仓进 git；2026-07-30 迁数据湖）；
  5. --upload：打包 media/files/ 传 GitHub Releases（tag=community-assets「社区二创」，需 GH_TOKEN）。

清单已 ok 的跳过（可续跑）；--budget 秒级预算，超时优雅退出。

用法：
  DISCORD_BOT_TOKEN=xxx python projects/news/scripts/backfill_media.py        # 全量补录
  python projects/news/scripts/backfill_media.py --no-discord                 # 只补持久平台源
  python projects/news/scripts/backfill_media.py --upload                     # 打包传 Releases
"""
import os
import sys
import json
import re
import glob
import time
import argparse
import subprocess
import hashlib
import html
import urllib.request
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # SSRF 守卫 + safe_get 单一真源（R2-H1：补齐姊妹下载路径）
import archive_layout  # 归档布局单一真相源（分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认）
import requests

ROOT = "projects/news/data"            # media 输出（二进制，gitignore，留原地）
SRC = str(archive_layout.community_root())  # 源读取根（分仓桥接：随 community_root() 换位 data 仓 / 在树）
FILES = f"{ROOT}/media/files"
# manifest 随数据湖走（守密人 2026-07-30 裁定）：<pool>/Record/media/backfill_manifest.json。
# 原 code 仓路径被 .gitignore 整目录忽略（#876）致 CI 落档步骤连败 38 天。
MANIFEST = str(archive_layout.media_manifest_path())
_LEGACY_MANIFEST = f"{ROOT}/media/backfill_manifest.json"  # 迁移前旧址，只读回退
UA = "Mozilla/5.0 (silver-core media backfill)"
REFERER = {"pixiv": "https://www.pixiv.net/", "bilibili": "https://www.bilibili.com/"}
RELEASE_TAG = "community-assets"  # 2026-06-21 收敛：media 并入「社区二创」桶
REFRESH_API = "https://discord.com/api/v10/attachments/refresh-urls"
REFRESH_BATCH = 50


def load_manifest():
    # 新址缺档时回退读旧址（一次性迁移兜底）：保住 06-21 前最后一份断点台账，
    # 避免迁移当口从零重下全部媒体。写方只写新址，旧址自然淘汰。
    path = MANIFEST if Path(MANIFEST).is_file() else _LEGACY_MANIFEST
    if not Path(path).is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(m):
    Path(MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    # 原子替换：manifest 是回填断点续跑的唯一依据，写一半被中断即整份不可解析,
    # 下一轮当作「无 manifest」从零重跑（既浪费带宽，也可能重复计费的 API 配额）。
    news_common.dump_json_atomic(MANIFEST, m, indent=1)


def ext_of(url, d="jpg"):
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|mp4)(\?|$)", url, re.IGNORECASE)
    return m.group(1).lower() if m else d


def fname(url, source):
    # usedforsecurity=False：这里 md5 只用来把 URL 折成稳定文件名，与安全无关。
    # 标出来不只是消 lint——**FIPS 模式的 Python 会让裸 hashlib.md5() 直接抛
    # ValueError**，加了这个标志才继续可用。
    return f"{source}_{hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:12]}.{ext_of(url)}"


def fetch(url, dest, source):
    url = html.unescape(url)  # 归档 URL 常含 &amp; 等实体，破坏签名导致 403
    headers = {"User-Agent": UA}
    if source in REFERER:
        headers["Referer"] = REFERER[source]
    # 经 news_common.safe_get：SSRF 守卫 + 逐跳重校验 + IP pin，禁用自动重定向（R2-H1）。
    try:
        resp = news_common.safe_get(url, headers=headers, timeout=20)
    except ValueError:
        return "err_unsafe_url"  # 不安全 URL / 坏重定向，拒绝
    try:
        resp.raise_for_status()
        data = resp.content
    except requests.HTTPError:
        return f"http_{resp.status_code}"
    except requests.RequestException as e:
        return f"err_{type(e).__name__}"
    if len(data) < 200:
        return "empty"
    # 先写同目录临时文件再 os.replace：直接 open(dest,'wb').write(data) 一旦写一半
    # 被中断（磁盘满 / 进程被杀），留下的半截文件在 manifest 里记的是 "ok",
    # 后续轮次不会重下——半截图片就此永久入库。
    tmp = f"{dest}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return "ok"


# ── 进程级预算（扫描修复 2026-07-28）──────────────────────────────────────────
# 缺陷：`--budget` 原先只从**下载循环**开始计时，而它前面还压着三个真正昂贵的阶段:
#   ① collect_urls 全量遍历社区档案（数百万条 discord JSONL + 全部平台 json）；
#   ② 去重；
#   ③ refresh_discord 对**全部**待办 discord 链接批量刷签名（每批一次 HTTP + 0.3s
#      节流；待办上万即数千批）。
# 三者都不受预算约束，于是 CI 作业在进入下载循环之前就先撞上 timeout-minutes: 90。
# 实测后果（GitHub 侧取证）：backfill-media.yml 连续 37 天、500+ 次运行**没有一次
# 成功**——每次都是「Backfill 步骤跑满 89 分 47 秒被杀 → 上传与提交 manifest 步骤
# skipped」。manifest 从不落地，下一轮又从同一处重来，是个恒不前进的死循环。
# （2026-07-26 曾把 cron 由每小时降为每日，判词是「排队顶掉待跑 run」——那是误诊:
#  改为每日后的首跑 run#522 照样跑满 90 分钟被杀。真因在预算覆盖面，不在触发频率。）
# 修法：预算改为**从进程启动算起的截止时刻**，三个阶段共用，任一阶段逼近即收尾,
# 让作业总能走到「保存 manifest → 上传 → 提交」，从而每轮都有实际推进。
_START = time.time()
_DEADLINE: float | None = None


def _set_budget(seconds: int) -> None:
    global _DEADLINE
    _DEADLINE = _START + seconds


def _budget_left() -> float:
    """剩余预算秒数；未设预算时返回正无穷。"""
    return float('inf') if _DEADLINE is None else _DEADLINE - time.time()


def _budget_exhausted(reserve: float = 0.0) -> bool:
    """预算是否已耗尽。reserve 为给后续阶段预留的秒数。"""
    return _budget_left() <= reserve


def _reserve_for_downloads() -> float:
    """给下载阶段预留的秒数 = 总预算的三成。

    没有这条预留，遍历/刷新可以把预算吃干，下载一件都做不成——那一轮仍然「零推进」,
    与超时被杀的结果没有区别。留出下载窗口，才能保证每轮至少补进一批文件并落 manifest。
    """
    if _DEADLINE is None:
        return 0.0
    return (_DEADLINE - _START) * 0.3


def refresh_discord(urls, token):
    """批量把过期 Discord 链接换成新签名链接。返回 {original: refreshed}。"""
    out = {}
    for i in range(0, len(urls), REFRESH_BATCH):
        # 给下载阶段留至少三成预算：链接刷新本身不产出任何已下载文件,
        # 全花在刷新上等于这一轮又白跑。
        if _budget_exhausted(reserve=_reserve_for_downloads()):
            print(f"  预算逼近，刷新提前收尾：已刷 {i}/{len(urls)} 条链接")
            break
        batch = urls[i:i + REFRESH_BATCH]
        body = json.dumps({"attachment_urls": batch}).encode()
        req = urllib.request.Request(REFRESH_API, data=body, method="POST", headers={
            "Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)
            for item in res.get("refreshed_urls", []):
                out[item["original"]] = item["refreshed"]
        except Exception as e:
            print(f"  refresh 批 {i//REFRESH_BATCH} 失败: {type(e).__name__}")
        time.sleep(0.3)
    return out


def collect_urls(include_discord):
    urls = []
    # 遍历一律经 archive_layout（读方唯一入口）。原写法三处漏：
    #   1. glob 只吃 `<平台>/<date>.json` 扁平层，分层平台（steam 家族 / appstore /
    #      google_play / youtube 落 <平台>/<区服>/<类型>/）整批扫不到；
    #   2. 冷层 `.json.gz` 一个都匹配不上（上上个月及更早全漏，CLAUDE.md §5.2）；
    #   3. 日期茎用 basename[:-5] 手工切片，对 `.json.gz` 会切成 '2026-04-01.j',
    #      长度校验随即判 != 10 丢弃——即便匹配上也照样丢。
    src_root = Path(SRC)
    sources = sorted(p.name for p in src_root.iterdir()
                     if p.is_dir() and p.name != "discord") if src_root.is_dir() else []
    for source in sources:
        if _budget_exhausted(reserve=_reserve_for_downloads()):
            print(f"  预算逼近，档案遍历提前收尾（停在平台 {source} 之前）")
            return urls
        for fpath in archive_layout.dated_files(source, src_root):
            d = archive_layout.date_stem(fpath)
            try:
                with archive_layout.open_archive_text(fpath) as f:
                    items = json.load(f)
            # OSError 已覆盖 gzip.BadGzipFile（冷层坏档）
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            items = items if isinstance(items, list) else items.get("items", [])
            for it in items:
                if isinstance(it, dict) and it.get("media_url"):
                    urls.append((it["media_url"], source, d))
    if include_discord:
        # 三区服全量遍历（2026-07-10 方案甲布局，新旧布局经 SSOT 回落）
        for n_file, fpath in enumerate(archive_layout.iter_discord_message_files(src_root / "discord")):
            # discord 是遍历量最大的一段（数百万条），每 200 档查一次预算
            if n_file % 200 == 0 and _budget_exhausted(reserve=_reserve_for_downloads()):
                print(f"  预算逼近，discord 遍历提前收尾（已扫 {n_file} 档）")
                return urls
            # 日期茎一律经 SSOT（CLAUDE.md §5.2），不再手工 replace 后缀
            d = archive_layout.date_stem(fpath)
            if len(d) != 10:
                continue
            with archive_layout.open_archive_text(fpath) as fh:
                for line in fh:
                    if '"content_type": "image' not in line:
                        continue
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:  # 只挡坏行，不吞块内程序错误
                        continue
                    for att in (m.get("attachments") or []):
                        if str(att.get("content_type", "")).startswith("image") and att.get("url"):
                            urls.append((att["url"], "discord", d))
    return urls


def upload_release():
    files = glob.glob(f"{FILES}/*")
    if not files:
        print("无可上传文件")
        return
    tar = f"{ROOT}/media/media-files.tar"
    subprocess.run(["tar", "-cf", tar, "-C", FILES, "."], check=True)
    # 2026-06-21 收敛：media 二进制并入「社区二创」community-assets（与 fanart 同桶），
    # 不再单独建 media-archive-v1。往现有 release 追加资产（--clobber 覆盖同名），
    # 绝不 delete release（会连 fanart 一起删）。
    r = subprocess.run(["gh", "release", "upload", RELEASE_TAG, tar, "--clobber"],
                       capture_output=True, text=True)
    print(r.stdout or r.stderr)


def main():
    ap = argparse.ArgumentParser(description="媒体补录器")
    ap.add_argument("--no-discord", action="store_true", help="只补持久平台源，跳过 Discord")
    ap.add_argument("--budget", type=int, default=1800, help="运行预算秒数")
    ap.add_argument("--delay", type=float, default=0.12)
    ap.add_argument("--upload", action="store_true", help="补录后打包传 Releases")
    a = ap.parse_args()

    if a.upload:
        upload_release()
        return

    # 预算从**进程启动**算起（见 _START 上方说明），而不是从下载循环算起
    _set_budget(a.budget)

    os.makedirs(FILES, exist_ok=True)
    token = os.environ.get("DISCORD_BOT_TOKEN")
    manifest = load_manifest()
    urls = collect_urls(include_discord=not a.no_discord)

    seen = set()
    todo = []
    for url, src, d in urls:
        if url in seen:
            continue
        seen.add(url)
        if manifest.get(url, {}).get("status") == "ok":
            continue
        todo.append((url, src, d))

    # Discord 过期链接先刷新（需 token）
    refreshed = {}
    disc = [u for u, s, _ in todo if s == "discord"]
    if disc and token:
        print(f"刷新 {len(disc)} 个 Discord 链接（refresh-urls）...")
        refreshed = refresh_discord(disc, token)
        print(f"  刷新成功 {len(refreshed)} 个")
    elif disc and not token:
        print(f"警告：{len(disc)} 个 Discord 链接多已过期，但未提供 DISCORD_BOT_TOKEN，无法刷新；"
              f"将直接尝试原链接（多半 404）。设置 DISCORD_BOT_TOKEN 后续跑可补回。")

    done = 0
    stats = {}
    for url, src, d in todo:
        if _budget_exhausted():
            print(f"预算耗尽，已处理 {done}/{len(todo)}，下次续跑")
            break
        dl_url = refreshed.get(url, url)
        fn = fname(url, src)
        status = fetch(dl_url, os.path.join(FILES, fn), src)
        manifest[url] = {"filename": fn if status == "ok" else None, "source": src,
                         "date": d, "status": status, "refreshed": url in refreshed,
                         # utcnow() 产出的是「无时区」datetime，isoformat() 不带 +00:00
                         # 后缀——落档后读方无从分辨是 UTC 还是本地时；且 3.12 起已弃用。
                         "checked_at": datetime.now(UTC).isoformat()}
        stats[status] = stats.get(status, 0) + 1
        done += 1
        if done % 200 == 0:
            save_manifest(manifest)
            print(f"  ...{done}/{len(todo)}")
        time.sleep(a.delay)

    save_manifest(manifest)
    ok = sum(1 for v in manifest.values() if v.get("status") == "ok")
    print(f"本次处理 {done}；状态 {stats}")
    # 分母与分子原先来自两个不同总体：`ok` 是**清单累计**（历轮所有成功），
    # `len(seen)` 是**本轮扫到**的候选——而 collect_urls 在预算逼近时会提前 return，
    # 本轮只扫到几百条时会打出「累计存活图 12000 张 / 候选 300 URL」这种分子远大于
    # 分母的比值，读起来像覆盖率爆表。两个总体分别报，并点名本轮扫描是否被预算截断。
    truncated = " ⚠ 本轮扫描/刷新被预算截断，候选数不代表全量" if _budget_exhausted() else ""
    print(f"清单累计：{ok} 张存活 / {len(manifest)} 条记录。"
          f"本轮扫到候选 {len(seen)} URL、待办 {len(todo)}、处理 {done}。{truncated}")
    print(f"清单 {MANIFEST}")


if __name__ == "__main__":
    main()
