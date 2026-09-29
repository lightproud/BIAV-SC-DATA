#!/usr/bin/env python3
"""
Discord 服务器清单探测 — 列出 bot 当前加入的所有服务器（guild）

用途：新服务器接入归档计划前的「发现」步骤。bot 接入新服务器（如日服）后运行本
脚本，即可列出 bot 所在的全部 guild，对照已登记清单（Global / 志愿者）高亮
「未登记」服务器，并把快照写入 data/discord/guilds_seen.json，供配置归档
workflow 时取用 guild ID。

只读：仅调用 GET /users/@me/guilds，不抓消息、不碰 channels 数据、不改 state。

用法:
  DISCORD_BOT_TOKEN=xxx python projects/news/scripts/discord_list_guilds.py
"""

import json
import os
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

# 复用归档器的 Global guild 常量；已登记清单从布局 SSOT 注册表派生（单一映射源）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive_layout import DISCORD_GUILD_REGIONS, discord_root  # noqa: E402

API_BASE = 'https://discord.com/api/v10'
# 429 退避的本地硬顶（与 discord_archiver.MAX_RETRY_AFTER_SECONDS 同口径）。
MAX_RETRY_AFTER_SECONDS = 60

# 已登记服务器（归档计划已覆盖）。不在此表的 guild = 待接入候选。
# 2026-07-10 方案甲：清单 = archive_layout.DISCORD_GUILD_REGIONS，归档至 discord/<区服>/。
VOLUNTEER_GUILD_ID = '1402537664619479100'
KNOWN_GUILDS = {
    gid: f'{region} · 归档至 discord/{region}/'
    for gid, region in DISCORD_GUILD_REGIONS.items()
}

SEEN_PATH = discord_root() / 'guilds_seen.json'  # 分仓桥接：env BIAV_SC_DATA_ROOT 或在树默认


def classify_guilds(guilds: list, known: dict) -> tuple[list, list]:
    """对照已登记清单给每个 guild 打标。纯函数，无 IO。

    返回 (rows, unregistered)：
      rows         —— 全部 guild，含 registered/role 标注，未登记者排在前
      unregistered —— 未登记的 guild（待接入归档计划的候选，如新接入的日服）
    """
    rows = []
    unregistered = []
    for g in guilds:
        gid = str(g.get('id', ''))
        name = g.get('name', '')
        role = known.get(gid)
        registered = role is not None
        rows.append({'id': gid, 'name': name, 'registered': registered, 'role': role})
        if not registered:
            unregistered.append({'id': gid, 'name': name})
    # 未登记(False)排前，便于日志一眼定位候选；同组按名称升序。
    rows.sort(key=lambda r: (r['registered'], r['name']))
    return rows, unregistered


def _get(path, headers, **params):
    import requests

    url = f'{API_BASE}{path}'
    resp = None
    for _ in range(4):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            # retry_after 是**服务端**给的秒数，无上限照睡等于把本进程的存活时间
            # 交给对端：一个 retry_after=86400 的 429（全局限流 / 中间层拦截）会让
            # 工具干睡到作业超时被杀。夹到 60 秒，荒谬值很快耗尽这 4 次重试并如实报错。
            wait = min(max(resp.json().get('retry_after', 2), 2), MAX_RETRY_AFTER_SECONDS)
            time.sleep(wait)
            continue
        return resp
    return resp


def main() -> int:
    token = os.environ.get('DISCORD_BOT_TOKEN', '')
    if not token:
        print('FAIL: DISCORD_BOT_TOKEN 未设置')
        return 1

    headers = {'Authorization': f'Bot {token}', 'Content-Type': 'application/json'}
    resp = _get('/users/@me/guilds', headers, limit=200)
    if resp is None:
        print('FAIL: 请求服务器清单失败（无响应）')
        return 1
    if resp.status_code == 401:
        print('FAIL: token 无效（401 Unauthorized）')
        return 1
    if resp.status_code != 200:
        print(f'FAIL: 获取服务器清单失败 HTTP {resp.status_code}: {resp.text[:200]}')
        return 1

    guilds = resp.json()
    if not isinstance(guilds, list):
        print(f'FAIL: 响应格式异常: {str(guilds)[:200]}')
        return 1

    rows, unregistered = classify_guilds(guilds, KNOWN_GUILDS)

    print(f'bot 当前加入 {len(rows)} 个服务器：')
    for r in rows:
        mark = r['role'] if r['registered'] else '★ 未登记（待接入候选）'
        print(f"  - {r['id']}  {r['name']}  [{mark}]")

    snapshot = {
        'probed_at': datetime.now(UTC).isoformat(),
        'bot_guild_count': len(rows),
        'guilds': rows,
        'unregistered': unregistered,
    }
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print(f'\n快照已写入 {SEEN_PATH}')

    print('---')
    if not unregistered:
        print('结论: 未发现未登记服务器。bot 所在服务器均已纳入归档计划。')
    elif len(unregistered) == 1:
        u = unregistered[0]
        print(f"结论: 发现 1 个未登记服务器 →「{u['name']}」(ID {u['id']})。")
        print("      下一步: 将此 ID 填入 .github/workflows/discord-archive-jp.yml 的")
        print("      env.JP_GUILD_ID，并取消该 workflow 的 schedule 注释以启用日服归档。")
    else:
        print(f'结论: 发现 {len(unregistered)} 个未登记服务器，请确认哪个是日服后填入归档 workflow：')
        for u in unregistered:
            print(f"      - {u['id']}  {u['name']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
