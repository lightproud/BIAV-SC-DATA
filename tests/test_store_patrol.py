"""store_patrol.py 单测（无网络：注入假 fetch，档案写 tmp_path）。"""
import json
import urllib.error

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import store_patrol as sp

APPDETAILS = {
    "id": "steam-appdetails",
    "title": "Morimens Steam storefront (price / release surface)",
    "url": "https://store.example/appdetails",
    "extract": "steam-appdetails",
    "key": "3052450",
}
REVIEWS = {
    "id": "steam-review-summary",
    "title": "Morimens Steam review summary",
    "url": "https://store.example/appreviews",
    "extract": "steam-review-summary",
}


def _appdetails_body(key="3052450"):
    return json.dumps({key: {"success": True, "data": {
        "type": "game", "name": "Morimens", "steam_appid": 3052450, "is_free": True,
        "release_date": {"coming_soon": False, "date": "Aug 1, 2024"}}}})


def _reviews_body(pos=4361):
    return json.dumps({"success": 1, "query_summary": {
        "num_reviews": 0, "review_score": 8, "review_score_desc": "Very Positive",
        "total_positive": pos, "total_negative": 1079, "total_reviews": pos + 1079}})


def _fetcher(bodies):
    def fetch(url):
        body = bodies[url]
        if isinstance(body, Exception):
            raise body
        return body
    return fetch


def _run(tmp_path, bodies, targets=(APPDETAILS, REVIEWS), **kw):
    kw.setdefault("today", "2026-09-28")
    return sp.run_patrol(list(targets), tmp_path, fetch=_fetcher(bodies),
                         sleep=lambda s: None, log=lambda m: None, **kw)


def test_snapshot_written_in_original_format(tmp_path):
    rep = _run(tmp_path, {APPDETAILS["url"]: _appdetails_body(),
                          REVIEWS["url"]: _reviews_body()})
    assert rep["failures"] == []
    assert sorted(rep["changes"]) == ["steam-appdetails", "steam-review-summary"]

    day = tmp_path / "steam-appdetails" / "2026-09-28.json"
    raw = day.read_text(encoding="utf-8")
    snap = json.loads(raw)
    # 与 JS JSON.stringify(v, null, 2) + '\n' 同形
    assert raw == json.dumps(snap, indent=2, ensure_ascii=False) + "\n"
    assert list(snap) == ["target", "title", "url", "checked_at", "signature"]
    assert snap["signature"] == {"name": "Morimens", "type": "game", "is_free": True,
                                 "price": None, "release_date": "Aug 1, 2024",
                                 "coming_soon": False}
    assert snap["checked_at"].endswith("Z") and len(snap["checked_at"]) == 24
    assert (tmp_path / "steam-appdetails" / "latest.json").read_text() == raw

    line = (tmp_path / "steam-appdetails" / "changes.jsonl").read_text().splitlines()
    assert len(line) == 1
    entry = json.loads(line[0])
    assert list(entry) == ["at", "target", "from", "to"]
    assert entry["from"] is None and entry["to"] == snap["signature"]
    assert " " not in line[0].replace("Aug 1, 2024", "")  # 紧凑单行

    rev = json.loads((tmp_path / "steam-review-summary" / "2026-09-28.json").read_text())
    assert "num_reviews" not in rev["signature"]
    state = json.loads((tmp_path / "state" / "patrol-state.json").read_text())
    assert state["targets"]["steam-appdetails"]["last_success_date"] == "2026-09-28"


def test_unchanged_signature_no_change_entry(tmp_path):
    bodies = {APPDETAILS["url"]: _appdetails_body(), REVIEWS["url"]: _reviews_body()}
    _run(tmp_path, bodies, today="2026-09-27")
    latest_before = (tmp_path / "steam-appdetails" / "latest.json").read_text()

    bodies[REVIEWS["url"]] = _reviews_body(pos=4400)
    rep = _run(tmp_path, bodies, today="2026-09-28")
    assert rep["changes"] == ["steam-review-summary"]
    assert rep["results"]["steam-appdetails"] == "unchanged"
    # 日快照照写，但 latest 不动、变更日志不追加
    assert (tmp_path / "steam-appdetails" / "2026-09-28.json").exists()
    assert (tmp_path / "steam-appdetails" / "latest.json").read_text() == latest_before
    assert len((tmp_path / "steam-appdetails" / "changes.jsonl").read_text().splitlines()) == 1
    rlines = (tmp_path / "steam-review-summary" / "changes.jsonl").read_text().splitlines()
    assert len(rlines) == 2
    assert json.loads(rlines[1])["from"]["total_positive"] == 4361


def test_same_day_success_skipped_unless_forced(tmp_path):
    calls = []
    bodies = {APPDETAILS["url"]: _appdetails_body(), REVIEWS["url"]: _reviews_body()}

    def fetch(url):
        calls.append(url)
        return bodies[url]
    kw = dict(today="2026-09-28", sleep=lambda s: None, log=lambda m: None)
    sp.run_patrol([APPDETAILS, REVIEWS], tmp_path, fetch=fetch, **kw)
    rep = sp.run_patrol([APPDETAILS, REVIEWS], tmp_path, fetch=fetch, **kw)
    assert len(calls) == 2
    assert set(rep["results"].values()) == {"skipped"}
    sp.run_patrol([APPDETAILS, REVIEWS], tmp_path, fetch=fetch, force=True, **kw)
    assert len(calls) == 4


def test_appdetails_rekeyed_response_falls_back_to_steam_appid(tmp_path):
    rep = _run(tmp_path, {APPDETAILS["url"]: _appdetails_body(key="5224390")},
               targets=[APPDETAILS])
    assert rep["failures"] == []


def test_partial_failure_keeps_other_targets_and_exits_nonzero(tmp_path, monkeypatch):
    bodies = {APPDETAILS["url"]: urllib.error.URLError("boom"),
              REVIEWS["url"]: _reviews_body()}
    rep = _run(tmp_path, bodies)
    assert rep["failures"] == ["steam-appdetails"]
    assert (tmp_path / "steam-review-summary" / "2026-09-28.json").exists()
    assert not (tmp_path / "steam-appdetails").exists()
    st = json.loads((tmp_path / "state" / "patrol-state.json").read_text())
    ad = st["targets"]["steam-appdetails"]
    assert ad["last_status"] == "failed" and ad["last_attempts"] == sp.MAX_ATTEMPTS
    assert ad["consecutive_failures"] == 1 and "boom" in ad["last_error"]

    # main() 层：写完成功目标后返回非零
    cfg = tmp_path / "targets.json"
    cfg.write_text(json.dumps({"targets": [APPDETAILS, REVIEWS]}))
    out = tmp_path / "out"
    monkeypatch.setattr(sp, "fetch_text", _fetcher(
        {APPDETAILS["url"]: '{"3052450": {"success": false}}', REVIEWS["url"]: _reviews_body()}))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    assert sp.main(["--targets", str(cfg), "--out", str(out), "--today", "2026-09-28"]) == 1
    assert (out / "steam-review-summary" / "2026-09-28.json").exists()
    assert not (out / "steam-appdetails" / "2026-09-28.json").exists()


def test_dry_run_writes_nothing(tmp_path):
    rep = _run(tmp_path, {APPDETAILS["url"]: _appdetails_body(),
                          REVIEWS["url"]: _reviews_body()}, dry_run=True)
    assert rep["failures"] == []
    assert list(tmp_path.iterdir()) == []


def test_corrupt_latest_and_state_self_heal(tmp_path):
    (tmp_path / "steam-review-summary").mkdir()
    (tmp_path / "steam-review-summary" / "latest.json").write_text("{bad")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "patrol-state.json").write_text("<<<<<<< HEAD")
    rep = _run(tmp_path, {REVIEWS["url"]: _reviews_body()}, targets=[REVIEWS])
    assert rep["changes"] == ["steam-review-summary"]
    assert any(p.name.startswith("patrol-state.json.corrupt-")
               for p in (tmp_path / "state").iterdir())


def test_registry_extractors_cover_all_targets():
    cfg = json.loads(sp.DEFAULT_TARGETS.read_text(encoding="utf-8"))
    assert cfg["targets"]
    for t in cfg["targets"]:
        assert t["extract"] in sp.EXTRACTORS
        assert t["url"].startswith("https://")


@pytest.mark.parametrize("body", ["null", "[]", '{"query_summary": null}'])
def test_review_summary_bad_body_raises(body):
    with pytest.raises(ValueError):
        sp._extract_steam_review_summary(body, REVIEWS)
