#!/usr/bin/env python3
"""同人图采集器 — 把某日各信息源的玩家二创图抓到本地，供日报附录嵌图。

信息源（每个 Discord 频道亦视作独立源）：
  - Discord 二创类频道附件（image/*）：同人创作 / art-and-memes / 官方素材 / 晒卡分享等
  - Pixiv media_url（需 Referer 头绕过热链保护）

注意：Discord 附件本体长期在 Discord，过期的只是带签名的 CDN 链接。带 DISCORD_BOT_TOKEN 时
经 refresh-urls 接口刷新即可全量补回（历史日亦可）；无 token 时仅能下到最近 ~24h 仍存活的。

用法：
  DISCORD_BOT_TOKEN=xxx python projects/news/scripts/collect_fanart.py --date 2026-06-01 --out projects/news/data/fanart/2026-06-01
输出：out/ 下图片文件 + gallery_manifest.json（每条含 source/channel/author/text/file/status）。
"""
import os
import json
import re
import time
import argparse
import hashlib
import urllib.request
import urllib.error
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_layout  # discord 布局 SSOT（2026-07-10 方案甲）

# 归档根**不再硬编码**（2026-07-26 死手开关首跑抓到：T62 §7甲 把数据湖迁出 code 仓后，
# 本脚本仍按在树路径找 discord/global/channel_index.json，自 07-21 起天天崩）。
# 一律问 archive_layout 这个单一真相源；env `BIAV_SC_DATA_ROOT` 指向数据仓 checkout，
# 未设时它自己回落在树默认，故本地与 CI 同一份代码。
def _community_root() -> Path:
    """社区档案根。调用期现算——单测 monkeypatch env 即可改根。"""
    return archive_layout.community_root()


def _discord_global_dir() -> Path:
    """主服 global 数据目录（原语义：二创频道只在主服；新旧布局经 SSOT 解析）。

    调用期经 archive_layout 现算（新旧布局与数据仓 env 根均由 SSOT 解析）。
    """
    droot = _community_root() / "discord"
    return archive_layout.discord_region_roots(droot).get("global", droot / "global")
FANART_CH = ["同人创作", "art-and-memes", "官方素材", "official-materials",
             "fanart", "创作", "二创", "绘"]
HEADERS = {"User-Agent": "Mozilla/5.0 (silver-core fanart collector)"}
# Discord API 要求合规 Bot User-Agent（DiscordBot (url, version)），否则可能被拒
DISCORD_UA = "DiscordBot (https://github.com/lightproud/BIAV-SC-CODE, 1.0)"
REFRESH_API = "https://discord.com/api/v10/attachments/refresh-urls"
# 单文件下载上限（与 download_media.MAX_FILE_SIZE_MB 同口径）。下载目标是
# **社区任意成员贴出来的**附件 URL，体积完全由外部决定：原写法 `r.read()` 无参,
# 一条 4 GB 的附件链接就能把整个采集进程的内存吃光（CI runner 直接 OOM 被杀,
# 当轮 gallery 全灭）。改为**读到上限即止**——超限的图不是图，丢掉即可。
MAX_FILE_BYTES = 20 * 1024 * 1024


def refresh_discord(urls, token):
    """批量把过期 Discord 链接换成新签名链接，返回 {original: refreshed}。"""
    out = {}
    for i in range(0, len(urls), 50):
        body = json.dumps({"attachment_urls": urls[i:i + 50]}).encode()
        req = urllib.request.Request(REFRESH_API, data=body, method="POST", headers={
            "Authorization": f"Bot {token}", "Content-Type": "application/json",
            "User-Agent": DISCORD_UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                for item in json.load(r).get("refreshed_urls", []):
                    out[item["original"]] = item["refreshed"]
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except (OSError, UnicodeDecodeError):  # 读不出错误正文而已，无碍主流程
                pass
            print(f"  refresh 批 {i // 50} 失败: HTTP {e.code} {detail}")
        except Exception as e:
            print(f"  refresh 批 {i // 50} 失败: {type(e).__name__}")
        time.sleep(0.3)
    return out


def fetch(url, dest, referer=None):
    # 已在盘上的直接认账，不重下。本采集器按日期目录产出，而 recover-fanart 的补录窗口
    # （默认 4 天）与 collect-fanart 每日跑必然重叠——同一日期会被反复采。重跑时 Discord
    # 签名链接多半已过期（无 token 或刷新失败即 404），于是每条都判 http_404 / file=None,
    # 把 gallery_manifest.json 整份重写成「一张都没下到」，而图片文件本身仍躺在同一目录
    # （月桶 tar 连同旧图一起打包）——清单当场与实物矛盾，日报据此嵌不出任何图。
    # 跳过已存在文件让重跑成为真正的 no-op（顺带省一轮带宽与 CDN 敲门）。
    _dest = Path(dest)
    if _dest.is_file() and _dest.stat().st_size >= 200:
        return "ok"
    req = urllib.request.Request(url, headers={**HEADERS, **({"Referer": referer} if referer else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            # 多读 1 字节用于判超限：读满 MAX_FILE_BYTES+1 说明本体 > 上限。
            data = r.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return "too_large"
        if len(data) < 200:
            return "empty"
        # 先写同目录临时文件再 os.replace：直写一旦在中途被杀（工作流 30 分钟超时）
        # 会留下半截图片，而上面「已存在即认账」会把这半截当完好文件永久收编。
        tmp = f"{dest}.part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return "ok"
    except urllib.error.HTTPError as e:
        return f"http_{e.code}"
    except Exception as e:
        return f"err_{type(e).__name__}"


def ext_of(url, default="jpg"):
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", url, re.IGNORECASE)
    return m.group(1).lower() if m else default


def main():
    ap = argparse.ArgumentParser(description="同人图采集器")
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    gallery = []
    token = os.environ.get("DISCORD_BOT_TOKEN")

    # ---- Discord 二创频道附件：先收集，再（带 token）刷新过期链接，最后下载 ----
    _gdir = _discord_global_dir()
    with open(_gdir / "channel_index.json", encoding="utf-8") as _f:
        idx = json.load(_f)
    dir2name = {v["dir"]: v["name"] for v in idx.values()}
    disc = []  # [{channel, author, text, orig_filename, url, fn}]
    for d, name in dir2name.items():
        if not any(k in name for k in FANART_CH):
            continue
        # 冷热分层（CLAUDE.md §5.2）：上上个月及更早的 dated 归档已压成 .jsonl.gz。
        # 原写法只 isfile() 裸 .jsonl，于是任何进了冷层的日期都静默判「无归档」跳过
        # ——历史日期的二创采集恒为 0 条，且不报任何错。读方必须走冷热双开入口。
        raw = _gdir / "channels" / d / f"{a.date}.jsonl"
        fp = raw if raw.is_file() else raw.with_suffix(".jsonl.gz")
        if not fp.is_file():
            continue
        with archive_layout.open_archive_text(fp) as _fh:
            lines = _fh.readlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:  # 只挡坏行，不吞块内程序错误
                continue
            for att in (m.get("attachments") or []):
                if not str(att.get("content_type", "")).startswith("image"):
                    continue
                url = att.get("url", "")
                disc.append({"channel": name, "author": m.get("author_id", ""),
                             "text": (m.get("content") or "")[:200],
                             "orig_filename": att.get("filename", ""), "url": url,
                             "fn": f"discord_{att.get('id','')}.{ext_of(att.get('filename', url))}"})
    refreshed = {}
    if disc and token:
        print(f"刷新 {len(disc)} 个 Discord 二创附件链接...")
        refreshed = refresh_discord([x["url"] for x in disc], token)
        print(f"  刷新成功 {len(refreshed)}")
    elif disc and not token:
        print(f"警告：{len(disc)} 个 Discord 附件链接多已过期，无 DISCORD_BOT_TOKEN 无法刷新；"
              f"仅试原链接（历史日多 404）。")
    for x in disc:
        status = fetch(refreshed.get(x["url"], x["url"]), os.path.join(a.out, x["fn"]))
        gallery.append({"source": "discord", "channel": x["channel"], "author": x["author"],
                        "text": x["text"], "orig_filename": x["orig_filename"],
                        "refreshed": x["url"] in refreshed,
                        "file": x["fn"] if status == "ok" else None, "status": status})

    # ---- Pixiv ----
    # 落点一律问 archive_layout（写方同源）。原写法硬编码 `platforms/pixiv/`——多出的
    # `platforms/` 一段没有任何写方产出（写方是 community_root/<平台>/<date>.json），
    # 于是 isfile() 恒 False，pixiv 二创永远采到 0 条且不报任何错。
    _pf, _rg, _st = archive_layout.resolve_write_layout("pixiv")
    _praw = _community_root() / archive_layout.build_relpath(_pf, _rg, _st, a.date)
    pfp = _praw if _praw.is_file() else _praw.with_suffix(".json.gz")
    if pfp.is_file():
        with archive_layout.open_archive_text(pfp) as _f:
            items = json.load(_f)
        items = items if isinstance(items, list) else items.get("items", [])
        for it in items:
            if not isinstance(it, dict) or not it.get("media_url"):
                continue
            url = it["media_url"]
            # usedforsecurity=False：仅用于折算文件名；亦保证 FIPS 模式下不抛 ValueError
            fn = f"pixiv_{hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:10]}.{ext_of(url)}"
            status = fetch(url, os.path.join(a.out, fn), referer="https://www.pixiv.net/")
            gallery.append({"source": "pixiv", "channel": "pixiv",
                            "author": it.get("author", ""),
                            "text": (it.get("title") or "")[:200],
                            "url": it.get("url", ""),
                            "file": fn if status == "ok" else None, "status": status})

    ok = [g for g in gallery if g["status"] == "ok"]
    with open(os.path.join(a.out, "gallery_manifest.json"), "w", encoding="utf-8") as _f:
        json.dump(gallery, _f, ensure_ascii=False, indent=2)
    by_status = {}
    for g in gallery:
        by_status[g["status"]] = by_status.get(g["status"], 0) + 1
    print(f"同人图采集 {a.date}: 候选 {len(gallery)} / 成功下载 {len(ok)}")
    print(f"状态分布: {by_status}")
    print(f"manifest: {os.path.join(a.out, 'gallery_manifest.json')}")


if __name__ == "__main__":
    main()
