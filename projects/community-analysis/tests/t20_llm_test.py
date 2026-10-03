"""T20 模型网关适配器：重试、超时、记账、预算到额即停、FakeLLM 的确定性。不联网。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from yuqing import cli
from yuqing.config import load_config
from yuqing.llm import (
    Budget,
    BudgetExceeded,
    BudgetNotSet,
    Completion,
    FakeLLM,
    FakeMiss,
    Gateway,
    GatewaySettings,
    RunLedger,
    TransientError,
    build_gateway,
)
from yuqing.llm.base import BackendError, input_hash
from yuqing.llm.gateway import RateLimiter
from yuqing.llm.http import AnthropicBackend, OpenAICompatBackend

T0 = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
BIG = Budget(tokens_per_run=10_000, tokens_per_day=100_000)
FAST = GatewaySettings(max_retries=3, backoff_base_s=1.0, backoff_max_s=8.0, rpm=0)


class Clock:
    def __init__(self, start: datetime = T0):
        self.t = 0.0
        self.start = start
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def mono(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.t)


class Scripted:
    """按脚本依次抛错或返回的假真模型（is_real=True，受预算约束）。"""

    name = "scripted"
    is_real = True

    def __init__(self, script: list, tokens: tuple[int, int] | None = (100, 20)):
        self.script = list(script)
        self.tokens = tokens
        self.n = 0

    def send(self, system, user, model, json_schema, timeout):
        self.n += 1
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, Exception):
            raise step
        from yuqing.llm.base import Usage

        usage = Usage(*self.tokens, estimated=False) if self.tokens else None
        return Completion(text=f"reply-{self.n}", usage=usage, model=model)


def gw(backend, clock: Clock | None = None, ledger: RunLedger | None = None, budget=BIG, settings=FAST, **kw):
    clock = clock or Clock()
    return Gateway(
        backend, ledger or RunLedger(None), budget, settings,
        sleep=clock.sleep, clock=clock.mono, now=clock.now, run_id=kw.pop("run_id", "run-1"), **kw,
    )  # fmt: skip


# -- 重试 --------------------------------------------------------------------
def test_retries_429_and_5xx_with_jittered_backoff():
    clock = Clock()
    be = Scripted([TransientError("429", 429), TransientError("503", 503), TransientError("502", 502)])
    g = gw(be, clock)
    res = g.complete("sys", "user", "m-low")
    assert res.text == "reply-4" and res.attempts == 4 and res.retried
    assert len(clock.sleeps) == 3
    for n, s in enumerate(clock.sleeps, start=1):  # 第 n 次退避落在 [cap/2, cap]
        cap = min(8.0, 2 ** (n - 1))
        assert cap / 2 <= s <= cap
    assert g.ledger.calls[-1]["retried"] is True and g.ledger.calls[-1]["attempts"] == 4


def test_backoff_jitter_is_seeded():
    def delays():
        clock = Clock()
        gw(Scripted([TransientError("x", 500)] * 3), clock).complete("s", "u", "m")
        return clock.sleeps

    assert delays() == delays()


def test_retry_after_is_respected():
    clock = Clock()
    gw(Scripted([TransientError("429", 429, retry_after=5.0)]), clock).complete("s", "u", "m")
    assert clock.sleeps[0] >= 5.0


def test_gives_up_after_max_retries_and_records_failure():
    be = Scripted([TransientError("500", 500)] * 10)
    g = gw(be)
    with pytest.raises(TransientError):
        g.complete("s", "u", "m")
    assert be.n == FAST.max_retries + 1
    row = g.ledger.calls[-1]
    assert row["status"] == "failed" and row["attempts"] == 4 and row["tokens_in"] == 0


def test_4xx_is_not_retried():
    be = Scripted([BackendError("400", 400)])
    with pytest.raises(BackendError):
        gw(be).complete("s", "u", "m")
    assert be.n == 1


# -- 超时（走真实的 httpx 栈，传输层模拟）------------------------------------
def _openai_ok(model="m"):
    body = {
        "model": model,
        "choices": [{"message": {"content": "pong"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 1},
    }
    return httpx.Response(200, json=body)


def test_timeout_is_passed_and_retried():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.extensions["timeout"]["read"])
        if len(seen) < 3:
            raise httpx.ReadTimeout("slow", request=req)
        return _openai_ok()

    be = OpenAICompatBackend("http://gw.test/v1", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    clock = Clock()
    res = gw(be, clock, settings=GatewaySettings(timeout_s=12.5, max_retries=3, rpm=0)).complete("s", "u", "m")
    assert res.text == "pong" and res.attempts == 3
    assert seen == [12.5, 12.5, 12.5]


def test_timeout_exhausted_raises_transient():
    def handler(req):
        raise httpx.ConnectTimeout("slow", request=req)

    be = OpenAICompatBackend("http://gw.test/v1", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(TransientError, match="超时"):
        gw(be, settings=GatewaySettings(max_retries=1, rpm=0)).complete("s", "u", "m")


def test_http_status_mapping():
    codes = iter([429, 503, 200])

    def handler(req):
        c = next(codes)
        return _openai_ok() if c == 200 else httpx.Response(c, headers={"retry-after": "2"})

    be = OpenAICompatBackend("http://gw.test/v1", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    clock = Clock()
    assert gw(be, clock).complete("s", "u", "m").attempts == 3
    assert all(s >= 2 for s in clock.sleeps)

    be401 = OpenAICompatBackend(
        "http://gw.test/v1", "k", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    )
    with pytest.raises(BackendError):
        gw(be401).complete("s", "u", "m")


# -- 两种协议的请求与解析 -------------------------------------------------------
def test_openai_payload_and_usage():
    captured = {}

    def handler(req: httpx.Request):
        captured["url"] = str(req.url)
        captured["auth"] = req.headers["authorization"]
        captured["body"] = json.loads(req.content)
        return _openai_ok()

    be = OpenAICompatBackend("http://gw.test/v1/", "sk-x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = gw(be).complete("FIXED", "VAR", "m", json_schema={"type": "object"})
    assert captured["url"] == "http://gw.test/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-x"
    msgs = captured["body"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"] and msgs[0]["content"] == "FIXED"
    assert captured["body"]["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert (res.usage.tokens_in, res.usage.tokens_out, res.usage.estimated) == (7, 1, False)


def test_anthropic_payload_tool_json_and_cache_usage():
    captured = {}

    def handler(req: httpx.Request):
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.content)
        body = {
            "model": "claude-x",
            "content": [{"type": "tool_use", "name": "emit", "input": {"a": 1}}],
            "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 90},
        }
        return httpx.Response(200, json=body)

    be = AnthropicBackend("http://gw.test", "ak", client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = gw(be).complete("FIXED", "VAR", "claude-x", json_schema={"type": "object"})
    assert captured["headers"]["x-api-key"] == "ak"
    body = captured["body"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tool_choice"] == {"type": "tool", "name": "emit"}
    assert json.loads(res.text) == {"a": 1}
    assert (res.usage.tokens_in, res.usage.tokens_out) == (100, 4)


def test_malformed_response_is_backend_error():
    be = OpenAICompatBackend(
        "http://gw.test/v1",
        "k",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    with pytest.raises(BackendError, match="格式"):
        gw(be).complete("s", "u", "m")


# -- 记账 --------------------------------------------------------------------
def test_ledger_rows_on_disk_and_totals(tmp_path):
    ledger = RunLedger(tmp_path / "runs")
    clock = Clock()
    g = gw(Scripted([TransientError("x", 500)]), clock, ledger, kind="annotate", run_id="r-1")
    for i in range(3):
        g.complete("sys", f"user {i}", "m-low")
    row = g.close()
    day = "2026-10-03"
    lines = (tmp_path / "runs" / "calls" / day / "r-1.jsonl").read_text(encoding="utf-8").splitlines()
    calls = [json.loads(x) for x in lines]
    assert len(calls) == 3
    assert sum(c["tokens_in"] for c in calls) == row["tokens_in"] == 300
    assert sum(c["tokens_out"] for c in calls) == row["tokens_out"] == 60
    assert [c["retried"] for c in calls] == [True, False, False]
    assert all(c["latency_ms"] >= 0 and c["model"] == "m-low" for c in calls)
    assert calls[0]["input_hash"] == input_hash("sys", "user 0", "m-low")
    runs = [json.loads(x) for x in (tmp_path / "runs" / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert runs == [row]
    assert row["kind"] == "annotate" and row["calls"] == 3 and row["status"] == "ok"
    assert (tmp_path / "runs" / "reports" / "r-1.json").is_file()


def test_ledger_never_holds_bodies_or_key(tmp_path, monkeypatch):
    def handler(req):
        return _openai_ok()

    be = OpenAICompatBackend(
        "http://gw.test/v1", "sk-SECRET", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    g = gw(be, ledger=RunLedger(tmp_path))
    g.complete("SYSTEM-BODY", "PLAYER-TEXT", "m")
    g.close()
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file())
    assert "sk-SECRET" not in blob and "PLAYER-TEXT" not in blob and "SYSTEM-BODY" not in blob


def test_missing_usage_is_estimated_and_marked():
    g = gw(Scripted([], tokens=None))
    res = g.complete("系统提示", "abcdefgh", "m")
    assert res.usage.estimated is True
    assert res.usage.tokens_in == 4 + 2  # 4 个汉字 + 8 个拉丁字符 / 4
    assert g.ledger.calls[-1]["estimated"] is True
    assert g.close()["estimated"] is True


# -- 预算 --------------------------------------------------------------------
def test_real_backend_refused_without_budget():
    with pytest.raises(BudgetNotSet, match="YUQING_BUDGET_TOKENS_PER_RUN"):
        gw(Scripted([]), budget=Budget())
    with pytest.raises(BudgetNotSet, match="YUQING_BUDGET_TOKENS_PER_DAY"):
        gw(Scripted([]), budget=Budget(tokens_per_run=100))


def test_fake_backend_needs_no_budget():
    fake = FakeLLM(default=lambda s, u, m: "ok")
    assert gw(fake, budget=Budget()).complete("s", "u", "m").text == "ok"


def test_budget_stops_at_limit_and_writes_report(tmp_path):
    be = Scripted([], tokens=(100, 20))  # 每次 120
    g = gw(be, ledger=RunLedger(tmp_path), budget=Budget(tokens_per_run=300, tokens_per_day=10_000), run_id="r-b")
    g.complete("s", "u", "m")
    g.complete("s", "u", "m")  # 已用 240
    g.complete("s", "u", "m")  # 已用 360 ≥ 300：这次照常返回，随后停
    assert g.stop_reason and g.closed
    with pytest.raises(BudgetExceeded):
        g.complete("s", "u", "m")
    assert be.n == 3
    report = json.loads((tmp_path / "reports" / "r-b.json").read_text(encoding="utf-8"))
    assert report["status"] == "budget_exhausted" and report["tokens_in"] + report["tokens_out"] == 360
    assert report["stop_reason"] and report["budget"]["tokens_per_run"] == 300
    runs = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(runs) == 1  # 之后再 close 不重复写
    g.close()
    assert len((tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_budget_precheck_refuses_call_that_would_overrun():
    be = Scripted([])
    g = gw(be, budget=Budget(tokens_per_run=5, tokens_per_day=10_000))
    with pytest.raises(BudgetExceeded):
        g.complete("s", "x" * 100, "m")  # 输入粗估 25 > 5：不发
    assert be.n == 0 and g.close()["status"] == "budget_exhausted"


def test_daily_budget_counts_earlier_runs(tmp_path):
    ledger = RunLedger(tmp_path)
    b = Budget(tokens_per_run=10_000, tokens_per_day=250)
    g1 = gw(Scripted([]), ledger=ledger, budget=b, run_id="r-a")
    g1.complete("s", "u", "m")
    g1.complete("s", "u", "m")  # 当日 240
    g1.close()
    g2 = gw(Scripted([]), ledger=RunLedger(tmp_path), budget=b, run_id="r-b")
    g2.complete("s", "u", "m")  # 当日 360 ≥ 250：停
    with pytest.raises(BudgetExceeded, match="到额"):
        g2.complete("s", "u", "m")


# -- FakeLLM -----------------------------------------------------------------
def test_fake_llm_is_deterministic_by_input_hash():
    fake = FakeLLM()
    fake.add("sys", "u1", "m", '{"a":1}')
    fake.add("sys", "u2", "m", '{"a":2}')
    g = gw(fake)
    assert g.complete("sys", "u1", "m").text == '{"a":1}'
    assert g.complete("sys", "u2", "m").text == '{"a":2}'
    assert g.complete("sys", "u1", "m").text == '{"a":1}'
    assert fake.calls[0] == fake.calls[2] != fake.calls[1]
    with pytest.raises(FakeMiss):
        g.complete("sys", "u3", "m")


def test_fake_llm_hash_includes_model_and_schema():
    fake = FakeLLM()
    fake.add("s", "u", "m1", "A")
    fake.add("s", "u", "m1", "B", json_schema={"type": "object"})
    g = gw(fake)
    assert g.complete("s", "u", "m1").text == "A"
    assert g.complete("s", "u", "m1", json_schema={"type": "object"}).text == "B"
    with pytest.raises(FakeMiss):
        g.complete("s", "u", "m2")


# -- 限流与配置 -----------------------------------------------------------------
def test_rate_limiter_waits_when_window_full():
    clock = Clock()
    rl = RateLimiter(2, clock.mono, clock.sleep)
    rl.acquire()
    rl.acquire()
    rl.acquire()  # 第三个要等到窗口滑出
    assert clock.sleeps and clock.t >= 60.0


def test_settings_from_default_toml():
    cfg = load_config(environ={"YUQING_LLM_PROTOCOL": "anthropic", "YUQING_LLM_RPM": "30"})
    s = GatewaySettings.from_params(cfg.params)
    assert s.protocol == "anthropic" and s.rpm == 30 and s.log_bodies is False and s.timeout_s == 60.0


def test_build_gateway_requires_env_and_budget(tmp_path):
    cfg = load_config(environ={"DATA_ROOT": str(tmp_path)})
    with pytest.raises(Exception, match="GATEWAY_BASE_URL"):
        build_gateway(cfg, kind="ping")
    cfg = load_config(environ={"DATA_ROOT": str(tmp_path), "GATEWAY_BASE_URL": "http://gw", "GATEWAY_API_KEY": "k"})
    with pytest.raises(BudgetNotSet):
        build_gateway(cfg, kind="ping")


def test_cli_registers_llm_ping():
    args = cli.build_parser().parse_args(["llm", "ping", "--model", "x"])
    assert args.llm_cmd == "ping" and args.model == "x"


def test_concurrency_cap():
    import threading
    import time as _time

    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    class Slow(Scripted):
        def send(self, *a):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            _time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return super().send(*a)

    g = Gateway(Slow([]), RunLedger(None), BIG, GatewaySettings(max_concurrency=2, rpm=0))
    ts = [threading.Thread(target=g.complete, args=("s", f"u{i}", "m")) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert state["max"] <= 2 and g.calls == 6
