"""community_cold_compress（平台档案压冷）+ archive_layout.date_stem 单测。

全部走 tmp，绝不触真实归档；并轨条目去重、不可识别旁车保底不吞数据、
date_stem 双重后缀剥离与 dated_files 的 gz 感知逐一钉住。
"""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import archive_layout  # noqa: E402
import community_cold_compress as ccc  # noqa: E402


class TestDateStem(unittest.TestCase):
    def test_strips_double_suffix(self):
        self.assertEqual(archive_layout.date_stem(Path('2026-04-01.json.gz')), '2026-04-01')
        self.assertEqual(archive_layout.date_stem(Path('2026-04-01.jsonl.gz')), '2026-04-01')

    def test_plain_suffixes(self):
        self.assertEqual(archive_layout.date_stem(Path('2026-04-01.json')), '2026-04-01')
        self.assertEqual(archive_layout.date_stem(Path('2026-04-01.jsonl')), '2026-04-01')

    def test_other_files_fall_back_to_stem(self):
        self.assertEqual(archive_layout.date_stem(Path('state.txt')), 'state')


class TestDatedFilesGzAware(unittest.TestCase):
    def test_cold_gz_counted_as_dated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / 'bilibili'
            pdir.mkdir()
            (pdir / '2026-06-01.json').write_text('{}', encoding='utf-8')
            with gzip.open(pdir / '2026-04-01.json.gz', 'wt', encoding='utf-8') as g:
                g.write('{}')
            files = archive_layout.dated_files('bilibili', root)
            stems = [archive_layout.date_stem(f) for f in files]
            self.assertEqual(stems, ['2026-04-01', '2026-06-01'])  # 冷月不再蒸发成缺口


def _doc(urls):
    return {'date': '2026-04-01', 'item_count': len(urls),
            'items': [{'url': u, 'title': f't{u}'} for u in urls]}


class TestCompressPlatformFile(unittest.TestCase):
    def test_plain_json_compressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / '2026-04-01.json'
            raw.write_text(json.dumps(_doc(['a'])), encoding='utf-8')
            outcome = ccc.compress_platform_file(raw)
            self.assertEqual(outcome, 'compressed')
            self.assertFalse(raw.exists())
            with archive_layout.open_archive_text(Path(tmp) / '2026-04-01.json.gz') as f:
                self.assertEqual(json.load(f)['item_count'], 1)

    def test_sidecar_merged_by_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / '2026-04-01.json.gz'
            with gzip.open(gz, 'wt', encoding='utf-8') as g:
                json.dump(_doc(['a', 'b']), g)
            raw = Path(tmp) / '2026-04-01.json'
            raw.write_text(json.dumps(_doc(['b', 'c'])), encoding='utf-8')
            outcome = ccc.compress_platform_file(raw)
            self.assertEqual(outcome, 'merged')
            self.assertFalse(raw.exists())
            with archive_layout.open_archive_text(gz) as f:
                doc = json.load(f)
            self.assertEqual([i['url'] for i in doc['items']], ['a', 'b', 'c'])
            self.assertEqual(doc['item_count'], 3)

    def test_unmergeable_sidecar_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / '2026-04-01.json.gz'
            with gzip.open(gz, 'wt', encoding='utf-8') as g:
                g.write('[1, 2, 3]')  # 非 {items:[...]} 形态
            raw = Path(tmp) / '2026-04-01.json'
            raw.write_text('[4]', encoding='utf-8')
            outcome = ccc.compress_platform_file(raw)
            self.assertEqual(outcome, 'kept')
            self.assertTrue(raw.exists())  # 绝不吞数据

    def test_jsonl_sidecar_line_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / '2026-04-01.jsonl.gz'
            with gzip.open(gz, 'wt', encoding='utf-8') as g:
                g.write(json.dumps({'id': '1'}) + '\n')
            raw = Path(tmp) / '2026-04-01.jsonl'
            raw.write_text(json.dumps({'id': '1'}) + '\n' + json.dumps({'id': '2'}) + '\n',
                           encoding='utf-8')
            outcome = ccc.compress_platform_file(raw)
            self.assertEqual(outcome, 'merged')
            with archive_layout.open_archive_text(gz) as f:
                ids = [json.loads(ln)['id'] for ln in f if ln.strip()]
            self.assertEqual(ids, ['1', '2'])


class TestCompressPlatforms(unittest.TestCase):
    def test_walks_platforms_skips_discord_and_hot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'bilibili').mkdir(parents=True)
            (root / 'bilibili' / '2026-04-01.json').write_text(json.dumps(_doc(['a'])), encoding='utf-8')
            (root / 'bilibili' / '2026-06-01.json').write_text(json.dumps(_doc(['b'])), encoding='utf-8')
            (root / 'bilibili' / 'state.json').write_text('{}', encoding='utf-8')
            (root / 'discord' / 'global' / 'channels' / 'x').mkdir(parents=True)
            (root / 'discord' / 'global' / 'channels' / 'x' / '2026-04-01.jsonl').write_text(
                '{"id":"m"}\n', encoding='utf-8')
            with mock.patch.object(ccc, 'COMMUNITY_ROOT', root):
                totals = ccc.compress_platforms('2026-06')
            self.assertEqual(totals['compressed'], 1)  # 只压 bilibili 冷月
            self.assertTrue((root / 'bilibili' / '2026-04-01.json.gz').exists())
            self.assertTrue((root / 'bilibili' / '2026-06-01.json').exists())   # 热层不动
            self.assertTrue((root / 'bilibili' / 'state.json').exists())        # 非日期不动
            self.assertTrue((root / 'discord' / 'global' / 'channels' / 'x' / '2026-04-01.jsonl').exists())  # discord 归 dcc 管




class TestCompressActivityDaily(unittest.TestCase):
    def test_cold_compressed_hot_kept_raw_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sd = root / 'discord' / 'global' / 'activity_daily'
            sd.mkdir(parents=True)
            (root / 'discord' / 'global' / 'channels').mkdir()
            (sd / '2026-04-01.json').write_text(json.dumps({'date': '2026-04-01', 'messages': 5}),
                                                encoding='utf-8')
            (sd / '2026-06-01.json').write_text(json.dumps({'date': '2026-06-01', 'messages': 1}),
                                                encoding='utf-8')
            # raw 胜出场景：旧 gz 计数 3，裸旁车（写方已含底续加）计数 8
            with gzip.open(sd / '2026-04-02.json.gz', 'wt', encoding='utf-8') as g:
                json.dump({'date': '2026-04-02', 'messages': 3}, g)
            (sd / '2026-04-02.json').write_text(json.dumps({'date': '2026-04-02', 'messages': 8}),
                                                encoding='utf-8')
            with mock.patch.object(ccc, 'COMMUNITY_ROOT', root):
                totals = ccc.compress_activity_daily('2026-06')
            self.assertEqual(totals['compressed'], 1)
            self.assertEqual(totals['superseded'], 1)
            self.assertTrue((sd / '2026-06-01.json').exists())  # 热层不动
            with archive_layout.open_archive_text(sd / '2026-04-02.json.gz') as f:
                self.assertEqual(json.load(f)['messages'], 8)   # raw 胜出
            self.assertFalse((sd / '2026-04-02.json').exists())


if __name__ == '__main__':
    unittest.main()
