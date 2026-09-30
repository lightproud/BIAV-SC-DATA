"""backfill_platforms.py 单测：状态机 / 归档去重 / 各平台回溯（网络全打桩）。

global_collectors._get / subprocess (curl) / asyncio 一律 mock；归档写入
monkeypatch ARCHIVE_DIR / STATE_PATH 到 tmp，绝不污染真实 data 树、绝不触网。
"""

import json
import sys
from datetime import datetime, UTC
from unittest import mock

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

from archive_layout import BEIJING_TZ  # noqa: E402  归档分桶时区单一真相源
import backfill_platforms as bp  # noqa: E402
import global_collectors as gc  # noqa: E402


@pytest.fixture
def paths(tmp_path, monkeypatch):
    adir = tmp_path / "platforms"
    spath = tmp_path / "backfill" / "state.json"
    monkeypatch.setattr(bp, "ARCHIVE_DIR", adir)
    monkeypatch.setattr(bp, "STATE_PATH", spath)
    # never actually sleep
    monkeypatch.setattr(bp.time, "sleep", lambda *_: None)
    # default: time is not up
    monkeypatch.setattr(bp, "_is_time_up", lambda: False)
    return adir, spath


def _resp(payload, status=200, text=""):
    r = mock.MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = text
    return r


# ── timing ──────────────────────────────────────────────────────────────────

def test_is_time_up_false_at_start(monkeypatch):
    monkeypatch.setattr(bp, "_start_time", bp.time.time())
    assert bp._is_time_up() is False


def test_is_time_up_true_when_exceeded(monkeypatch):
    monkeypatch.setattr(bp, "_start_time", bp.time.time() - (bp.MAX_RUNTIME_SECONDS + 10))
    assert bp._is_time_up() is True


# ── state load / save / platform_state ──────────────────────────────────────

def test_load_state_missing_returns_empty(paths):
    assert bp._load_state() == {}


def test_save_then_load_roundtrip(paths):
    bp._save_state({"bilibili": {"page": 3, "done": False, "total": 9}})
    assert bp._load_state() == {"bilibili": {"page": 3, "done": False, "total": 9}}


def test_platform_state_initializes_default(paths):
    state = {}
    ps = bp._platform_state(state, "pixiv")
    assert ps == {"page": 1, "done": False, "total": 0}
    assert state["pixiv"] is ps


def test_platform_state_returns_existing(paths):
    state = {"pixiv": {"page": 5, "done": True, "total": 99}}
    ps = bp._platform_state(state, "pixiv")
    assert ps["page"] == 5


# ── _archive_items ──────────────────────────────────────────────────────────

def test_archive_items_empty_noop(paths):
    adir, _ = paths
    bp._archive_items("reddit", [])
    assert not adir.exists() or not any(adir.iterdir())


def test_archive_items_buckets_by_utc8(paths):
    adir, _ = paths
    bp._archive_items("reddit", [
        {"url": "u1", "time": "2026-04-13T20:00:00+00:00", "engagement": 1},
    ])
    out = adir / "reddit" / "2026-04-14.json"
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["item_count"] == 1
    assert data["source"] == "reddit"


def test_archive_items_naive_time_treated_utc(paths):
    adir, _ = paths
    bp._archive_items("reddit", [{"url": "u1", "time": "2026-04-13T20:00:00"}])
    assert (adir / "reddit" / "2026-04-14.json").exists()


def test_archive_items_bad_time_falls_back_to_now(paths):
    adir, _ = paths
    # 归档分桶一律北京日期（archive_layout.archive_today），故断言也须用北京钟：
    # 用 UTC 会在每天 16:00 UTC 之后跨日、令本例无故转红 8 小时。
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    bp._archive_items("reddit", [{"url": "u1", "time": "garbage"}])
    assert (adir / "reddit" / f"{today}.json").exists()


def test_archive_items_dedup_by_url(paths):
    adir, _ = paths
    bp._archive_items("reddit", [
        {"url": "u1", "time": "2026-04-13T20:00:00+00:00"},
        {"url": "u1", "time": "2026-04-13T20:00:00+00:00"},  # dup
        {"url": "u2", "time": "2026-04-13T20:00:00+00:00"},
    ])
    data = json.loads((adir / "reddit" / "2026-04-14.json").read_text(encoding="utf-8"))
    assert {i["url"] for i in data["items"]} == {"u1", "u2"}


def test_archive_items_dedup_by_title_when_no_url(paths):
    adir, _ = paths
    bp._archive_items("reddit", [
        {"title": "same", "source": "reddit", "time": "2026-04-13T20:00:00+00:00"},
        {"title": "same", "source": "reddit", "time": "2026-04-13T20:00:00+00:00"},
    ])
    data = json.loads((adir / "reddit" / "2026-04-14.json").read_text(encoding="utf-8"))
    assert data["item_count"] == 1


def test_archive_items_merges_existing(paths):
    adir, _ = paths
    pdir = adir / "reddit"
    pdir.mkdir(parents=True)
    (pdir / "2026-04-14.json").write_text(
        json.dumps({"items": [{"url": "old"}]}), encoding="utf-8")
    bp._archive_items("reddit", [{"url": "new", "time": "2026-04-13T20:00:00+00:00"}])
    data = json.loads((pdir / "2026-04-14.json").read_text(encoding="utf-8"))
    assert {i["url"] for i in data["items"]} == {"old", "new"}


def test_archive_items_corrupt_existing_ignored(paths):
    adir, _ = paths
    pdir = adir / "reddit"
    pdir.mkdir(parents=True)
    (pdir / "2026-04-14.json").write_text("{bad", encoding="utf-8")
    bp._archive_items("reddit", [{"url": "new", "time": "2026-04-13T20:00:00+00:00"}])
    data = json.loads((pdir / "2026-04-14.json").read_text(encoding="utf-8"))
    assert data["item_count"] == 1


def test_archive_items_sorts_by_engagement(paths):
    adir, _ = paths
    bp._archive_items("reddit", [
        {"url": "low", "time": "2026-04-13T20:00:00+00:00", "engagement": 1},
        {"url": "high", "time": "2026-04-13T20:00:00+00:00", "engagement": 99},
    ])
    data = json.loads((adir / "reddit" / "2026-04-14.json").read_text(encoding="utf-8"))
    assert data["items"][0]["url"] == "high"


# ── backfill_bilibili ───────────────────────────────────────────────────────

def test_backfill_bilibili_done_short_circuits(paths):
    state = {"bilibili": {"page": 1, "done": True, "total": 0}}
    assert bp.backfill_bilibili(state, 5) == 0


def test_backfill_bilibili_collects(paths, monkeypatch):
    pubdate = int(datetime(2026, 4, 13, tzinfo=UTC).timestamp())
    full = _resp({"data": {"result": [
        {"pubdate": pubdate, "title": '<em class="keyword">M</em>orimens',
         "description": "d", "arcurl": "http://x", "play": 20000,
         "favorites": 5, "author": "a"},
    ]}})
    empty = _resp({"data": {"result": []}})
    # alternate full / empty so each keyword finishes quickly
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=[full, empty] * 20))
    state = {}
    n = bp.backfill_bilibili(state, 2)
    assert n >= 1
    assert state["bilibili"]["total"] >= 1


def test_backfill_bilibili_exception_breaks(paths, monkeypatch):
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=RuntimeError("boom")))
    state = {}
    assert bp.backfill_bilibili(state, 2) == 0


# ── backfill_appstore ───────────────────────────────────────────────────────

def test_backfill_appstore_done_short_circuits(paths):
    state = {"appstore": {"page": 1, "done": True, "total": 0}}
    assert bp.backfill_appstore(state, 5) == 0


def test_backfill_appstore_collects_and_marks_done(paths, monkeypatch):
    entry = {
        "im:rating": {"label": "5"},
        "title": {"label": "Great"},
        "content": {"label": "love it"},
        "author": {"name": {"label": "bob"}},
        "updated": {"label": "2026-04-13T12:00:00-07:00"},
    }
    full = _resp({"feed": {"entry": [entry]}})
    empty = _resp({"feed": {"entry": []}})
    # First region first page returns entry, then empty breaks that region
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=[full, empty] * 50))
    state = {}
    # start_page default 1, max_pages 11 so start+max > 10 -> done True
    n = bp.backfill_appstore(state, 11)
    assert n >= 1
    assert state["appstore"]["done"] is True


def test_backfill_appstore_skips_non_dict_rating(paths, monkeypatch):
    entry = {"im:rating": "5"}  # not a dict -> continue/skip
    full = _resp({"feed": {"entry": [entry]}})
    empty = _resp({"feed": {"entry": []}})
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=[full, empty] * 50))
    state = {}
    n = bp.backfill_appstore(state, 3)
    assert n == 0


# ── backfill_arca_live ──────────────────────────────────────────────────────

ARCA_HTML = (
    '<div data-url="/b/forgettingeve/123">'
    '<a class="title">Hello Morimens</a>'
    '<span class="col-time">2026-04-13</span></div>'
)


def test_backfill_arca_done_short_circuits(paths):
    state = {"arca_live": {"page": 1, "done": True, "total": 0}}
    assert bp.backfill_arca_live(state, 5) == 0


def test_backfill_arca_collects_then_done_on_empty(paths, monkeypatch):
    page1 = _resp({}, text=ARCA_HTML)
    page2 = _resp({}, text="<html>no matches</html>")
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=[page1, page2]))
    state = {}
    n = bp.backfill_arca_live(state, 5)
    assert n == 1
    assert state["arca_live"]["done"] is True


def test_backfill_arca_exception_breaks(paths, monkeypatch):
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=RuntimeError("net")))
    state = {}
    assert bp.backfill_arca_live(state, 5) == 0


# ── backfill_steam_reviews ──────────────────────────────────────────────────

def test_backfill_steam_done_short_circuits(paths):
    state = {"steam_review": {"page": 1, "done": True, "total": 0}}
    assert bp.backfill_steam_reviews(state, 5) == 0


def _curl(stdout, returncode=0):
    r = mock.MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    return r


def test_backfill_steam_collects(paths, monkeypatch):
    ts = int(datetime(2026, 4, 13, tzinfo=UTC).timestamp())
    body = json.dumps({"reviews": [
        {"timestamp_created": ts, "language": "en", "voted_up": True,
         "review": "x" * 60, "author": {"steamid": "s1"}, "votes_up": 20},
    ], "cursor": "next"})
    empty = json.dumps({"reviews": [], "cursor": "next2"})
    import subprocess as sp
    monkeypatch.setattr(sp, "run", mock.Mock(side_effect=[_curl(body), _curl(empty)]))
    state = {}
    n = bp.backfill_steam_reviews(state, 5)
    assert n == 1
    assert state["steam_review"]["cursor"] == "next"


def test_backfill_steam_curl_failure_breaks(paths, monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(sp, "run", mock.Mock(return_value=_curl("", returncode=1)))
    state = {}
    assert bp.backfill_steam_reviews(state, 5) == 0


def test_backfill_steam_empty_stdout_breaks(paths, monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(sp, "run", mock.Mock(return_value=_curl("   ")))
    state = {}
    assert bp.backfill_steam_reviews(state, 5) == 0


def test_backfill_steam_same_cursor_marks_done(paths, monkeypatch):
    body = json.dumps({"reviews": [
        {"timestamp_created": 1700000000, "language": "en", "voted_up": False,
         "review": "short", "author": {"steamid": "s1"}, "votes_up": 1},
    ], "cursor": "*"})  # cursor unchanged from start '*'
    import subprocess as sp
    monkeypatch.setattr(sp, "run", mock.Mock(return_value=_curl(body)))
    state = {}
    n = bp.backfill_steam_reviews(state, 5)
    assert n == 0
    assert state["steam_review"]["done"] is True


# ── backfill_pixiv ──────────────────────────────────────────────────────────

def test_backfill_pixiv_done_short_circuits(paths):
    state = {"pixiv": {"page": 1, "done": True, "total": 0}}
    assert bp.backfill_pixiv(state, 5) == 0


def test_backfill_pixiv_collects(paths, monkeypatch):
    work = {"title": "art", "description": "d", "createDate": "2026-04-13T12:00:00+00:00",
            "id": "999", "bookmarkCount": 200, "userName": "painter"}
    full = _resp({"body": {"illustManga": {"data": [work]}}})
    empty = _resp({"body": {"illustManga": {"data": []}}})
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=[full, empty] * 20))
    state = {}
    n = bp.backfill_pixiv(state, 2)
    assert n >= 1


def test_backfill_pixiv_non200_breaks(paths, monkeypatch):
    monkeypatch.setattr(gc, "_get", mock.Mock(return_value=_resp({}, status=403)))
    state = {}
    assert bp.backfill_pixiv(state, 2) == 0


# ── backfill_ruliweb ────────────────────────────────────────────────────────

RULIWEB_HTML = (
    '<span class="date">2026.04.13</span>'
    '<a class="subject_link" href="/best/board/300143/12345"> Morimens topic </a>'
)


def test_backfill_ruliweb_done_short_circuits(paths):
    state = {"ruliweb": {"page": 1, "done": True, "total": 0}}
    assert bp.backfill_ruliweb(state, 5) == 0


def test_backfill_ruliweb_collects_then_done(paths, monkeypatch):
    page1 = _resp({}, text=RULIWEB_HTML)
    page2 = _resp({}, text="<html></html>")
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=[page1, page2] * 5))
    state = {}
    n = bp.backfill_ruliweb(state, 5)
    assert n >= 1


def test_backfill_ruliweb_exception_breaks(paths, monkeypatch):
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=RuntimeError("x")))
    state = {}
    assert bp.backfill_ruliweb(state, 5) == 0


# ── backfill_weixin ─────────────────────────────────────────────────────────

WEIXIN_HTML = (
    '<h3><a href="https://mp.weixin.qq.com/s/abc" data-t="1744545600">'
    'Morimens <em>news</em></a></h3>'
)


def test_backfill_weixin_done_short_circuits(paths):
    state = {"weixin": {"page": 1, "done": True, "total": 0}}
    assert bp.backfill_weixin(state, 5) == 0


def test_backfill_weixin_collects_then_done(paths, monkeypatch):
    page1 = _resp({}, text=WEIXIN_HTML)
    page2 = _resp({}, text="<html></html>")
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=[page1, page2] * 5))
    state = {}
    n = bp.backfill_weixin(state, 5)
    assert n >= 1


def test_backfill_weixin_exception_breaks(paths, monkeypatch):
    monkeypatch.setattr(gc, "_get", mock.Mock(side_effect=RuntimeError("x")))
    state = {}
    assert bp.backfill_weixin(state, 5) == 0


# ── backfill_taptap ─────────────────────────────────────────────────────────

def test_backfill_taptap_browser_failure_returns_zero(paths, monkeypatch):
    # asyncio.run raising simulates missing browser env
    import asyncio
    # taptap_collector 必须一并替身：不替身则 `taptap_collector.collect(...)` 会真造出一个
    # 协程对象，而 asyncio.run 被替身成抛异常 => 协程永不被 await，Python 在**任意后续
    # 时点**抛 "coroutine 'collect' was never awaited" RuntimeWarning。该告警会随 GC
    # 时机挂到别的用例名下（本仓实测每轮换一个受害者），是不可复现失败的经典来源；
    # -W error::RuntimeWarning 下更会直接把无关用例判红。
    monkeypatch.setitem(sys.modules, "taptap_collector", mock.MagicMock())
    monkeypatch.setattr(asyncio, "run", mock.Mock(side_effect=RuntimeError("no chromium")))
    state = {}
    assert bp.backfill_taptap(state, 5) == 0


def test_backfill_taptap_collects(paths, monkeypatch):
    import asyncio
    topics = [{"url": "t1", "time": "2026-04-13T20:00:00+00:00"}]
    reviews = [{"url": "r1", "time": "2026-04-13T20:00:00+00:00"}]
    fake_mod = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "taptap_collector", fake_mod)
    monkeypatch.setattr(asyncio, "run", mock.Mock(return_value=(topics, reviews)))
    state = {}
    n = bp.backfill_taptap(state, 5)
    assert n == 2
    assert state["taptap"]["total"] == 2


# ── show_status ─────────────────────────────────────────────────────────────

def test_show_status_smoke(paths, capsys):
    state = {"bilibili": {"page": 3, "done": False, "total": 12},
             "pixiv": {"done": True, "total": 5}}
    bp.show_status(state)
    out = capsys.readouterr().out
    assert "历史回溯进度" in out
    assert "bilibili" in out
    assert "完成" in out


# ── main ────────────────────────────────────────────────────────────────────

def _run_main(argv):
    with mock.patch.object(sys, "argv", ["backfill_platforms.py", *argv]):
        bp.main()


def test_main_status(paths, capsys):
    bp._save_state({"bilibili": {"page": 2, "done": False, "total": 4}})
    _run_main(["--status"])
    assert "历史回溯进度" in capsys.readouterr().out


def test_main_unknown_platform(paths, capsys):
    _run_main(["--platform", "nope"])
    # error logged, no crash; registry untouched
    assert not (paths[1]).exists()


def test_main_single_platform(paths, monkeypatch, capsys):
    fake = mock.Mock(return_value=7)
    monkeypatch.setitem(bp.BACKFILL_REGISTRY, "bilibili", fake)
    _run_main(["--platform", "bilibili", "--pages", "3"])
    fake.assert_called_once()
    assert fake.call_args[0][1] == 3


def test_main_all_platforms_runs_each(paths, monkeypatch):
    calls = []

    def make(name):
        def fn(state, pages):
            calls.append(name)
            return 1
        return fn

    fake_registry = {n: make(n) for n in ["bilibili", "pixiv"]}
    monkeypatch.setattr(bp, "BACKFILL_REGISTRY", fake_registry)
    _run_main([])
    assert set(calls) == {"bilibili", "pixiv"}


def test_main_all_skips_done_platforms(paths, monkeypatch):
    calls = []

    def fn(state, pages):
        calls.append("ran")
        return 0

    monkeypatch.setattr(bp, "BACKFILL_REGISTRY", {"bilibili": fn})
    bp._save_state({"bilibili": {"page": 1, "done": True, "total": 0}})
    _run_main([])
    assert calls == []  # done platform skipped


def test_main_all_stops_when_time_up(paths, monkeypatch):
    calls = []

    def fn(state, pages):
        calls.append("ran")
        return 0

    monkeypatch.setattr(bp, "BACKFILL_REGISTRY", {"bilibili": fn, "pixiv": fn})
    monkeypatch.setattr(bp, "_is_time_up", lambda: True)
    _run_main([])
    assert calls == []  # time up before first platform
