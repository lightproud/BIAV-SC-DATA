"""Temporal provenance survives validation, conversion and daily archival."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

import global_collectors as gc
import news_common
import collect_global
import archive_platforms

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
FIXTURES = Path(__file__).parent / 'fixtures'


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


@pytest.mark.parametrize('fixture,reply', [
    ('steam_discussions_last_reply_only.html', '2026-10-01T00:00:00+00:00'),
    ('steam_discussions_no_time.html', None),
])
def test_temporal_provenance_pipeline(monkeypatch, tmp_path, fixture, reply):
    monkeypatch.setattr(gc, 'datetime', FixedDatetime)
    # merge_and_dedup 的时效过滤读 collect_global 的时钟；不钉住，样本过了 24 小时就被当旧条目滤掉
    monkeypatch.setattr(collect_global, 'datetime', FixedDatetime)
    monkeypatch.setattr(gc, 'HOURS_LOOKBACK', 24)
    monkeypatch.setattr(gc.time, 'sleep', lambda _: None)
    response = Mock(text=(FIXTURES / fixture).read_text(encoding='utf-8'))
    get = Mock(return_value=response)
    monkeypatch.setattr(gc.requests, 'get', get)
    items = gc._fetch_steam_discussions_one('3052450', 'global', max_pages=1)
    assert len(items) == 1
    item = items[0]
    expected = {
        'created_at': None,
        'last_reply_at': reply,
        'fetched_at': NOW.isoformat(),
        'time_basis': 'last_reply_at' if reply else 'fetched_at',
    }
    assert {k: item[k] for k in expected} == expected
    assert item['time'] == (reply or NOW.isoformat())
    assert bool(item.get('time_is_approximate')) == (reply is None)
    valid, cleaned = news_common.validate_news_item(item)
    assert valid
    converted = collect_global.convert_item(cleaned)
    assert {k: converted[k] for k in expected} == expected
    assert bool(converted.get('time_is_approximate')) == (reply is None)
    # A later fetch/last reply never changes the URL-based identity.
    later = dict(converted, fetched_at=(NOW + timedelta(hours=1)).isoformat(),
                 time=(NOW + timedelta(hours=1)).isoformat())
    assert collect_global.dedup_key(converted) == collect_global.dedup_key(later)
    assert archive_platforms.item_key(converted) == archive_platforms.item_key(later)
    assert len(collect_global.merge_and_dedup([], [converted, later])) == 1
    monkeypatch.setattr(archive_platforms, 'ARCHIVE_DIR', tmp_path)
    day = archive_platforms.item_date_utc8(converted, '1900-01-01')
    assert day == '2026-10-01'
    assert archive_platforms.write_archive('steam', 'global', 'discussion', day, [converted]) == 1
    assert archive_platforms.write_archive('steam', 'global', 'discussion', day, [later]) == 1
    doc = json.loads(archive_platforms.archive_path('steam', 'global', 'discussion', day).read_text())
    assert {k: doc['items'][0][k] for k in expected} == expected


def test_old_discussion_remains_valid_without_new_fields():
    old = dict(title='Legacy thread', source='steam_discussion',
               time=NOW.isoformat(), engagement=0, url='https://steamcommunity.com/app/3052450/discussions/0/123/')
    valid, cleaned = news_common.validate_news_item(old)
    assert valid
    converted = collect_global.convert_item(cleaned)
    assert converted['time'] == old['time']
    assert 'created_at' not in converted


def test_last_reply_cutoff_is_unchanged(monkeypatch):
    monkeypatch.setattr(gc, 'datetime', FixedDatetime)
    monkeypatch.setattr(gc, 'HOURS_LOOKBACK', 1)
    response = Mock(text=(FIXTURES / 'steam_discussions_last_reply_only.html').read_text())
    monkeypatch.setattr(gc.requests, 'get', Mock(return_value=response))
    assert gc._fetch_steam_discussions_one('3052450', 'global', max_pages=1) == []
