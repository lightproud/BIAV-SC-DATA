"""沉默叶的公开源日期核对；仅提供证据，不改变健康门控。"""
from datetime import UTC, date, datetime

import requests

from archive_layout import archive_date_str
from sources import REGION_APPS


def _endpoint(leaf):
    if leaf == 'steam/global/news':
        app_id = int(REGION_APPS['steam']['global'])
        return f'https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={app_id}&count=3&maxlength=0&format=json', app_id
    if leaf == 'appstore/jp':
        app_id = REGION_APPS['appstore']['jp']
        return f'https://itunes.apple.com/jp/rss/customerreviews/id={app_id}/sortBy=mostRecent/json', app_id
    return None, None


def _latest(leaf, payload, app_id):
    if leaf == 'steam/global/news':
        news = payload['appnews']
        if news['appid'] != app_id:
            raise ValueError('appid mismatch')
        timestamps = []
        for item in news['newsitems']:
            stamp = item['date']
            if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp <= 0:
                raise ValueError('invalid timestamp')
            timestamps.append(datetime.fromtimestamp(stamp, UTC))
    else:
        entries = payload['feed']['entry']
        if isinstance(entries, dict):
            entries = [entries]
        timestamps = []
        for entry in entries:
            if 'im:rating' not in entry:
                continue  # RSS 可以包含应用元信息。
            stamp = datetime.fromisoformat(entry['updated']['label'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('missing timezone')
            timestamps.append(stamp.astimezone(UTC))
    if not timestamps:
        raise ValueError('empty publication list')
    return max(timestamps)


def audit_source_dates(leaves):
    """只请求支持的沉默叶；失败/空响应均为 unverified。"""
    results = []
    seen = set()
    for leaf in leaves:
        name = leaf['leaf']
        if not leaf['stalled'] or name in seen:
            continue
        seen.add(name)
        url, app_id = _endpoint(name)
        row = {'leaf': name, 'last_archive_date': leaf['last_archive_date'],
               'checked_at': datetime.now(UTC).isoformat(), 'status': 'unverified',
               'scope': 'latest_publication_date_only'}
        if url is None:
            row['reason'] = 'no_checker'
        else:
            row['source_url'] = url
            try:
                archived = date.fromisoformat(leaf['last_archive_date'])
                response = requests.get(url, timeout=(5, 15))
                response.raise_for_status()
                latest = _latest(name, response.json(), app_id)
                source_date = archive_date_str(latest)
                row.update(latest_source_at=latest.isoformat(), latest_source_date=source_date,
                           status='newer_source_date' if date.fromisoformat(source_date) > archived
                           else 'no_newer_source_date')
            except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
                # 不把响应内容或异常中的请求信息写进持久状态。
                row['reason'] = type(exc).__name__
        results.append(row)
    return results


def print_source_dates(rows):
    labels = {'newer_source_date': '源站有更晚日期',
              'no_newer_source_date': '本次未发现更晚日期', 'unverified': '未能核实'}
    print('【公开源日期核对：仅比对日期，不证明采集完整】')
    for row in rows:
        print(f"  {row['leaf']}: {labels[row['status']]} "
              f"(源日期={row.get('latest_source_date', '未知')}, "
              f"归档日期={row['last_archive_date']}, 原因={row.get('reason', '-')})")
