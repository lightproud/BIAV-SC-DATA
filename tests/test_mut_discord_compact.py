"""Mutation-testing harness for discord_compact (see setup.cfg [mutmut]).

expand_record 是紧凑 schema（CLAUDE.md §5.2「缺字段 = 默认值」契约）的唯一还原器，
全量 732 万条社区档案的可读性压在它的默认值表上；compact_record 是与之成对的唯一
压缩器。二者逻辑密集、常量表噪声低——存活变异体可明确归因为断言盲点，符合本区
收录标准。行覆盖已由 test_discord_compact_unit 提供；本档的职责是让 mutmut 翻转
任何一个默认值 / 判断条件时测试必然变红。

Imports via PACKAGE path (`projects.news.scripts.discord_compact`) so mutmut's
runtime trampoline keys line up with the file-path-derived keys (the sibling
unit test imports the bare module name, which mutmut cannot match). Also a
normal, fast pytest module under plain `pytest tests/`.
"""

# Repo root on path so `projects` resolves as a namespace package in every run
# mode (plain pytest, python -m pytest, and mutmut's mutants/ copy).
import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

from projects.news.scripts.discord_compact import (  # noqa: E402
    compact_record,
    expand_record,
)


def full_record(**over):
    rec = {
        'id': '111', 'channel_id': 'c9', 'type': 0, 'author_id': 'a1',
        'author_name': 'nick', 'author_bot': False, 'content': 'hello',
        'timestamp': '2026-06-01T00:00:00', 'edited_timestamp': None,
        'pinned': False, 'mentions': [], 'reactions': [], 'attachments': [],
        'embeds': [], 'reply_to': None, 'has_thread': False, 'thread_id': None,
        'flags': 0,
    }
    rec.update(over)
    return rec


# --- compact: 恒留字段逐一钉死 ---

def test_always_kept_fields_exact():
    out = compact_record(full_record())
    assert out == {
        'id': '111', 'channel_id': 'c9', 'author_id': 'a1',
        'author_name': 'nick', 'content': 'hello',
        'timestamp': '2026-06-01T00:00:00',
    }


def test_always_kept_none_becomes_empty_string():
    out = compact_record(full_record(content=None))
    assert out['content'] == ''


def test_channel_id_dropped_only_when_falsy():
    assert 'channel_id' not in compact_record(full_record(channel_id=''))
    assert 'channel_id' not in compact_record(full_record(channel_id=None))
    assert compact_record(full_record(channel_id='c1'))['channel_id'] == 'c1'


# --- compact: 非默认才留，逐字段双向断言 ---

def test_type_kept_only_if_nonzero():
    assert 'type' not in compact_record(full_record(type=0))
    assert compact_record(full_record(type=19))['type'] == 19


def test_author_bot_kept_only_if_true():
    assert 'author_bot' not in compact_record(full_record(author_bot=False))
    assert compact_record(full_record(author_bot=True))['author_bot'] is True


def test_pinned_kept_only_if_true():
    assert 'pinned' not in compact_record(full_record(pinned=False))
    assert compact_record(full_record(pinned=True))['pinned'] is True


def test_flags_kept_only_if_nonzero():
    assert 'flags' not in compact_record(full_record(flags=0))
    assert compact_record(full_record(flags=4))['flags'] == 4


def test_has_thread_kept_only_if_true():
    assert 'has_thread' not in compact_record(full_record(has_thread=False))
    assert compact_record(full_record(has_thread=True))['has_thread'] is True


def test_thread_id_kept_when_not_none():
    assert 'thread_id' not in compact_record(full_record(thread_id=None))
    assert compact_record(full_record(thread_id='t7'))['thread_id'] == 't7'


def test_truthy_containers_kept_empty_dropped():
    rec = full_record(mentions=['u2'], reactions=[{'e': 1}], attachments=[{'a': 1}],
                      embeds=[{'b': 2}], reply_to='55',
                      edited_timestamp='2026-06-02T00:00:00')
    out = compact_record(rec)
    assert out['mentions'] == ['u2']
    assert out['reactions'] == [{'e': 1}]
    assert out['attachments'] == [{'a': 1}]
    assert out['embeds'] == [{'b': 2}]
    assert out['reply_to'] == '55'
    assert out['edited_timestamp'] == '2026-06-02T00:00:00'
    empty = compact_record(full_record())
    for k in ('mentions', 'reactions', 'attachments', 'embeds', 'reply_to',
              'edited_timestamp'):
        assert k not in empty


def test_compact_idempotent():
    once = compact_record(full_record(type=19, reactions=[{'e': 1}]))
    assert compact_record(once) == once


# --- expand: 默认值表逐一钉死 ---

def test_expand_defaults_exact():
    assert expand_record({}) == {
        'id': '', 'channel_id': '', 'type': 0, 'author_id': '',
        'author_name': '', 'author_bot': False, 'content': '',
        'timestamp': '', 'edited_timestamp': None, 'pinned': False,
        'mentions': [], 'reactions': [], 'attachments': [], 'embeds': [],
        'reply_to': None, 'has_thread': False, 'thread_id': None, 'flags': 0,
    }


def test_expand_passes_present_values_through():
    src = {'id': '5', 'type': 19, 'flags': 2, 'pinned': True,
           'mentions': ['u1'], 'thread_id': 't1', 'reply_to': '4'}
    out = expand_record(src)
    assert out['id'] == '5'
    assert out['type'] == 19
    assert out['flags'] == 2
    assert out['pinned'] is True
    assert out['mentions'] == ['u1']
    assert out['thread_id'] == 't1'
    assert out['reply_to'] == '4'


# --- 往返无损：compact → expand == 原记录（全字段） ---

def test_roundtrip_default_record():
    rec = full_record()
    assert expand_record(compact_record(rec)) == rec


def test_roundtrip_fully_nondefault_record():
    rec = full_record(type=19, author_bot=True, pinned=True, flags=16,
                      has_thread=True, thread_id='t1', reply_to='9',
                      edited_timestamp='2026-06-03T01:02:03',
                      mentions=['u2', 'u3'], reactions=[{'emoji': 'x', 'count': 2}],
                      attachments=[{'url': 'u'}], embeds=[{'t': 'e'}])
    assert expand_record(compact_record(rec)) == rec
