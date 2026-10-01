"""Run the five workflow retry loops against temporary, local Git repositories."""
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import os
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOOPS = []
for name in ("backfill-media", "backfill-news", "collect-comments", "community-cold-compress"):
    source = (ROOT / ".github/workflows" / f"{name}.yml").read_text()
    for index, match in enumerate(re.finditer(r"(?m)^ +for i in 1 2 3 4; do\n.*?^ +done\n", source, re.S)):
        LOOPS.append((f"{name}-{index}", textwrap.dedent(match.group())))
assert len(LOOPS) == 5


def verify_retry_cleans_rebase(tmp_path, loop, mode):
    real_git = shutil.which("git")
    assert real_git

    def git(*args, cwd=tmp_path):
        return subprocess.check_output([real_git, *args], cwd=cwd, text=True, stderr=subprocess.PIPE).strip()

    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    git("init", "--bare", "--initial-branch=main", str(remote))
    git("clone", str(remote), str(local))
    git("config", "user.name", "Test", cwd=local)
    git("config", "user.email", "test@example.invalid", cwd=local)
    target = local / "data.txt"
    target.write_text("base\n")
    git("add", ".", cwd=local)
    git("commit", "-m", "base", cwd=local)
    git("push", "origin", "main", cwd=local)
    target.write_text("remote\n")
    git("commit", "-am", "remote", cwd=local)
    git("push", "origin", "main", cwd=local)
    git("reset", "--hard", "HEAD~1", cwd=local)
    if mode in ("conflict", "abort-failed"):
        target.write_text("local\n")
    else:
        (local / "other.txt").write_text("local\n")
    git("add", ".", cwd=local)
    git("commit", "-m", "local", cwd=local)
    original = git("rev-parse", "HEAD", cwd=local)
    # Functions wrap real Git only to observe clean retry entry, suppress sleeps,
    # and simulate transport/push/abort failures. Conflicts themselves are real.
    prelude = r'''
set -euo pipefail
pulls=0
pushes=0
git() {
  if [ "$1" = pull ]; then
    test ! -d .git/rebase-merge && test ! -d .git/rebase-apply || exit 90
    test -z "$("$REAL_GIT" status --porcelain)" || exit 91
    pulls=$((pulls + 1))
    echo pull >> attempts.log
    if [ "$MODE" = pull-failed ]; then return 1; fi
  elif [ "$1" = push ]; then
    pushes=$((pushes + 1))
    if [ "$MODE" = push-rejected ] && [ "$pushes" = 1 ]; then return 1; fi
  elif [ "$1" = rebase ] && [ "$2" = --abort ]; then
    echo abort >> attempts.log
    if [ "$MODE" = abort-failed ]; then return 1; fi
  fi
  "$REAL_GIT" "$@"
}
sleep() { :; }
'''
    # Log outside the worktree so status checks observe only Git's changes.
    prelude = prelude.replace("attempts.log", str(tmp_path / "attempts.log"))
    result = subprocess.run(
        ["bash", "-c", prelude + loop + '\nexit ' + ("0" if mode == "push-rejected" else "1")], cwd=local,
        env={**os.environ, "REAL_GIT": real_git, "MODE": mode},
        capture_output=True, text=True, timeout=30,
    )
    events = (tmp_path / "attempts.log").read_text().splitlines()
    if mode == "push-rejected":
        assert result.returncode == 0, result.stderr
        assert events == ["pull", "pull"]
        assert git("rev-parse", "HEAD", cwd=local) == git("rev-parse", "main", cwd=remote)
    elif mode == "abort-failed":
        assert result.returncode == 1
        assert events == ["pull", "abort"]  # No retry with uncleared state.
        assert (local / ".git/rebase-merge").is_dir()
    else:
        assert result.returncode == 1, result.stderr
        assert events == (["pull", "abort"] * 4 if mode == "conflict" else ["pull"] * 4)
        assert git("rev-parse", "HEAD", cwd=local) == original
        assert git("status", "--porcelain", cwd=local) == ""
        assert not (local / ".git/rebase-merge").exists()
        assert not (local / ".git/rebase-apply").exists()


class PushRetryTests(unittest.TestCase):
    def test_all_workflow_loops(self):
        for name, loop in LOOPS:
            for mode in ("conflict", "push-rejected", "pull-failed", "abort-failed"):
                with self.subTest(workflow=name, mode=mode):
                    with tempfile.TemporaryDirectory() as directory:
                        verify_retry_cleans_rebase(Path(directory), loop, mode)


if __name__ == "__main__":
    unittest.main()
