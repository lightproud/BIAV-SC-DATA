"""运行台账 runs：逐次调用一行，运行结束一行汇总，外加收尾报告。只追加，不记密钥，默认不记正文。

落盘布局（DATA_ROOT/runs/）：
- calls/<day_cn>/<run_id>.jsonl  每次调用一行（模型、token、是否估算、耗时、是否重试、输入哈希）
- runs.jsonl                     每次运行一行（数据契约 runs 的字段）
- reports/<run_id>.json          收尾报告（含停止原因）
root 为 None 时只记在内存（测试与预演用）。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from yuqing.census.fields import day_cn


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RunLedger:
    def __init__(self, root: Path | None):
        self.root = Path(root) if root is not None else None
        self.calls: list[dict] = []
        self.runs: list[dict] = []
        self._lock = threading.Lock()

    def _append(self, path: Path, row: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def record_call(self, row: dict, now: datetime) -> None:
        row = {**row, "ts_utc": _iso(now), "day_cn": day_cn(now)}
        with self._lock:
            self.calls.append(row)
            if self.root is not None:
                self._append(self.root / "calls" / row["day_cn"] / f"{row['run_id']}.jsonl", row)

    def day_tokens(self, day: str) -> int:
        """当日（UTC+8）全部运行已用的 token，跨进程累计（读落盘的调用行）。"""
        with self._lock:
            if self.root is None:
                rows = [r for r in self.calls if r["day_cn"] == day]
            else:
                rows = []
                for f in sorted((self.root / "calls" / day).glob("*.jsonl")):
                    with f.open(encoding="utf-8") as fh:
                        rows.extend(json.loads(line) for line in fh if line.strip())
            return sum(int(r["tokens_in"]) + int(r["tokens_out"]) for r in rows if r.get("status") == "ok")

    def finish_run(self, row: dict, report: dict) -> None:
        with self._lock:
            self.runs.append(row)
            if self.root is not None:
                self._append(self.root / "runs.jsonl", row)
                rp = self.root / "reports" / f"{row['run_id']}.json"
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


iso_utc = _iso
