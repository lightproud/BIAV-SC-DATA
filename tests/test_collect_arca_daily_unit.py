"""collect_arca_daily 单测——CC 例程过渡桥（2026-07-10 方案 2）的响亮失败语义。

该脚本每日无人值守跑「采集 → 归档 → commit → push（带重试）」，其可靠性
支柱正是失败路径：零条采集 / commit 失败 / push 重试耗尽都必须非零退出。
Hermetic：fetch_arca_live / write_archive / subprocess.run / time.sleep 全部
打桩，零网络、不触碰真实 git 状态。
"""

import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import collect_arca_daily as cad


def _item(title, date=None):
    it = {"title": title, "url": f"https://arca.live/b/forgettingeve/{title}"}
    if date:
        it["time"] = f"{date}T04:00:00+00:00"  # UTC 04:00 → UTC+8 当日 12:00
    return it


class _FakeProc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


class _GitScript:
    """按 git 子命令回放预设结果的 subprocess.run 桩（无预设的命令默认成功）。

    key 匹配 cmd 中第一个出现的子命令词（add / diff / commit / pull / push），
    值为按调用次序弹出的 _FakeProc 列表；耗尽后沿用最后一个。
    """

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        for word in ("diff", "commit", "pull", "push", "add"):
            if word in cmd:
                queue = self.script.get(word)
                if queue:
                    return queue.pop(0) if len(queue) > 1 else queue[0]
                return _FakeProc()
        return _FakeProc()


class TestCollectArcaDaily(unittest.TestCase):
    def _run(self, argv, items, git_script=None):
        """跑 main() 一次，返回 (exit_code, git 桩, write_archive 桩)。"""
        fake_git = _GitScript(git_script or {})
        write_archive = mock.Mock(side_effect=lambda *a: len(a[4]))
        with mock.patch.object(cad, "fetch_arca_live", return_value=items), \
             mock.patch.object(cad, "write_archive", write_archive), \
             mock.patch.object(cad.subprocess, "run", fake_git), \
             mock.patch.object(cad.time, "sleep") as fake_sleep, \
             mock.patch.object(sys, "argv", ["collect_arca_daily.py", *argv]):
            code = cad.main()
        return code, fake_git, write_archive, fake_sleep

    # ── 响亮失败：零条采集 ──────────────────────────────────────────────

    def test_zero_items_exits_nonzero(self):
        code, git, wa, _ = self._run([], items=[])
        self.assertEqual(code, 1)
        wa.assert_not_called()
        self.assertEqual(git.calls, [])  # 零条时绝不碰 git

    # ── 归档分桶 ────────────────────────────────────────────────────────

    def test_items_bucketed_by_utc8_date(self):
        items = [_item("a", "2026-07-10"), _item("b", "2026-07-11"), _item("c", "2026-07-11")]
        code, _, wa, _ = self._run(["--no-push"], items)
        self.assertEqual(code, 0)
        dates = [call.args[3] for call in wa.call_args_list]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(wa.call_args_list), 2)  # 两个日期桶
        for call in wa.call_args_list:
            self.assertEqual(call.args[0], "arca_live")  # 平铺源：无区服/类型层
            self.assertIsNone(call.args[1])
            self.assertIsNone(call.args[2])

    def test_no_push_skips_git_entirely(self):
        code, git, _, _ = self._run(["--no-push"], [_item("a", "2026-07-11")])
        self.assertEqual(code, 0)
        self.assertEqual(git.calls, [])

    # ── git 提交路径 ────────────────────────────────────────────────────

    def test_no_staged_changes_exits_zero_without_commit(self):
        # diff --staged --quiet 返回 0 = 无变更
        code, git, _, _ = self._run([], [_item("a", "2026-07-11")],
                                    git_script={"diff": [_FakeProc(0)]})
        self.assertEqual(code, 0)
        subcmds = [c for c in git.calls if "commit" in c]
        self.assertEqual(subcmds, [])

    def test_commit_failure_exits_nonzero(self):
        code, _, _, _ = self._run([], [_item("a", "2026-07-11")],
                                  git_script={"diff": [_FakeProc(1)],
                                              "commit": [_FakeProc(1, stderr="hook rejected")]})
        self.assertEqual(code, 1)

    def test_commit_uses_ephemeral_bot_identity(self):
        # lesson #48/#49：机器身份只随单条命令 -c 生效，绝不 git config 落盘
        code, git, _, _ = self._run([], [_item("a", "2026-07-11")],
                                    git_script={"diff": [_FakeProc(1)]})
        self.assertEqual(code, 0)
        commit_calls = [c for c in git.calls if "commit" in c]
        self.assertTrue(commit_calls and "-c" in commit_calls[0])
        self.assertFalse(any(c[:2] == ["git", "config"] for c in git.calls))

    # ── push 重试语义 ───────────────────────────────────────────────────

    def test_push_success_first_attempt(self):
        code, git, _, sleep = self._run([], [_item("a", "2026-07-11")],
                                        git_script={"diff": [_FakeProc(1)]})
        self.assertEqual(code, 0)
        self.assertEqual(len([c for c in git.calls if "push" in c]), 1)
        sleep.assert_not_called()

    def test_push_retries_then_succeeds(self):
        code, git, _, sleep = self._run(
            [], [_item("a", "2026-07-11")],
            git_script={"diff": [_FakeProc(1)],
                        "push": [_FakeProc(1, stderr="rejected"), _FakeProc(1, stderr="rejected"),
                                 _FakeProc(0), _FakeProc(0)]})
        self.assertEqual(code, 0)
        self.assertEqual(len([c for c in git.calls if "push" in c]), 3)
        # 指数退避：2^1, 2^2
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])

    def test_push_exhausts_retries_exits_nonzero(self):
        code, git, _, sleep = self._run(
            [], [_item("a", "2026-07-11")],
            git_script={"diff": [_FakeProc(1)], "push": [_FakeProc(1, stderr="rejected")]})
        self.assertEqual(code, 1)
        self.assertEqual(len([c for c in git.calls if "push" in c]), 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4, 8, 16])

    def test_push_attempts_rebase_before_each_push(self):
        code, git, _, _ = self._run([], [_item("a", "2026-07-11")],
                                    git_script={"diff": [_FakeProc(1)],
                                                "push": [_FakeProc(1), _FakeProc(0)]})
        self.assertEqual(code, 0)
        pulls = [c for c in git.calls if "pull" in c]
        pushes = [c for c in git.calls if "push" in c]
        self.assertEqual(len(pulls), len(pushes))  # 每次 push 前都 pull --rebase


if __name__ == "__main__":
    unittest.main()
