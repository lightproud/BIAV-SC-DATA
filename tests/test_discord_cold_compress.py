"""discord_cold_compress 单测（甲案冷热分层，守密人 2026-07-12 裁定）。

全部走 tmp 目录，绝不触真实归档；月界规则、并轨去重、幂等、dry-run、
统一开档函数契约逐一钉住。
"""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_layout  # noqa: E402
import discord_cold_compress as dcc  # noqa: E402


def _msg(mid, content='x'):
    return json.dumps({'id': mid, 'channel_id': 'c1', 'content': content}, ensure_ascii=False)


class TestDefaultCutoff(unittest.TestCase):
    def test_mid_year(self):
        # 7 月时冷月上界 = 2026-06（不含）→ 压 2026-05 及更早
        self.assertEqual(dcc.default_cutoff(date(2026, 7, 12)), '2026-06')

    def test_january_wraps_year(self):
        self.assertEqual(dcc.default_cutoff(date(2026, 1, 5)), '2025-12')


class TestOpenArchiveText(unittest.TestCase):
    def test_reads_raw_and_gz_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / 'a.jsonl'
            gz = Path(tmp) / 'b.jsonl.gz'
            raw.write_text(_msg('1') + '\n', encoding='utf-8')
            with gzip.open(gz, 'wt', encoding='utf-8') as g:
                g.write(_msg('1') + '\n')
            with archive_layout.open_archive_text(raw) as f:
                r1 = f.read()
            with archive_layout.open_archive_text(gz) as f:
                r2 = f.read()
            self.assertEqual(r1, r2)

    def test_reads_concatenated_gzip_members(self):
        # 追加场景可能产生多成员 gzip；透明读全
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / 'c.jsonl.gz'
            with open(gz, 'wb') as f:
                f.writelines(gzip.compress((_msg(mid) + '\n').encode('utf-8')) for mid in ('1', '2'))
            with archive_layout.open_archive_text(gz) as f:
                lines = [ln for ln in f if ln.strip()]
            self.assertEqual(len(lines), 2)


class TestCompressChannelDir(unittest.TestCase):
    def _dir(self, tmp):
        d = Path(tmp) / 'ch'
        d.mkdir()
        return d

    def test_cold_file_compressed_hot_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir(tmp)
            (d / '2026-04-01.jsonl').write_text(_msg('1') + '\n', encoding='utf-8')
            (d / '2026-06-01.jsonl').write_text(_msg('2') + '\n', encoding='utf-8')
            stats = dcc.compress_channel_dir(d, cutoff='2026-06')
            self.assertEqual(stats['compressed'], 1)
            self.assertFalse((d / '2026-04-01.jsonl').exists())
            self.assertTrue((d / '2026-04-01.jsonl.gz').exists())
            self.assertTrue((d / '2026-06-01.jsonl').exists())  # 热层不动

    def test_content_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir(tmp)
            lines = [_msg(str(i), f'内容{i}') for i in range(50)]
            (d / '2026-03-15.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
            dcc.compress_channel_dir(d, cutoff='2026-06')
            with archive_layout.open_archive_text(d / '2026-03-15.jsonl.gz') as f:
                out = [ln.rstrip('\n') for ln in f if ln.strip()]
            self.assertEqual(out, lines)

    def test_sidecar_merged_dedup_by_id(self):
        # 冷月已压 .gz 后被历史回填追加出裸旁车 → 并轨去重
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir(tmp)
            with gzip.open(d / '2026-04-01.jsonl.gz', 'wt', encoding='utf-8') as g:
                g.write(_msg('1') + '\n' + _msg('2') + '\n')
            (d / '2026-04-01.jsonl').write_text(
                _msg('2') + '\n' + _msg('3') + '\n', encoding='utf-8')
            stats = dcc.compress_channel_dir(d, cutoff='2026-06')
            self.assertEqual(stats['merged'], 1)
            self.assertFalse((d / '2026-04-01.jsonl').exists())
            with archive_layout.open_archive_text(d / '2026-04-01.jsonl.gz') as f:
                ids = [json.loads(ln)['id'] for ln in f if ln.strip()]
            self.assertEqual(ids, ['1', '2', '3'])

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir(tmp)
            (d / '2026-04-01.jsonl').write_text(_msg('1') + '\n', encoding='utf-8')
            dcc.compress_channel_dir(d, cutoff='2026-06')
            first = (d / '2026-04-01.jsonl.gz').read_bytes()
            stats2 = dcc.compress_channel_dir(d, cutoff='2026-06')
            self.assertEqual(stats2['compressed'] + stats2['merged'], 0)
            self.assertEqual((d / '2026-04-01.jsonl.gz').read_bytes(), first)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir(tmp)
            (d / '2026-04-01.jsonl').write_text(_msg('1') + '\n', encoding='utf-8')
            stats = dcc.compress_channel_dir(d, cutoff='2026-06', dry_run=True)
            self.assertEqual(stats['compressed'], 1)
            self.assertTrue((d / '2026-04-01.jsonl').exists())
            self.assertFalse((d / '2026-04-01.jsonl.gz').exists())

    def test_non_dated_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dir(tmp)
            (d / 'state.jsonl').write_text('{}\n', encoding='utf-8')
            stats = dcc.compress_channel_dir(d, cutoff='2026-06')
            self.assertEqual(stats['compressed'], 0)
            self.assertTrue((d / 'state.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
