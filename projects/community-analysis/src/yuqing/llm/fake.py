"""FakeLLM：按输入哈希返回预置输出。测试一律用它，不联网。"""

from __future__ import annotations

from collections.abc import Callable

from yuqing.llm.base import Completion, Usage, estimate_tokens, input_hash


class FakeMiss(KeyError):
    """没有为这个输入预置输出；不编造。"""


class FakeLLM:
    name = "fake"
    is_real = False

    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        default: Callable[[str, str, str], str] | None = None,
        report_usage: bool = True,
    ):
        self._outputs = dict(outputs or {})
        self._default = default
        self._report_usage = report_usage
        self.calls: list[str] = []  # 收到过的输入哈希，按到达顺序

    def add(self, system: str, user: str, model: str, output: str, json_schema: dict | None = None) -> str:
        key = input_hash(system, user, model, json_schema)
        self._outputs[key] = output
        return key

    def send(self, system: str, user: str, model: str, json_schema: dict | None, timeout: float) -> Completion:
        key = input_hash(system, user, model, json_schema)
        self.calls.append(key)
        if key in self._outputs:
            text = self._outputs[key]
        elif self._default is not None:
            text = self._default(system, user, model)
        else:
            raise FakeMiss(f"FakeLLM 没有为输入 {key[:12]}… 预置输出")
        usage = None
        if self._report_usage:
            usage = Usage(estimate_tokens(system) + estimate_tokens(user), estimate_tokens(text), estimated=False)
        return Completion(text=text, usage=usage, model=model)
