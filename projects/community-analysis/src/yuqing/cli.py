"""命令行入口：`yuqing <子命令>`。未实现的子命令只登记空壳，并指向负责的工单。"""

from __future__ import annotations

import argparse
import sys

from yuqing.config import ConfigError, load_config

# 子命令 → (说明, 负责工单)。空壳按此登记；实现后在 _REAL 里接上真处理函数。
SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "census": ("数据普查：量出数据湖的真实形状", "T01"),
    "normalize": ("规范化：各平台原文读成 messages 表", "T10"),
    "clean": ("清洗打标：写 msg_flags，只打标不删", "T11"),
    "unitize": ("切段：把聊天切成阅读单元", "T12"),
    "llm": ("模型网关适配器（含 ping）", "T20"),
    "annotate": ("标注流水线：单元送标，写出 points", "T22"),
    "gold": ("金标集工具：抽样、标注页、导入、评测", "T23"),
    "query": ("查询层：按任意维度切片", "T30"),
    "mcp": ("只读 MCP 工具服务", "T32"),
    "run-incremental": ("增量运行：新数据从原文走到可查询", "T33"),
    "issues": ("议题层：存储、归属、发现、收口", "T40"),
    "ops": ("运营聚合接入", "T50"),
    "ledgers": ("裁定台账与预判台账", "T52"),
}


def _add_census_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lake", help="数据湖根目录（默认取环境变量 LAKE_ROOT，未设则用本仓 Record/Community）")
    p.add_argument("--out", help="报告目录（默认 DATA_ROOT/reports/census/<UTC+8 日期>/）")
    p.add_argument("--sample", type=int, default=None, help="每个平台抽样条数（默认 1000）")


def _run_census(args: argparse.Namespace) -> int:
    from yuqing.census.run import run_census_cli

    return run_census_cli(args)


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="llm_cmd", metavar="<动作>", required=True)
    ping = sub.add_parser("ping", help="连真网关发一次最小请求（人工验收用，不进自动测试）")
    ping.add_argument("--model", help="模型名（默认取环境变量 MODEL_LOW）")


def _run_llm(args: argparse.Namespace) -> int:
    from yuqing.llm.cli import run_ping

    return run_ping(args)


def _add_normalize_args(p: argparse.ArgumentParser) -> None:
    from yuqing.normalize.run import add_args

    add_args(p)


def _run_normalize(args: argparse.Namespace) -> int:
    from yuqing.normalize.run import run_cli

    return run_cli(args)


def _add_clean_args(p: argparse.ArgumentParser) -> None:
    del p  # 无参数：读 DATA_ROOT/messages，阈值取 config/default.toml 的 [clean]


def _run_clean(args: argparse.Namespace) -> int:
    from yuqing.normalize.clean import run_cli

    return run_cli(args)


def _add_unitize_args(p: argparse.ArgumentParser) -> None:
    from yuqing.unitize.run import add_args

    add_args(p)


def _run_unitize(args: argparse.Namespace) -> int:
    from yuqing.unitize.run import run_cli

    return run_cli(args)


_ARGS = {
    "census": _add_census_args,
    "llm": _add_llm_args,
    "normalize": _add_normalize_args,
    "clean": _add_clean_args,
    "unitize": _add_unitize_args,
}
_REAL = {
    "census": _run_census,
    "llm": _run_llm,
    "normalize": _run_normalize,
    "clean": _run_clean,
    "unitize": _run_unitize,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yuqing", description="玩家舆情分析系统")
    sub = parser.add_subparsers(dest="command", metavar="<子命令>")
    for name, (desc, task) in SUBCOMMANDS.items():
        p = sub.add_parser(name, help=f"{desc}（{task}）", description=desc)
        if name in _ARGS:
            _ARGS[name](p)
        else:
            p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        load_config()  # 启动检查：参数文件可读、DATA_ROOT（若设）在仓库外
        handler = _REAL.get(args.command)
        if handler is None:
            _, task = SUBCOMMANDS[args.command]
            print(f"「{args.command}」尚未实现，见工单 {task}。", file=sys.stderr)
            return 2
        return handler(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
