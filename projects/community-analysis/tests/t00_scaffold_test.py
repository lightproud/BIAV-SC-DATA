"""T00 脚手架：配置、命令行空壳、取工单工具。"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from yuqing import cli
from yuqing.config import ConfigError, check_not_in_lake, load_config, project_root, repo_root

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_SUBCOMMANDS = [
    "census", "normalize", "clean", "unitize", "llm", "annotate", "gold",
    "query", "mcp", "run-incremental", "issues", "ops", "ledgers",
]  # fmt: skip


def _load_task_tool():
    spec = importlib.util.spec_from_file_location("task_tool", ROOT / "tools" / "task.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


task_tool = _load_task_tool()


def test_defaults_loaded():
    cfg = load_config(environ={})
    assert cfg.get("unitize", "gap_minutes") == 2
    assert cfg.get("issues", "tau_hi") == 0.80
    assert cfg.get("budget", "tokens_per_run") == 0


def test_env_overrides_param_with_type():
    cfg = load_config(environ={"YUQING_UNITIZE_GAP_MINUTES": "15", "YUQING_ANNOTATE_LOW_CONF": "0.5"})
    assert cfg.get("unitize", "gap_minutes") == 15
    assert cfg.get("annotate", "low_conf") == 0.5


def test_bad_env_value_names_variable():
    with pytest.raises(ConfigError, match="YUQING_UNITIZE_MAX_MSGS"):
        load_config(environ={"YUQING_UNITIZE_MAX_MSGS": "多"})


def test_missing_required_names_the_variable():
    cfg = load_config(environ={})
    with pytest.raises(ConfigError, match="LAKE_ROOT"):
        cfg.require("LAKE_ROOT")


def test_data_root_defaults_to_project_data_and_refuses_lake():
    # 守密人 2026-10-03：产出留仓内；只是不能写进数据湖（原文只读）
    assert load_config(environ={}).data_root() == (project_root() / "data").resolve()
    with pytest.raises(ConfigError, match="DATA_ROOT"):
        load_config(environ={"DATA_ROOT": str(repo_root() / "Record" / "Community" / "out")})


def test_data_root_anywhere_but_lake(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    outside = tmp_path / "data"
    lake = repo / "lake"
    assert check_not_in_lake(outside, "DATA_ROOT", [lake]) == outside.resolve()
    assert check_not_in_lake(repo / "out", "DATA_ROOT", [lake]) == (repo / "out").resolve()
    with pytest.raises(ConfigError):
        check_not_in_lake(lake / "out", "DATA_ROOT", [lake])


def test_cli_registers_all_subcommands():
    assert list(cli.SUBCOMMANDS) == EXPECTED_SUBCOMMANDS
    help_text = cli.build_parser().format_help()
    for name in EXPECTED_SUBCOMMANDS:
        assert name in help_text


def test_cli_stub_exits_nonzero(capsys):
    assert cli.main(["ops"]) == 2
    assert "T50" in capsys.readouterr().err


def test_yuqing_help_subprocess():
    out = subprocess.run(
        [sys.executable, "-m", "yuqing.cli", "--help"], capture_output=True, text=True, check=True
    ).stdout
    assert all(name in out for name in EXPECTED_SUBCOMMANDS)


def test_task_list_matches_plan():
    plan = task_tool.load_plan()
    assert len(plan["tasks"]) == 24
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "task.py"), "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(out.strip().splitlines()) == 24


def test_task_prints_t01():
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "task.py"), "T01"], capture_output=True, text=True, check=True
    ).stdout
    assert out.startswith("【工单 T01】数据普查")


def test_export_writes_one_file_per_task(tmp_path):
    task_tool.main(["--export", str(tmp_path)])
    files = sorted(p.name for p in tmp_path.glob("T*.md"))
    assert len(files) == 24
    assert "T24.md" in files


def test_checklist_counts_and_order(tmp_path):
    path = tmp_path / "checklist.md"
    task_tool.main(["--checklist", str(path)])
    text = path.read_text(encoding="utf-8")
    ids = re.findall(r"^- \[ \] ([TGR]\d+)", text, re.M)
    assert sum(i.startswith("T") for i in ids) == 24
    assert sum(i.startswith("G") for i in ids) == 6
    assert sum(i.startswith("R") for i in ids) == 3
    assert ids.index("G0") == ids.index("T01") + 1
    assert ids.index("R1") == ids.index("G1") + 1
    assert ids.index("R3") == ids.index("T42") + 1
    assert ids.index("G4") == ids.index("R3") + 1
    assert ids.index("G5") == ids.index("T50") - 1


def test_checklist_preserves_ticked_lines(tmp_path):
    path = tmp_path / "checklist.md"
    task_tool.main(["--checklist", str(path)])
    ticked = path.read_text(encoding="utf-8").replace("- [ ] T00 脚手架", "- [x] T00 脚手架 · abc1234")
    path.write_text(ticked, encoding="utf-8")
    task_tool.main(["--checklist", str(path)])
    text = path.read_text(encoding="utf-8")
    assert "- [x] T00 脚手架 · abc1234" in text
    assert text.count("T00") == 1
