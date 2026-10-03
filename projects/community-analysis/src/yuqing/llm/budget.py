"""预算：budget.* 未设置（为 0）时拒绝调用真模型；到额即停。计量口径 = 输入 + 输出 token。"""

from __future__ import annotations

from dataclasses import dataclass

from yuqing.llm.base import LLMError


class BudgetNotSet(LLMError):
    """预算没设：不调真模型。"""


class BudgetExceeded(LLMError):
    """预算到额：本次运行停止。"""


@dataclass(frozen=True)
class Budget:
    tokens_per_run: int = 0
    tokens_per_day: int = 0

    @classmethod
    def from_params(cls, params: dict) -> Budget:
        b = params.get("budget", {})
        return cls(int(b.get("tokens_per_run", 0)), int(b.get("tokens_per_day", 0)))

    def require_set(self) -> None:
        missing = [k for k in ("tokens_per_run", "tokens_per_day") if getattr(self, k) <= 0]
        if missing:
            names = "、".join(f"budget.{k}（环境变量 YUQING_BUDGET_{k.upper()}）" for k in missing)
            raise BudgetNotSet(f"预算未设置，拒绝调用真模型：{names}")

    def check(self, run_used: int, day_used: int, incoming: int = 0) -> None:
        """调用前检查：已用量加上本次输入的粗估超过任一额度即拒绝。额度为 0 视为未设置、不在此处拦。"""
        if self.tokens_per_run and run_used + incoming > self.tokens_per_run:
            raise BudgetExceeded(f"本次运行预算到额：已用 {run_used}，额度 {self.tokens_per_run}")
        if self.tokens_per_day and day_used + incoming > self.tokens_per_day:
            raise BudgetExceeded(f"当日预算到额：已用 {day_used}，额度 {self.tokens_per_day}")

    def exhausted(self, run_used: int, day_used: int) -> bool:
        return bool(
            (self.tokens_per_run and run_used >= self.tokens_per_run)
            or (self.tokens_per_day and day_used >= self.tokens_per_day)
        )
