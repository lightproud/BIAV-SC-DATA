"""日期证据核对的契约与失败分支；不请求真实网络。"""
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
import requests

import _paths  # noqa: F401
import source_freshness as sf
import silent_sources_audit as ssa


def leaf(name='steam/global/news', archived='2026-09-21', stalled=True):
    return {'leaf': name, 'last_archive_date': archived, 'stalled': stalled}


def steam(stamp=None):
    if stamp is None:
        stamp = int(datetime(2026, 9, 21, 17, tzinfo=UTC).timestamp())
    return {'appnews': {'appid': 3052450, 'newsitems': [{'date': stamp}]}}


def run(monkeypatch, payload, row=None):
    response = Mock()
    response.json.return_value = payload
    get = Mock(return_value=response)
    monkeypatch.setattr(sf.requests, 'get', get)
    result = sf.audit_source_dates([row or leaf()])[0]
    return result, get


def test_steam_beijing_day_and_evidence(monkeypatch):
    row, get = run(monkeypatch, steam())
    assert row['status'] == 'newer_source_date'
    assert row['latest_source_date'] == '2026-09-22'
    assert row['checked_at'] and row['scope'] == 'latest_publication_date_only'
    get.assert_called_once_with(row['source_url'], timeout=(5, 15))


@pytest.mark.parametrize('archived', ['2026-09-22', '2026-09-23'])
def test_same_or_older_is_limited_no_new_date(monkeypatch, archived):
    row, _ = run(monkeypatch, steam(), leaf(archived=archived))
    assert row['status'] == 'no_newer_source_date'


@pytest.mark.parametrize('payload', [{}, {'appnews': {'appid': 1, 'newsitems': [{'date': 1}]}},
    {'appnews': {'appid': 3052450, 'newsitems': []}}, steam(True), steam('bad'), steam(-1), steam(10**30)])
def test_invalid_steam_is_unverified(monkeypatch, payload):
    row, _ = run(monkeypatch, payload)
    assert row['status'] == 'unverified'
    assert 'latest_source_date' not in row


def test_appstore_collapsed_review_and_timezone(monkeypatch):
    payload = {'feed': {'entry': {'im:rating': {'label': '5'},
        'updated': {'label': '2026-09-16T19:43:27-07:00'}}}}
    row, _ = run(monkeypatch, payload, leaf('appstore/jp', '2026-09-17'))
    assert row['latest_source_date'] == '2026-09-17'
    assert row['status'] == 'no_newer_source_date'


@pytest.mark.parametrize('entries', [[], [{'name': 'metadata'}],
    [{'im:rating': {}, 'updated': {'label': '2026-09-17T00:00:00'}}],
    [{'im:rating': {}, 'updated': {'label': 'bad'}}]])
def test_invalid_appstore_is_unverified(monkeypatch, entries):
    row, _ = run(monkeypatch, {'feed': {'entry': entries}}, leaf('appstore/jp'))
    assert row['status'] == 'unverified'


def test_appstore_uses_latest_review_skips_metadata(monkeypatch):
    entries = [{'name': 'metadata'}] + [{'im:rating': {}, 'updated': {'label': stamp}}
        for stamp in ['2026-09-19T00:00:00Z', '2026-09-22T00:00:00Z']]
    row, _ = run(monkeypatch, {'feed': {'entry': entries}}, leaf('appstore/jp'))
    assert row['latest_source_date'] == '2026-09-22'


@pytest.mark.parametrize('failure', [requests.Timeout('private response'), requests.HTTPError('secret'),
                                   ValueError('invalid JSON')])
def test_failure_is_unverified_without_exception_text(monkeypatch, failure):
    monkeypatch.setattr(sf.requests, 'get', Mock(side_effect=failure))
    row = sf.audit_source_dates([leaf()])[0]
    assert row['status'] == 'unverified'
    assert row['reason'] == type(failure).__name__
    assert str(failure) not in str(row)


def test_unsupported_and_active_do_not_request(monkeypatch):
    get = Mock(side_effect=AssertionError('no network'))
    monkeypatch.setattr(sf.requests, 'get', get)
    rows = sf.audit_source_dates([leaf(stalled=False), leaf('unknown/leaf')])
    assert len(rows) == 1 and rows[0]['reason'] == 'no_checker'
    get.assert_not_called()


def test_invalid_archive_date_does_not_request(monkeypatch):
    row, get = run(monkeypatch, steam(), leaf(archived='bad'))
    assert row['status'] == 'unverified'
    get.assert_not_called()


def test_duplicate_leaf_only_requests_once(monkeypatch):
    response = Mock()
    response.json.return_value = steam()
    get = Mock(return_value=response)
    monkeypatch.setattr(sf.requests, 'get', get)
    assert len(sf.audit_source_dates([leaf(), leaf()])) == 1
    get.assert_called_once()


def test_evidence_does_not_suppress_core_alarm():
    report = {'entries': [{'source': 'official', 'level': 'dormant'}],
              'source_date_checks': [{'leaf': 'steam/global/news', 'status': 'no_newer_source_date'}]}
    assert ssa.core_source_alarms(report) == ['official']
