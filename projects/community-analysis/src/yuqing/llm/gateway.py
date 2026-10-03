"""Gateway：唯一的模型调用入口。重试、超时、限流、并发、记账、预算都在这里，上层只调 complete()。"""

from __future__ import annotations

import random
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

from yuqing.census.fields import day_cn
from yuqing.llm.base import Backend, BackendError, Completion, LLMError, TransientError, Usage, estimate_tokens
from yuqing.llm.base import input_hash as _input_hash
from yuqing.llm.budget import Budget, BudgetExceeded
from yuqing.llm.ledger import RunLedger, iso_utc


@dataclass(frozen=True)
class GatewaySettings:
    protocol: str = "openai"  # openai（OpenAI 兼容 chat completions）或 anthropic（Messages）
    timeout_s: float = 60.0
    max_retries: int = 4  # 429 / 5xx / 超时的重试次数（不含首发）
    backoff_base_s: float = 1.0
    backoff_max_s: float = 30.0
    max_concurrency: int = 4
    rpm: int = 60  # 每分钟请求上限；0 不限
    max_tokens: int = 4096
    log_bodies: bool = False  # 默认不记正文，只记输入哈希

    @classmethod
    def from_params(cls, params: dict) -> GatewaySettings:
        llm = params.get("llm", {})
        kw = {f.name: type(f.default)(llm[f.name]) for f in fields(cls) if f.name in llm}
        return cls(**kw)


class RateLimiter:
    """滑动 60 秒窗口的每分钟请求上限。"""

    def __init__(self, rpm: int, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.rpm = rpm
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.rpm <= 0:
            return
        while True:
            with self._lock:
                now = self._clock()
                while self._sent and now - self._sent[0] >= 60.0:
                    self._sent.popleft()
                if len(self._sent) < self.rpm:
                    self._sent.append(now)
                    return
                wait = 60.0 - (now - self._sent[0])
            self._sleep(max(wait, 0.001))


def new_run_id(now: datetime) -> str:
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


class Gateway:
    def __init__(
        self,
        backend: Backend,
        ledger: RunLedger | None = None,
        budget: Budget | None = None,
        settings: GatewaySettings | None = None,
        kind: str = "adhoc",
        run_id: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        seed: int = 1,
    ):
        self.backend = backend
        self.ledger = ledger or RunLedger(None)
        self.budget = budget or Budget()
        self.settings = settings or GatewaySettings()
        self.kind = kind
        self._sleep = sleep
        self._now = now
        self._rng = random.Random(seed)  # 退避抖动用固定种子
        self._sem = threading.Semaphore(max(1, self.settings.max_concurrency))
        self._limiter = RateLimiter(self.settings.rpm, clock, sleep)
        self._lock = threading.Lock()
        if backend.is_real:
            self.budget.require_set()  # 真模型：预算没设就在开跑前拒绝
        started = now()
        self.run_id = run_id or new_run_id(started)
        self.started = started
        self.models: set[str] = set()
        self.calls = 0
        self.failed_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.estimated = False
        self.stop_reason: str | None = None
        self.closed = False
        self._day = day_cn(started)
        self._day_base = self.ledger.day_tokens(self._day)  # 开跑时当日已用（含别的运行）
        self._day_own = 0  # 本运行在当日新增的量

    # -- 预算 --------------------------------------------------------------
    @property
    def run_used(self) -> int:
        return self.tokens_in + self.tokens_out

    def _day_used(self) -> int:
        today = day_cn(self._now())
        if today != self._day:  # 跨日：当日基数重读
            self._day, self._day_base, self._day_own = today, self.ledger.day_tokens(today), 0
        return self._day_base + self._day_own

    def _check_budget(self, incoming: int) -> None:
        if not self.backend.is_real:
            return
        with self._lock:
            if self.stop_reason:
                raise BudgetExceeded(self.stop_reason)
            try:
                self.budget.check(self.run_used, self._day_used(), incoming)
            except BudgetExceeded as exc:
                self._stop(str(exc))
                raise

    def _stop(self, reason: str) -> None:
        """到额即停：记下原因并写收尾报告（只写一次）。调用方需持有 _lock。"""
        self.stop_reason = reason
        self._close_locked("budget_exhausted")

    # -- 调用 --------------------------------------------------------------
    def complete(self, system: str, user: str, model: str, json_schema: dict | None = None) -> Completion:
        """调一次模型，返回文本与用量。system 放固定部分（逐字节稳定，便于网关缓存），user 放变化部分。"""
        if self.closed and not self.stop_reason:
            raise LLMError("本次运行已收尾，不能再调用")
        if not model:
            raise LLMError("缺少模型名（MODEL_LOW / MODEL_MID）")
        est_in = estimate_tokens(system) + estimate_tokens(user)
        self._check_budget(est_in)
        key = _input_hash(system, user, model, json_schema)
        attempts = 0
        t0 = time.monotonic()
        status = "ok"
        result: Completion | None = None
        error: Exception | None = None
        while True:
            attempts += 1
            self._limiter.acquire()
            try:
                with self._sem:
                    result = self.backend.send(system, user, model, json_schema, self.settings.timeout_s)
                break
            except TransientError as exc:
                if attempts > self.settings.max_retries:
                    status, error = "failed", exc
                    break
                self._sleep(self._backoff(attempts, exc.retry_after))
            except (BackendError, LLMError) as exc:
                status, error = "failed", exc
                break
        latency = int((time.monotonic() - t0) * 1000)
        if result is not None:
            usage = result.usage or Usage(est_in, estimate_tokens(result.text), estimated=True)
            result = Completion(result.text, usage, result.model or model, result.latency_ms or latency, attempts)
        else:
            usage = Usage(0, 0, estimated=False)
        self._account(key, model, usage, latency, attempts, status, error, system, user, result)
        if error is not None:
            raise error
        assert result is not None
        return result

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """带抖动的指数退避：上限 base·2^(n-1)（封顶 backoff_max_s），取上限的一半到全额之间的随机值。

        网关给了 Retry-After 就不少于它。
        """
        s = self.settings
        cap = min(s.backoff_max_s, s.backoff_base_s * (2 ** (attempt - 1)))
        delay = cap / 2 + self._rng.random() * cap / 2
        if retry_after is not None:
            delay = max(delay, min(retry_after, s.backoff_max_s))
        return delay

    def _account(self, key, model, usage, latency, attempts, status, error, system, user, result) -> None:
        row = {
            "run_id": self.run_id,
            "kind": self.kind,
            "backend": self.backend.name,
            "model": model,
            "input_hash": key,
            "tokens_in": usage.tokens_in,
            "tokens_out": usage.tokens_out,
            "estimated": usage.estimated,
            "latency_ms": latency,
            "attempts": attempts,
            "retried": attempts > 1,
            "status": status,
            "error": type(error).__name__ if error else None,
        }
        if self.settings.log_bodies:
            row["system"], row["user"] = system, user
            row["output"] = result.text if result else None
        with self._lock:
            self.calls += 1
            self.models.add(model)
            if status == "ok":
                self.tokens_in += usage.tokens_in
                self.tokens_out += usage.tokens_out
                self.estimated = self.estimated or usage.estimated
                self._day_own += usage.tokens_in + usage.tokens_out
            else:
                self.failed_calls += 1
            self.ledger.record_call(row, self._now())
            if self.backend.is_real and not self.stop_reason and self.budget.exhausted(self.run_used, self._day_used()):
                self._stop(f"预算到额：本次运行已用 {self.run_used} token")

    # -- 收尾 --------------------------------------------------------------
    def summary(self, status: str) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "status": status,
            "started_utc": iso_utc(self.started),
            "finished_utc": iso_utc(self._now()),
            "model": ",".join(sorted(self.models)),
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "estimated": self.estimated,
        }

    def _close_locked(self, status: str) -> dict:
        if self.closed:
            return self.ledger.runs[-1] if self.ledger.runs else {}
        self.closed = True
        row = self.summary(status)
        report = {
            **row,
            "stop_reason": self.stop_reason,
            "budget": {"tokens_per_run": self.budget.tokens_per_run, "tokens_per_day": self.budget.tokens_per_day},
        }
        self.ledger.finish_run(row, report)
        return row

    def close(self, status: str = "ok") -> dict:
        with self._lock:
            return self._close_locked(status)

    def __enter__(self) -> Gateway:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close("ok" if exc_type is None else "failed")


def build_gateway(cfg, kind: str, backend: Backend | None = None, **kw) -> Gateway:
    """按配置组装：协议取 llm.protocol，地址与密钥取环境变量，台账写 DATA_ROOT/runs。"""
    from yuqing.llm.http import BACKENDS

    settings = GatewaySettings.from_params(cfg.params)
    if backend is None:
        cls = BACKENDS.get(settings.protocol)
        if cls is None:
            raise LLMError(f"不认识的协议 llm.protocol={settings.protocol!r}（可选：{'、'.join(BACKENDS)}）")
        env = cfg.require("GATEWAY_BASE_URL", "GATEWAY_API_KEY")
        backend = cls(env["GATEWAY_BASE_URL"], env["GATEWAY_API_KEY"], max_tokens=settings.max_tokens)
    root: Path | None = cfg.data_root() / "runs" if backend.is_real or "DATA_ROOT" in cfg.env else None
    return Gateway(backend, RunLedger(root), Budget.from_params(cfg.params), settings, kind=kind, **kw)
