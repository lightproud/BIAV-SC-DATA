"""`yuqing census`：普查数据湖，写 census.json 与 census.html。终端只打印统计。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from yuqing.census.engine import Census
from yuqing.census.fields import CN_TZ
from yuqing.census.report import render_html
from yuqing.config import ConfigError, check_not_in_lake, load_config, repo_root
from yuqing.lake.layout import discover
from yuqing.lake.reader import LakeReader

DEFAULT_SAMPLE = 1000
DEFAULT_SEED = 1


def run_census(lake: Path, params: dict, sample_n: int = DEFAULT_SAMPLE, seed: int = DEFAULT_SEED) -> dict:
    lake = Path(lake)
    if not lake.is_dir():
        raise ConfigError(f"数据湖目录不存在：{lake}")
    found = discover(lake)
    census = Census(LakeReader(lake), params, sample_n=sample_n, seed=seed)
    census.run(found)
    return census.summary(found)


def write_outputs(result: dict, out_dir: Path, generated: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp = out_dir / "census.json"
    hp = out_dir / "census.html"
    jp.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hp.write_text(render_html(result, generated), encoding="utf-8")
    return jp, hp


def default_lake() -> Path | None:
    """代码与数据同在 BIAV-SC-DATA：未设 LAKE_ROOT 时取本仓的 Record/Community（存在才用）。"""
    cand = repo_root() / "Record" / "Community"
    return cand if cand.is_dir() else None


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def run_census_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    lake = Path(args.lake or cfg.env.get("LAKE_ROOT") or default_lake() or cfg.require("LAKE_ROOT")["LAKE_ROOT"])
    now = datetime.now(CN_TZ)
    out = Path(args.out) if args.out else cfg.data_root() / "reports" / "census" / now.date().isoformat()
    check_not_in_lake(out, "报告目录", [lake])
    if _within(out, lake):
        raise ConfigError(f"报告目录不能放进数据湖（原文只读）：{out}")
    result = run_census(lake, cfg.params, sample_n=args.sample or DEFAULT_SAMPLE)
    jp, hp = write_outputs(result, out, now.strftime("%Y-%m-%d %H:%M"))
    t = result["totals"]
    print(f"普查完成：{len(result['platforms'])} 个平台，{t['messages']:,} 条，聊天类占比 {t['chat_share']:.2%}，")
    print(f"清洗后留存 {t['retention']:.2%}，试切单元 {t['trial_units_total']:,}。")
    print(f"报告：{jp}\n      {hp}")
    return 0
