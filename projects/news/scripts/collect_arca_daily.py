#!/usr/bin/env python3
"""collect_arca_daily.py — arca_live 日采单脚本（银芯 CC 例程专用，方案 2 过渡桥）。

背景（守密人 2026-07-10 裁定方案 2）：Cloudflare 拦死 GitHub Actions 机房 IP
（HTTP 403 / PW 挑战页超时 / App API 403 三路全堵），arca_live 在免费确定性层
不可采；银芯 CC 云环境出口实测畅通。故由每日一次的 CC 例程（fresh session）
调用本脚本完成采集——**数据路径仍是确定性脚本，例程会话只是执行环境**。

设计约束：
  - 单脚本闭环：采集 → 按日归档（复用 archive_platforms 写方，走 archive_layout
    布局）→ commit → push（带重试）。例程会话只需跑一条命令，token 开销最小化。
  - 响亮失败：采集零条 / push 失败均非零退出并打印可诊断原因——例程侧不静默；
    健康兜底由沉默源审计承担（arca_live 断更 >7 天即 degraded 告警）。
  - 过渡桥定位：GC 编排里的 arca 尝试保留，若 CF 某日放行 Actions，正常路径
    自动恢复，本例程即可退役（删 Routine + 本脚本）。

用法（例程提示词即此一条）：
  python3 projects/news/scripts/collect_arca_daily.py
  python3 projects/news/scripts/collect_arca_daily.py --no-push   # 本地验证
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import archive_layout  # noqa: E402  归档日期基准 SSOT（北京日期）
from global_collectors import fetch_arca_live  # noqa: E402
from archive_platforms import write_archive, item_date_utc8  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description='arca_live 日采（CC 例程单脚本）')
    ap.add_argument('--no-push', action='store_true', help='只采集归档，不 commit/push（本地验证）')
    args = ap.parse_args()

    items = fetch_arca_live()
    if not items:
        print('FATAL: fetch_arca_live 返回 0 条——出口疑似被 Cloudflare 拦截或站点改版，'
              '请在会话内 live 探测 https://arca.live/b/forgettingeve 定性', file=sys.stderr)
        return 1

    # 按内容日期分桶落 Record/Community/arca_live/{date}.json（平铺源，无区服/类型层）
    fallback = archive_layout.archive_date_str()  # 日期基准 SSOT（北京日期）
    by_date: dict[str, list] = {}
    for it in items:
        by_date.setdefault(item_date_utc8(it, fallback), []).append(it)
    total = 0
    for date_str, day_items in sorted(by_date.items()):
        count = write_archive('arca_live', None, None, date_str, day_items)
        print(f'  arca_live/{date_str}.json  merged={count}')
        total += len(day_items)
    print(f'采集 {len(items)} 条，归档覆盖 {len(by_date)} 个日期桶')

    if args.no_push:
        print('--no-push：跳过提交')
        return 0

    def run(*cmd):
        return subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True, text=True)

    # 机器身份只随单条命令生效（-c 一次性参数）——绝不 `git config` 改写仓库级
    # 身份：自绑定模式下脚本跑在主会话仓库里，改写会污染会话后续提交的署名
    # （lesson #48 follow-up / lesson #49 跨进程污染）。pull --rebase 重写本地
    # 提交时同样需要身份，故 commit 与 pull 两处都带。
    bot_identity = ['-c', 'user.name=github-actions[bot]',
                    '-c', 'user.email=github-actions[bot]@users.noreply.github.com',
                    '-c', 'commit.gpgsign=false']
    # `git add` 的返回码必须查：它失败时暗室里什么都不会暂存，紧随其后的
    # `diff --staged --quiet` 便会返回 0，脚本于是打印「无新增内容」并 return 0
    # ——采到的数据被静默丢弃，且对外表现为一次成功的采集轮次。
    added = run('git', 'add', 'Public-Info-Pool/Record/Community/arca_live/')
    if added.returncode != 0:
        print(f'FATAL: git add 失败: {added.stderr[:300]}', file=sys.stderr)
        return 1
    diff = run('git', 'diff', '--staged', '--quiet')
    if diff.returncode == 0:
        print('无新增内容，无需提交')
        return 0
    commit = run('git', *bot_identity, 'commit', '-m',
                 'chore: collect arca_live via CC routine [skip ci]')
    if commit.returncode != 0:
        print(f'FATAL: commit 失败: {commit.stderr[:300]}', file=sys.stderr)
        return 1
    for attempt in range(1, 5):
        pull = run('git', *bot_identity, 'pull', '--rebase', 'origin', 'main')
        if pull.returncode != 0:
            # rebase 冲突会把工作树留在 rebase 中途，后续每一轮都注定失败；必须先
            # 收回半场再重试，否则 4 次重试全空转，且工作树被留在脏状态。
            run('git', 'rebase', '--abort')
            print(f'pull --rebase 第 {attempt} 次失败: {pull.stderr[:200]}', file=sys.stderr)
            time.sleep(2 ** attempt)
            continue
        push = run('git', 'push', 'origin', 'HEAD:main')
        if push.returncode == 0:
            print('已推送 main')
            return 0
        # 原写法 `pull.stderr or push.stderr`：git pull 成功时也往 stderr 写进度，
        # 于是这里几乎永远印的是 pull 的噪声而不是 push 的真实错因。
        print(f'push 第 {attempt} 次失败: {push.stderr[:200]}', file=sys.stderr)
        time.sleep(2 ** attempt)
    print('FATAL: push 重试 4 次均失败', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
