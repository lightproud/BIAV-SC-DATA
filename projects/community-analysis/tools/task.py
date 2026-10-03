#!/usr/bin/env python3
"""取工单：从 docs/plan.html 的 <script id="plan" type="application/json"> 读方案。

python tools/task.py T12                    打印该工单纯文本
python tools/task.py --list                 列出全部编号与标题
python tools/task.py --export docs/tasks    每张工单写成一个 Txx.md
python tools/task.py --checklist docs/progress/checklist.md
                                            生成进度清单；已存在时只补新行，不覆盖已勾的
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "plan.html"
_PLAN_RE = re.compile(r'<script id="plan" type="application/json">(.*?)</script>', re.S)
_ITEM_RE = re.compile(r"^- \[([ xX])\] ([TGR]\d+)\b")
_WHEN_RE = re.compile(r"^([TGR]\d+) (之后|之前)$")


def load_plan(path: Path = PLAN) -> dict:
    m = _PLAN_RE.search(path.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f'{path} 里找不到 <script id="plan">')
    return json.loads(m.group(1))


def task_title(task: dict) -> str:
    return task["title"] + ("（可选）" if task.get("optional") else "")


def export_markdown(task: dict) -> str:
    head = "<!-- 由 tools/task.py 从 docs/plan.html 导出，不手改 -->\n"
    return f"{head}# {task['id']} {task_title(task)}\n\n```text\n{task['text']}\n```\n"


def _line_task(task: dict) -> str:
    return f"- [ ] {task['id']} {task_title(task)}"


def _line_gate(gid: str, gate: dict) -> str:
    return f"- [ ] {gid} 闸门 · {gate['title']}（{gate['when']}，人工）"


def _line_run(rid: str, run: dict) -> str:
    return f"- [ ] {rid} 全量运行 · {run['title']}（{run['when']}，由人触发）"


def checklist_layout(plan: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """按阶段分组的 (标题, [(编号, 默认行)])；闸门与全量运行按各自 when 插位。"""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for ph in plan["phases"]:
        items = [(tid, _line_task(plan["tasks"][tid])) for tid in ph["tasks"]]
        sections.append((f"第 {ph['id']} 阶段 · {ph['title']}", items))
    pending = [(g, _line_gate(g, v), v["when"]) for g, v in plan["gates"].items()]
    pending += [(r, _line_run(r, v), v["when"]) for r, v in plan["runs"].items()]
    while pending:
        placed = False
        for entry in list(pending):
            iid, line, when = entry
            m = _WHEN_RE.match(when)
            if not m:
                raise SystemExit(f"{iid} 的 when 无法解析：{when}")
            anchor, side = m.groups()
            for _, items in sections:
                ids = [i for i, _ in items]
                if anchor in ids:
                    pos = ids.index(anchor) + (1 if side == "之后" else 0)
                    items.insert(pos, (iid, line))
                    pending.remove(entry)
                    placed = True
                    break
        if not placed:
            raise SystemExit("闸门 / 运行的锚点找不到：" + "、".join(p[0] for p in pending))
    return sections


def render_checklist(plan: dict, existing: str = "") -> str:
    kept: dict[str, str] = {}
    extra: list[str] = []
    for line in existing.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            kept[m.group(2)] = line
        elif line.startswith("- "):
            extra.append(line)
    out = [
        "# 施工进度清单",
        "",
        "状态的唯一来源：每张工单一行，做完就勾，并写上提交号。由 `tools/task.py --checklist` 生成，"
        "再次生成只补新行、保留已勾的行。",
        "",
    ]
    for title, items in checklist_layout(plan):
        out += [f"## {title}", ""]
        out += [kept.get(iid, line) for iid, line in items]
        out.append("")
    if extra:
        out += ["## 其他（保留）", "", *extra, ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="取工单")
    ap.add_argument("task", nargs="?", help="工单编号，如 T12")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--export", metavar="DIR")
    ap.add_argument("--checklist", metavar="FILE")
    ap.add_argument("--plan", default=str(PLAN), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    plan = load_plan(Path(args.plan))
    tasks = plan["tasks"]
    if args.list:
        for t in tasks.values():
            print(f"{t['id']}\t{task_title(t)}")
    if args.export:
        out = Path(args.export)
        out.mkdir(parents=True, exist_ok=True)
        for t in tasks.values():
            (out / f"{t['id']}.md").write_text(export_markdown(t), encoding="utf-8")
        print(f"已导出 {len(tasks)} 张工单到 {out}")
    if args.checklist:
        path = Path(args.checklist)
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_checklist(plan, old), encoding="utf-8")
        print(f"已写 {path}")
    if args.task:
        tid = args.task.upper()
        if tid not in tasks:
            print(f"没有工单 {tid}", file=sys.stderr)
            return 1
        print(tasks[tid]["text"])
    if not (args.list or args.export or args.checklist or args.task):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
