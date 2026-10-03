"""`yuqing llm ping`：连真网关发一次最小请求，打印用量与耗时。受预算约束，台账照记。"""

from __future__ import annotations

import argparse
import sys

from yuqing.config import load_config
from yuqing.llm.base import LLMError
from yuqing.llm.gateway import build_gateway

PING_SYSTEM = "You are a connectivity check. Reply with the single word: pong"
PING_USER = "ping"


def run_ping(args: argparse.Namespace) -> int:
    cfg = load_config()
    model = args.model or cfg.env.get("MODEL_LOW") or cfg.require("MODEL_LOW")["MODEL_LOW"]
    try:
        with build_gateway(cfg, kind="ping") as gw:
            res = gw.complete(PING_SYSTEM, PING_USER, model)
    except LLMError as exc:
        print(f"ping 失败：{exc}", file=sys.stderr)
        return 1
    u = res.usage
    mark = "（估算）" if u.estimated else ""
    print(f"ping 成功：模型 {res.model}，耗时 {res.latency_ms} 毫秒，尝试 {res.attempts} 次，")
    print(f"输入 {u.tokens_in} / 输出 {u.tokens_out} token{mark}，回复 {len(res.text)} 字符。运行编号 {gw.run_id}。")
    return 0
