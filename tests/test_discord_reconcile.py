"""discord_reconcile 单测（T35）：合并式索引更新 + 孤儿目录对账。

全部走 tmp 目录与纯函数，绝不触真实归档、绝不触网；git 历史回收走注入的
names 映射，不依赖仓库状态。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import discord_reconcile as dr  # noqa: E402

import tempfile  # noqa: E402


def _entry(name, dir_suffix, status=None):
    e = {'name': name, 'type': 'text', 'parent_id': '', 'dir': dir_suffix}
    if status is not None:
        e['status'] = status
    return e


class TestMergeChannelIndex(unittest.TestCase):
    def test_current_entries_marked_active(self):
        merged = dr.merge_channel_index({}, {'1': _entry('a', '00000001')})
        self.assertEqual(merged['1']['status'], 'active')

    def test_dropped_entry_preserved_as_offline(self):
        existing = {'1': _entry('old-name', '00000001')}
        merged = dr.merge_channel_index(existing, {})
        self.assertEqual(merged['1']['status'], 'offline')
        self.assertEqual(merged['1']['name'], 'old-name')

    def test_orphan_status_sticks_while_offline(self):
        existing = {'1': _entry('', '00000001', status='orphan')}
        merged = dr.merge_channel_index(existing, {})
        self.assertEqual(merged['1']['status'], 'orphan')

    def test_orphan_coming_online_becomes_active(self):
        existing = {'1': _entry('', '00000001', status='orphan')}
        merged = dr.merge_channel_index(existing, {'1': _entry('back', '00000001')})
        self.assertEqual(merged['1']['status'], 'active')
        self.assertEqual(merged['1']['name'], 'back')

    def test_rename_takes_current_name(self):
        existing = {'1': _entry('old', '00000001', status='active')}
        merged = dr.merge_channel_index(existing, {'1': _entry('new', '00000001')})
        self.assertEqual(merged['1']['name'], 'new')

    def test_inputs_not_mutated(self):
        existing = {'1': _entry('a', '00000001')}
        current = {'2': _entry('b', '00000002')}
        dr.merge_channel_index(existing, current)
        self.assertNotIn('status', existing['1'])
        self.assertNotIn('status', current['2'])


class TestRecoverChannelId(unittest.TestCase):
    def test_recovers_from_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / '2026-07-01.jsonl').write_text(
                json.dumps({'id': 'm1', 'channel_id': '123456789012345678'}) + '\n',
                encoding='utf-8')
            self.assertEqual(dr.recover_channel_id(d), '123456789012345678')

    def test_skips_corrupt_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / '2026-07-02.jsonl').write_text('not json\n', encoding='utf-8')
            (d / '2026-07-01.jsonl').write_text(
                json.dumps({'channel_id': '42'}) + '\n', encoding='utf-8')
            self.assertEqual(dr.recover_channel_id(d), '42')

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dr.recover_channel_id(Path(tmp)), '')


class TestReconcileRegion(unittest.TestCase):
    def _make_region(self, tmp):
        root = Path(tmp)
        (root / 'channels').mkdir(parents=True)
        return root

    def _add_channel_dir(self, root, cid):
        d = root / 'channels' / cid[-8:]
        d.mkdir()
        (d / '2026-07-01.jsonl').write_text(
            json.dumps({'id': 'm', 'channel_id': cid}) + '\n', encoding='utf-8')
        return d

    def test_orphan_registered_with_recovered_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_region(tmp)
            self._add_channel_dir(root, '111111111100000001')
            stats = dr.reconcile_region(root, names={})
            idx = json.loads((root / 'channel_index.json').read_text(encoding='utf-8'))
            self.assertEqual(stats['orphans'], 1)
            self.assertEqual(idx['111111111100000001']['status'], 'orphan')
            self.assertEqual(idx['111111111100000001']['dir'], '00000001')

    def test_orphan_name_recovered_from_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_region(tmp)
            self._add_channel_dir(root, '111111111100000001')
            stats = dr.reconcile_region(root, names={'111111111100000001': '旧频道名'})
            idx = json.loads((root / 'channel_index.json').read_text(encoding='utf-8'))
            self.assertEqual(stats['named'], 1)
            self.assertEqual(idx['111111111100000001']['name'], '旧频道名')

    def test_indexed_dir_not_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_region(tmp)
            self._add_channel_dir(root, '111111111100000001')
            (root / 'channel_index.json').write_text(json.dumps({
                '111111111100000001': _entry('known', '00000001', status='active'),
            }), encoding='utf-8')
            stats = dr.reconcile_region(root, names={})
            idx = json.loads((root / 'channel_index.json').read_text(encoding='utf-8'))
            self.assertEqual(stats['orphans'], 0)
            self.assertEqual(idx['111111111100000001']['name'], 'known')

    def test_index_entry_without_dir_kept(self):
        # jp/volunteer 常态：索引含尚无归档的在线频道，对账不得删它
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_region(tmp)
            (root / 'channel_index.json').write_text(json.dumps({
                '222222222200000002': _entry('未产出频道', '00000002', status='active'),
            }), encoding='utf-8')
            dr.reconcile_region(root, names={})
            idx = json.loads((root / 'channel_index.json').read_text(encoding='utf-8'))
            self.assertIn('222222222200000002', idx)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_region(tmp)
            self._add_channel_dir(root, '111111111100000001')
            stats = dr.reconcile_region(root, names={}, dry_run=True)
            self.assertEqual(stats['orphans'], 1)
            self.assertFalse((root / 'channel_index.json').exists())

    def test_unrecoverable_id_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_region(tmp)
            d = root / 'channels' / 'deadbeef'
            d.mkdir()
            (d / '2026-07-01.jsonl').write_text('corrupt\n', encoding='utf-8')
            stats = dr.reconcile_region(root, names={})
            idx = json.loads((root / 'channel_index.json').read_text(encoding='utf-8'))
            self.assertEqual(stats['unrecovered'], 1)
            self.assertEqual(idx, {})


if __name__ == '__main__':
    unittest.main()
