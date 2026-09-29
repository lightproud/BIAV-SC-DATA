#!/usr/bin/env python3
"""忘却前夜店面日巡检（store-patrol），纯标准库。

2026-09-28 守密人裁定：原宿主 projects/silver-core-maestro-sdk/examples/store-patrol.mjs
（跑在 maestro 的 TaskLedger / LedgerDriver 上）随 maestro SDK 删除，巡检移植为本独立脚本，
不再依赖任何 SDK / Node。

做什么：按目标注册表（同目录 store_patrol_targets.json）逐个拉 Steam 公开 JSON 端点，
抽出「稳定字段签名」（易变 / 请求相关字段剔除，店面没变则签名逐日相同），写入：

  Public-Info-Pool/Record/store-patrol/{target-id}/
    {YYYY-MM-DD}.json   当日快照（UTC 日期；每次成功巡检都写 / 覆盖）
    latest.json         当前签名（仅签名变化时重写）
    changes.jsonl       变更日志，只追加（仅签名变化时追加一行）
  Public-Info-Pool/Record/store-patrol/state/patrol-state.json   运行状态

输出格式与原 .mjs 逐字节兼容（JSON.stringify(v, null, 2) + '\\n' ⇔ json.dumps(indent=2,
ensure_ascii=False) + '\\n'；changes.jsonl 为紧凑单行；checked_at 为毫秒精度 ISO + 'Z'），
历史档案无缝续写。

状态档取舍：maestro 账本 state/ledger.json（sessions + queries，每天 +2 会话 +2~6 查询行、
只增不删）**冻结保留不再写**（git 里留作 07-18 ~ 09-27 的运行史证据）；新写
state/patrol-state.json，每目标一条「最近一次尝试 / 最近一次成功 / 连续失败数 / 末次错误」，
体积恒定。同 UTC 日已成功的目标默认跳过（对齐原账本「target × day 幂等派发」语义），
--force 强制重巡。

失败语义：单目标失败（HTTP 错 / 超时 / 解析失败，每目标最多 3 次尝试、指数退避）不影响
其他目标落盘；全部目标处理完、状态写完之后，只要有目标失败即退出码 1（工作流先提交已采部分，
再让作业变红）。

用法：
  python projects/news/scripts/store_patrol.py                 # 正式巡检，写仓内档案
  python projects/news/scripts/store_patrol.py --out /tmp/sp   # 写到别处（试跑）
  python projects/news/scripts/store_patrol.py --dry-run       # 只拉取 + 打印签名，不写任何文件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
DEFAULT_TARGETS = HERE / "store_patrol_targets.json"
DEFAULT_ARCHIVE = REPO_ROOT / "Public-Info-Pool" / "Record" / "store-patrol"
STATE_REL = Path("state") / "patrol-state.json"

USER_AGENT = "biav-store-patrol/1.0 (+https://github.com/lightproud/BIAV-SC-CODE)"
TIMEOUT_S = 30
MAX_ATTEMPTS = 3
RETRY_BASE_S = 2.0


# ---------------------------------------------------------------- 抽取器 ----
def _extract_steam_appdetails(body: str, target: dict) -> dict:
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        parsed = {}
    key = str(target.get("key") or "")
    entry = parsed.get(key)
    if entry is None:
        # Steam 偶发以别的 id 作键（2026-09-25：appids=3052450 返回键 "5224390"，
        # 其 data.steam_appid 仍是 3052450）——回落到 steam_appid 匹配的条目。
        for e in parsed.values():
            if isinstance(e, dict) and isinstance(e.get("data"), dict) \
                    and str(e["data"].get("steam_appid")) == key:
                entry = e
                break
    if not isinstance(entry, dict) or entry.get("success") is not True \
            or not isinstance(entry.get("data"), dict):
        raise ValueError("steam-appdetails: no data for app " + (key or "(missing key)"))
    d = entry["data"]
    po = d.get("price_overview")
    rd = d.get("release_date") if isinstance(d.get("release_date"), dict) else {}
    return {
        "name": d.get("name"),
        "type": d.get("type"),
        "is_free": d.get("is_free"),
        "price": {
            "currency": po.get("currency"),
            "initial": po.get("initial"),
            "final": po.get("final"),
            "discount_percent": po.get("discount_percent"),
        } if isinstance(po, dict) else None,
        "release_date": rd.get("date"),
        "coming_soon": rd.get("coming_soon"),
    }


def _extract_steam_review_summary(body: str, target: dict) -> dict:
    parsed = json.loads(body)
    s = parsed.get("query_summary") if isinstance(parsed, dict) else None
    if not isinstance(s, dict):
        raise ValueError("steam-review-summary: no query_summary")
    # num_reviews 随请求分页大小变化，不入签名。
    return {
        "review_score": s.get("review_score"),
        "review_score_desc": s.get("review_score_desc"),
        "total_positive": s.get("total_positive"),
        "total_negative": s.get("total_negative"),
        "total_reviews": s.get("total_reviews"),
    }


EXTRACTORS: dict[str, Callable[[str, dict], dict]] = {
    "steam-appdetails": _extract_steam_appdetails,
    "steam-review-summary": _extract_steam_review_summary,
}


# ---------------------------------------------------------------- 工具 ------
def fetch_text(url: str, timeout: float = TIMEOUT_S) -> str:
    """GET 一个 URL 返回文本；非 2xx 由 urllib 抛 HTTPError。测试里 monkeypatch 掉。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def iso_now() -> str:
    """与 JS Date#toISOString 同形：毫秒精度 + 'Z'。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_json_atomic(path: Path, value: Any) -> None:
    """先写 .tmp 再 rename：进程中途被杀也不会留下半截文件被提交上 main。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_pretty(value), encoding="utf-8")
    os.replace(tmp, path)


def read_json_or_none(path: Path) -> Any:
    """读不了（缺失 / 损坏）即视作无基线，不让一个坏字节卡死该目标之后的每一天。"""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"targets": {}}
    parsed = read_json_or_none(path)
    if parsed is None or not isinstance(parsed.get("targets"), dict):
        # 隔离损坏的状态档（证据留存），从空状态起跑；档案本体不受影响。
        path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time() * 1000)}"))
        return {"targets": {}}
    return parsed


# ---------------------------------------------------------------- 主逻辑 ----
def patrol_target(target: dict, archive_dir: Path, today: str, fetch: Callable[[str], str],
                  write: bool = True) -> dict:
    """巡一个目标：拉取 → 抽签名 → （write 时）落快照 / 变更。返回 {changed, signature, snapshot}。"""
    extract = EXTRACTORS.get(target.get("extract", ""))
    if extract is None:
        raise ValueError(f"no extractor '{target.get('extract')}' for {target.get('id')}")
    body = fetch(target["url"])
    signature = extract(body, target)

    tdir = archive_dir / target["id"]
    latest_path = tdir / "latest.json"
    prev = read_json_or_none(latest_path)
    changed = prev is None or _compact(prev.get("signature")) != _compact(signature)
    snapshot = {
        "target": target["id"],
        "title": target.get("title") or target["id"],
        "url": target["url"],
        "checked_at": iso_now(),
        "signature": signature,
    }
    if write:
        write_json_atomic(tdir / f"{today}.json", snapshot)
        if changed:
            write_json_atomic(latest_path, snapshot)
            entry = {"at": snapshot["checked_at"], "target": target["id"],
                     "from": prev.get("signature") if prev else None, "to": signature}
            with (tdir / "changes.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(_compact(entry) + "\n")
    return {"changed": changed, "signature": signature, "snapshot": snapshot}


def run_patrol(targets: list[dict], archive_dir: Path, *, today: str | None = None,
               fetch: Callable[[str], str] | None = None, dry_run: bool = False,
               force: bool = False, max_attempts: int = MAX_ATTEMPTS,
               retry_base_s: float = RETRY_BASE_S,
               sleep: Callable[[float], None] | None = None,
               log: Callable[[str], None] = print) -> dict:
    """巡全部目标。返回 {results: {id: status}, failures: [id], changes: [id]}。"""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetch = fetch or fetch_text
    sleep = sleep or time.sleep
    state_path = archive_dir / STATE_REL
    state = {"targets": {}} if dry_run else load_state(state_path)

    results: dict[str, str] = {}
    failures: list[str] = []
    changes: list[str] = []
    for target in targets:
        tid = target["id"]
        rec = state["targets"].setdefault(tid, {})
        if not dry_run and not force and rec.get("last_success_date") == today:
            log(f"[store-patrol] skip {tid}（今日 {today} 已成功）")
            results[tid] = "skipped"
            continue
        last_err = ""
        attempts = 0
        outcome = None
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                outcome = patrol_target(target, archive_dir, today, fetch, write=not dry_run)
                break
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log(f"[store-patrol] attempt {attempt} {tid}: error — {last_err}")
                if attempt < max_attempts:
                    sleep(min(retry_base_s * 2 ** (attempt - 1), 30.0))
        now = iso_now()
        rec["last_attempt_at"] = now
        rec["last_attempts"] = attempts
        if outcome is not None:
            rec.update(last_status="ok", last_success_at=now, last_success_date=today,
                       consecutive_failures=0, last_error=None)
            results[tid] = "changed" if outcome["changed"] else "unchanged"
            if outcome["changed"]:
                changes.append(tid)
            tag = "CHANGED" if outcome["changed"] else "unchanged"
            log(f"[store-patrol] {tid}: {tag} {_compact(outcome['signature'])}")
        else:
            rec.update(last_status="failed", last_error=last_err,
                       consecutive_failures=int(rec.get("consecutive_failures") or 0) + 1)
            results[tid] = "failed"
            failures.append(tid)

    if not dry_run:
        # 注册表里已移除的目标不保留陈旧状态。
        ids = {t["id"] for t in targets}
        state["targets"] = {k: v for k, v in state["targets"].items() if k in ids}
        state["schema"] = 1
        state["updated_at"] = iso_now()
        write_json_atomic(state_path, {"schema": state["schema"], "updated_at": state["updated_at"],
                                       "targets": state["targets"]})
    return {"results": results, "failures": failures, "changes": changes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="忘却前夜店面日巡检（Steam 店面 / 评测摘要）")
    ap.add_argument("--targets", type=Path, default=DEFAULT_TARGETS, help="目标注册表 JSON")
    ap.add_argument("--out", type=Path, default=DEFAULT_ARCHIVE, help="档案根目录（默认仓内 Record/store-patrol）")
    ap.add_argument("--dry-run", action="store_true", help="只拉取 + 打印签名，不写任何文件")
    ap.add_argument("--force", action="store_true", help="今日已成功的目标也重巡")
    ap.add_argument("--today", help="覆盖 UTC 日期标签（YYYY-MM-DD），测试 / 补跑用")
    args = ap.parse_args(argv)

    config = json.loads(args.targets.read_text(encoding="utf-8"))
    report = run_patrol(config["targets"], args.out, today=args.today,
                        dry_run=args.dry_run, force=args.force)
    n = len(report["results"])
    if report["failures"]:
        print(f"[store-patrol] {len(report['failures'])}/{n} target(s) failed: "
              f"{', '.join(report['failures'])}", file=sys.stderr)
        return 1
    print(f"[store-patrol] all {n} targets patrolled, {len(report['changes'])} change(s)"
          + ("（dry-run，未写文件）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
