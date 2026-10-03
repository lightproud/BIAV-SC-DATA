"""两种 HTTP 协议：OpenAI 兼容 chat completions（默认）与 Anthropic Messages。

只负责「发一次、把响应翻成 Completion」；状态码翻成 TransientError / BackendError 由 Gateway 决定是否重试。
地址、密钥、模型名都由调用方从环境变量传进来，这里不读环境。
"""

from __future__ import annotations

import json
import time

import httpx

from yuqing.llm.base import BackendError, Completion, TransientError, Usage


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


class _HTTPBackend:
    name = "http"
    is_real = True
    path = ""

    def __init__(self, base_url: str, api_key: str, max_tokens: int = 4096, client: httpx.Client | None = None):
        if not base_url:
            raise BackendError("缺少网关地址 GATEWAY_BASE_URL")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.max_tokens = max_tokens
        self._client = client or httpx.Client()

    def _headers(self) -> dict[str, str]:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _payload(self, system: str, user: str, model: str, json_schema: dict | None) -> dict:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, body: dict, model: str, json_schema: dict | None) -> tuple[str, Usage | None]:  # pragma: no cover
        raise NotImplementedError

    def send(self, system: str, user: str, model: str, json_schema: dict | None, timeout: float) -> Completion:
        url = self.base_url + self.path
        t0 = time.monotonic()
        try:
            resp = self._client.post(
                url, headers=self._headers(), json=self._payload(system, user, model, json_schema), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise TransientError(f"请求超时（{timeout} 秒）") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"连接失败：{type(exc).__name__}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"网关返回 {resp.status_code}", resp.status_code, _retry_after(resp))
        if resp.status_code >= 400:
            raise BackendError(f"网关返回 {resp.status_code}", resp.status_code)
        try:
            body = resp.json()
            text, usage = self._parse(body, model, json_schema)
        except (ValueError, KeyError, IndexError, TypeError, StopIteration) as exc:
            raise BackendError(f"响应格式不对：{type(exc).__name__}") from exc
        latency = int((time.monotonic() - t0) * 1000)
        return Completion(text=text, usage=usage, model=str(body.get("model") or model), latency_ms=latency)


class OpenAICompatBackend(_HTTPBackend):
    """OpenAI 兼容的 chat completions：system 在前（固定前缀便于网关前缀缓存），user 在后。"""

    name = "openai"
    path = "/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _payload(self, system: str, user: str, model: str, json_schema: dict | None) -> dict:
        payload: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema},
            }
        return payload

    def _parse(self, body: dict, model: str, json_schema: dict | None) -> tuple[str, Usage | None]:
        text = body["choices"][0]["message"]["content"] or ""
        u = body.get("usage") or {}
        usage = None
        if "prompt_tokens" in u and "completion_tokens" in u:
            usage = Usage(int(u["prompt_tokens"]), int(u["completion_tokens"]), estimated=False)
        return text, usage


class AnthropicBackend(_HTTPBackend):
    """Anthropic Messages：system 标 cache_control 让固定前缀走提示缓存；要 JSON 时用强制工具调用取结构化输出。"""

    name = "anthropic"
    path = "/v1/messages"
    _TOOL = "emit"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def _payload(self, system: str, user: str, model: str, json_schema: dict | None) -> dict:
        payload: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        if json_schema is not None:
            payload["tools"] = [{"name": self._TOOL, "description": "输出结果", "input_schema": json_schema}]
            payload["tool_choice"] = {"type": "tool", "name": self._TOOL}
        return payload

    def _parse(self, body: dict, model: str, json_schema: dict | None) -> tuple[str, Usage | None]:
        blocks = body["content"]
        if json_schema is not None:
            tool = next(b for b in blocks if b.get("type") == "tool_use")
            text = json.dumps(tool["input"], ensure_ascii=False)
        else:
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        u = body.get("usage") or {}
        usage = None
        if "input_tokens" in u and "output_tokens" in u:
            tin = int(u["input_tokens"]) + int(u.get("cache_read_input_tokens") or 0)
            tin += int(u.get("cache_creation_input_tokens") or 0)
            usage = Usage(tin, int(u["output_tokens"]), estimated=False)
        return text, usage


BACKENDS = {"openai": OpenAICompatBackend, "anthropic": AnthropicBackend}
