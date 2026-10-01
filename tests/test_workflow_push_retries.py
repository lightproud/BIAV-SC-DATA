"""用真正的 Git 冲突验证工作流：每次重试后恢复干净状态并诚实失败。"""
from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ['backfill-media', 'backfill-news', 'collect-comments', 'community-cold-compress', 'update-news']


def retry_loops():
    cases = []
    for name in WORKFLOWS:
        workflow = yaml.safe_load((ROOT / '.github/workflows' / f'{name}.yml').read_text())
        for job in workflow['jobs'].values():
            for step in job.get('steps', []):
                for index, loop in enumerate(re.findall(r'for i in 1 2 3 4; do\n.*?\bdone', step.get('run', ''), re.S)):
                    if "git pull --rebase origin main && git push" not in loop:
                        continue  # update-news 状态推送另有内容级冲突合并方案
                    cases.append(pytest.param(loop, id=f'{name}-{step["name"]}-{index}'))
    assert len(cases) == 6, '必须验证这次修改的六个重试入口'
    return cases


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def repositories(tmp_path, conflict):
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', '-b', 'main', str(remote))
    upstream = tmp_path / 'upstream'
    git(tmp_path, 'clone', str(remote), str(upstream))
    git(upstream, 'config', 'user.name', 'Test')
    git(upstream, 'config', 'user.email', 'test@example.invalid')
    (upstream / 'state.txt').write_text('base\n')
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-m', 'base')
    git(upstream, 'push', 'origin', 'main')
    local = tmp_path / 'local'
    git(tmp_path, 'clone', str(remote), str(local))
    git(local, 'config', 'user.name', 'Test')
    git(local, 'config', 'user.email', 'test@example.invalid')
    (local / 'state.txt').write_text('local\n')
    git(local, 'add', '.')
    git(local, 'commit', '-m', 'local')
    (upstream / ('state.txt' if conflict else 'other.txt')).write_text('upstream\n')
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-m', 'upstream')
    git(upstream, 'push', 'origin', 'main')
    return local


@pytest.mark.parametrize('loop', retry_loops())
def test_each_rebase_conflict_is_aborted_before_retry(tmp_path, loop):
    local = repositories(tmp_path, conflict=True)
    original_head = git(local, 'rev-parse', 'HEAD')
    script = 'set -euo pipefail\nsleep() { :; }\n' + loop + '\nexit 1\n'
    result = subprocess.run(['bash', '-c', script], cwd=local, text=True, capture_output=True)
    assert result.returncode == 1
    assert result.stdout.count('CONFLICT') == 4
    assert git(local, 'rev-parse', 'HEAD') == original_head
    assert git(local, 'status', '--porcelain') == ''
    assert not (local / '.git/rebase-merge').exists()
    assert not (local / '.git/rebase-apply').exists()


def test_retry_can_publish_after_nonconflicting_rebase(tmp_path):
    local = repositories(tmp_path, conflict=False)
    workflow = yaml.safe_load((ROOT / '.github/workflows/collect-comments.yml').read_text())
    script = next(step['run'] for job in workflow['jobs'].values() for step in job['steps']
                  if 'for i in 1 2 3 4' in step.get('run', ''))
    # 执行同一真实重试循环；上面步骤的归档目录不属于测试夹具。
    loop = re.search(r'for i in 1 2 3 4; do\n.*?\bdone', script, re.S).group()
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\nsleep() { :; }\n' + loop], cwd=local)
    assert result.returncode == 0
    assert git(local, 'rev-parse', 'HEAD') == git(local, 'rev-parse', 'origin/main')
    assert (local / 'other.txt').read_text() == 'upstream\n'


def test_backfill_stops_cleanly_when_preflight_pull_conflicts(tmp_path):
    local = repositories(tmp_path, conflict=True)
    workflow = yaml.safe_load((ROOT / '.github/workflows/backfill-news.yml').read_text())
    script = next(step['run'] for job in workflow['jobs'].values() for step in job['steps']
                  if step.get('name') == 'Pull latest backfill state (code repo)')
    result = subprocess.run(['bash', '-c', script], cwd=local, text=True, capture_output=True)
    assert result.returncode == 1
    assert result.stdout.count('CONFLICT') == 1
    assert git(local, 'status', '--porcelain') == ''
    assert not (local / '.git/rebase-merge').exists()
