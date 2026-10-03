"""后端协议与公共类型。上层只认 Completion，不认任何网关的原始响应。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from yuqing.census.fields import script_profile


class LLMError(RuntimeError):
    """模型调用失败的总类。"""


class TransientError(LLMError):
    """可重试：429、5xx、超时、连接中断。retry_after 为网关建议的等待秒数（可空）。"""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class BackendError(LLMError):
    """不可重试：4xx（429 除外）、响应格式不对。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    estimated: bool  # 网关没返回用量、由本地粗估时为真


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage | None  # 后端给不出用量时为空，由 Gateway 粗估补上并标 estimated
    model: str
    latency_ms: int = 0
    attempts: int = 1

    @property
    def retried(self) -> bool:
        return self.attempts > 1


class Backend(Protocol):
    """一种网关协议。send 只发一次，不重试；重试、限流、记账、预算都在 Gateway。"""

    name: str
    is_real: bool  # 真模型才受预算约束；FakeLLM 为假

    def send(self, system: str, user: str, model: str, json_schema: dict | None, timeout: float) -> Completion: ...


def estimate_tokens(text: str) -> int:
    """与普查同一口径：汉字、假名、谚文按 1，其余每 4 个字符按 1。"""
    return script_profile(text)[1]


def input_hash(system: str, user: str, model: str, json_schema: dict | None = None) -> str:
    """一次调用输入的稳定哈希：记账只记它，不记正文；FakeLLM 按它查预置输出。"""
    payload = json.dumps(
        {"system": system, "user": user, "model": model, "schema": json_schema},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
