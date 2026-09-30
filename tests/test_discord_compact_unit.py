"""discord_compact 紧凑 schema 单测：默认字段删除、非默认保留、幂等、可还原。"""
import unittest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）
from discord_compact import compact_record, expand_record  # noqa: E402


def _full(**over):
    rec = {
        'id': '1', 'channel_id': 'c', 'type': 0, 'author_id': 'a', 'author_name': 'n',
        'author_bot': False, 'content': 'hi', 'timestamp': 't', 'edited_timestamp': None,
        'pinned': False, 'mentions': [], 'reactions': [], 'attachments': [], 'embeds': [],
        'reply_to': None, 'has_thread': False, 'thread_id': None, 'flags': 0,
    }
    rec.update(over)
    return rec


class TestCompact(unittest.TestCase):
    def test_drops_all_defaults(self):
        c = compact_record(_full())
        self.assertEqual(set(c), {'id', 'channel_id', 'author_id', 'author_name', 'content', 'timestamp'})

    def test_keeps_nondefault(self):
        c = compact_record(_full(type=19, author_bot=True, pinned=True, flags=2,
                                  has_thread=True, thread_id='t1',
                                  edited_timestamp='e', mentions=['u'],
                                  reactions=[{'count': 1}], attachments=[{'url': 'x'}],
                                  embeds=[{'type': 'rich'}], reply_to='9'))
        for k in ('type', 'author_bot', 'pinned', 'flags', 'has_thread', 'thread_id',
                  'edited_timestamp', 'mentions', 'reactions', 'attachments', 'embeds', 'reply_to'):
            self.assertIn(k, c, f'{k} 应保留')

    def test_idempotent(self):
        c = compact_record(_full(reply_to='9', type=19))
        self.assertEqual(compact_record(c), c)

    def test_expand_roundtrip_lossless_for_defaults(self):
        # 紧凑→还原应等于原始完整记录（默认值无损）
        self.assertEqual(expand_record(compact_record(_full())), _full())

    def test_keeps_required_even_if_empty_content(self):
        c = compact_record(_full(content=''))
        self.assertIn('content', c)
        self.assertEqual(c['content'], '')


if __name__ == '__main__':
    unittest.main()
