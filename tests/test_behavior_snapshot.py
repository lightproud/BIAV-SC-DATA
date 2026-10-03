"""behavior_snapshot.py 单测（无网络：monkeypatch fetch_text，自造各源响应，档案写 tmp_path）。"""
import json
from datetime import datetime, timezone

import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import behavior_snapshot as bs

# 2026-10-03 22:30 UTC = 北京时间 2026-10-04 06:30（跨日，专测 UTC+8 取日）
NOW = datetime(2026, 10, 3, 22, 30, 0, tzinfo=timezone.utc)


def _nuxt(review_count=3308, extra_stats=None):
    # devalue 形态：对象值是下标，数字本身存在平铺数组里
    arr = [
        {"stats": 1},
        {"reserve_count": 2, "review_count": 3, "fans_count": 4, "wish_count": 5},
        212950, review_count, 291322, 75,
    ]
    return f'<script type="application/json" id="__NUXT_DATA__">{json.dumps(arr)}</script>'


def taptap_html(rating=7.5, count=3308, nuxt=True, review_count=3308):
    ld = {"@type": "MobileApplication",
          "aggregateRating": {"@type": "AggregateRating", "ratingCount": count, "ratingValue": rating}}
    return ('<html><script type="application/ld+json" data-hid="x">'
            + json.dumps(ld) + '</script>' + (_nuxt(review_count) if nuxt else '') + '</html>')


def gp_html(rv="3.88", rc="6777", tier="100K+", unit="Downloads"):
    ld = {"aggregateRating": {"ratingValue": rv, "ratingCount": rc}}
    inst = f'<div class="ClM7O">{tier}</div><div class="g1rdde">{unit}</div>' if tier else ''
    return f'<script type="application/ld+json">{json.dumps(ld)}</script>{inst}'


YT_XML = """<?xml version="1.0"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
<entry><yt:videoId>AAA</yt:videoId><published>2026-09-30T10:00:16+00:00</published>
<media:group><media:community><media:starRating count="17" average="5.00"/><media:statistics views="356"/></media:community></media:group></entry>
<entry><yt:videoId>BBB</yt:videoId><published>2026-09-18T09:00:17+00:00</published>
<media:group><media:community><media:starRating count="75"/><media:statistics views="1875"/></media:community></media:group></entry>
</feed>"""

# 2026-10-03 与 2026-10-02 的 UTC 0 点
D1002, D1003 = 1790899200, 1790985600
SC_DATA = [[1722470400000, 360], [1725148800000, 644],           # 月粒度
           [1790899200000, 500], [1790985600000, 800],            # 日粒度（间隔 24h）
           [1791054000000, 569], [1791057600000, 500], [1791061200000, 476]]  # 小时粒度


def routes(overrides=None):
    r = {
        "discord.com/api/v10/invites/morimens": json.dumps(
            {"approximate_member_count": 63216, "approximate_presence_count": 12764,
             "guild": {"id": "1131791637933199470"}}),
        "discord.com/api/v10/invites/2hsGcAPcs9": json.dumps(
            {"approximate_member_count": 4752, "approximate_presence_count": 234}),
        "GetNumberOfCurrentPlayers/v1/?appid=3052450": json.dumps({"response": {"player_count": 427}}),
        "GetNumberOfCurrentPlayers/v1/?appid=4226130": json.dumps({"response": {"player_count": 25}}),
        "steamcharts.com/app/3052450": json.dumps(SC_DATA),
        "steamcharts.com/app/4226130": json.dumps(SC_DATA),
        "appreviewhistogram/3052450": json.dumps({"results": {"recent": [
            {"date": D1002, "recommendations_up": 8, "recommendations_down": 1},
            {"date": D1003, "recommendations_up": 6, "recommendations_down": 3}]}}),
        "appreviewhistogram/4226130": json.dumps({"results": {"recent": [
            {"date": D1003, "recommendations_up": 1, "recommendations_down": 0}]}}),
        "taptap.cn/app/364992": taptap_html(),
        "itunes.apple.com/lookup?id=6447354150&country=us": json.dumps({"results": [{
            "averageUserRating": 4.60377, "userRatingCount": 424, "version": "2.5.1",
            "currentVersionReleaseDate": "2026-04-20T00:37:01Z"}]}),
        "itunes.apple.com/lookup?id=6743462069&country=jp": json.dumps({"results": [{
            "averageUserRating": 4.79697, "userRatingCount": 5226, "version": "1.5.1",
            "currentVersionReleaseDate": "2026-03-12T05:50:09Z"}]}),
        "id=com.qookkagames.z1.gp.hk": gp_html(),
        "id=jp.co.altplus.boukyakuzenya": gp_html("4.7", "3154", "10万+", "ダウンロード"),
        "youtube.com/feeds/videos.xml": YT_XML,
        "morimens.fandom.com/api.php": json.dumps({"query": {"statistics": {
            "pages": 4354, "articles": 882, "edits": 13095, "activeusers": 18}}}),
    }
    for lang, (pos, neg) in {"schinese": (1401, 477), "japanese": (35, 12),
                             "english": (1674, 338), "all": (4397, 1086)}.items():
        for app in ("3052450", "4226130"):
            r[f"appreviews/{app}?json=1&num_per_page=0&language={lang}&"] = json.dumps(
                {"query_summary": {"total_positive": pos, "total_negative": neg}})
    r.update(overrides or {})
    return r


@pytest.fixture
def net(monkeypatch):
    table = routes()
    calls = []

    def fake(url, min_interval=1.0, lang="en"):
        calls.append(url)
        for key, body in table.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise bs.SourceError(f"未模拟的 URL {url}")

    monkeypatch.setattr(bs, "fetch_text", fake)
    fake.table = table
    fake.calls = calls
    return fake


def read(out, source, year="2026"):
    p = out / source / f"{year}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ 解析 ----
def test_discord(net):
    recs = bs.collect_discord()
    assert recs[0]["metrics"] == {"member_count": 63216, "presence_count": 12764,
                                  "guild_id": "1131791637933199470"}
    assert {r["target"] for r in recs} == {"morimens", "2hsGcAPcs9"}


def test_steam_metrics(net):
    g, jp = bs.collect_steam(NOW)
    m = g["metrics"]
    assert m["current_players"] == 427
    assert m["peak_24h_steamcharts"] == 800          # 最近 24h 内最高点（含 10-03 00:00 UTC 的 800）
    # 北京时间 10-04 → 昨日 10-03
    assert (m["yesterday_date"], m["yesterday_up"], m["yesterday_down"]) == ("2026-10-03", 6, 3)
    assert m["reviews"]["schinese"] == {"total_positive": 1401, "total_negative": 477}
    assert set(m["reviews"]) == {"schinese", "japanese", "english", "all"}
    assert jp["metrics"]["current_players"] == 25
    # 评价请求必须带 filter=all&purchase_type=all（否则 Steam 返回全 0）
    assert any("language=all&filter=all&purchase_type=all" in u for u in net.calls)


def test_steam_degrades_without_steamcharts_and_histogram(net):
    net.table["steamcharts.com/app/4226130"] = bs.SourceError("404")
    net.table["appreviewhistogram/4226130"] = bs.SourceError("boom")
    g, jp = bs.collect_steam(NOW)
    assert "peak_24h_steamcharts" not in jp["metrics"]
    assert "yesterday_up" not in jp["metrics"]
    assert jp["metrics"]["current_players"] == 25
    assert any("steamcharts" in w for w in jp["warnings"])
    assert "peak_24h_steamcharts" in g["metrics"]


def test_taptap_full(net):
    (rec,) = bs.collect_taptap()
    assert rec["metrics"] == {"rating_value": 7.5, "rating_count": 3308, "reserve_count": 212950,
                              "fans_count": 291322, "wish_count": 75}
    assert rec["warnings"] == []


def test_taptap_unlocatable_nuxt_only_jsonld(net):
    net.table["taptap.cn/app/364992"] = taptap_html(nuxt=False)
    (rec,) = bs.collect_taptap()
    assert rec["metrics"] == {"rating_value": 7.5, "rating_count": 3308}
    assert any("无法唯一定位" in w for w in rec["warnings"])


def test_taptap_nuxt_mismatch_not_guessed(net):
    net.table["taptap.cn/app/364992"] = taptap_html(review_count=999)
    (rec,) = bs.collect_taptap()
    assert "fans_count" not in rec["metrics"] and "reserve_count" not in rec["metrics"]
    assert any("不符" in w for w in rec["warnings"])


def test_appstore(net):
    g, jp = bs.collect_appstore()
    assert g["metrics"]["average_rating"] == 4.6038 and g["metrics"]["rating_count"] == 424
    assert g["metrics"]["version"] == "2.5.1" and g["metrics"]["country"] == "us"
    assert jp["metrics"]["country"] == "jp" and jp["metrics"]["rating_count"] == 5226


def test_google_play(net):
    g, jp = bs.collect_google_play()
    assert g["metrics"]["rating_value"] == 3.88 and g["metrics"]["rating_count"] == 6777
    assert g["metrics"]["installs_tier"] == "100K+"
    assert jp["metrics"]["installs_tier"] == "10万+"


def test_google_play_missing_installs_is_warning(net):
    net.table["id=com.qookkagames.z1.gp.hk"] = gp_html(tier="")
    g, _ = bs.collect_google_play()
    assert "installs_tier" not in g["metrics"]
    assert any("安装档位" in w for w in g["warnings"])


def test_youtube(net):
    (rec,) = bs.collect_youtube()
    assert rec["metrics"]["videos"][0] == {
        "video_id": "AAA", "published": "2026-09-30T10:00:16+00:00", "views": 356, "star_count": 17}
    assert len(rec["metrics"]["videos"]) == 2


def test_fandom(net):
    (rec,) = bs.collect_fandom()
    assert rec["metrics"] == {"pages": 4354, "articles": 882, "edits": 13095, "activeusers": 18}


# ----------------------------------------------------------- 隔离 / 幂等 ----
def test_one_source_failure_is_isolated(net, tmp_path):
    net.table["morimens.fandom.com/api.php"] = bs.SourceError("fandom 挂了")
    res = bs.run_snapshot(tmp_path, now=NOW)
    assert list(res["errors"]) == ["fandom"]
    assert not (tmp_path / "fandom").exists()
    for src in ("discord", "steam", "taptap", "appstore", "google_play", "youtube"):
        assert (tmp_path / src).exists(), src
    assert bs.main(["--out", str(tmp_path / "x"), "--only", "fandom"]) == 1


def test_unexpected_exception_also_isolated(net, tmp_path):
    net.table["itunes.apple.com/lookup?id=6447354150&country=us"] = "not json"
    net.table["itunes.apple.com/lookup?id=6743462069&country=jp"] = "not json"
    res = bs.run_snapshot(tmp_path, now=NOW)
    assert "appstore" in res["errors"]
    assert (tmp_path / "discord").exists()


def test_row_shape_and_date_is_utc8(net, tmp_path):
    bs.run_snapshot(tmp_path, only=["fandom"], now=NOW)
    (row,) = read(tmp_path, "fandom")
    assert row == {"date": "2026-10-04", "ts": "2026-10-03T22:30:00Z", "source": "fandom",
                   "target": "morimens", "metrics": {"pages": 4354, "articles": 882,
                                                     "edits": 13095, "activeusers": 18}}


def test_same_day_rerun_overwrites_not_duplicates(net, tmp_path):
    bs.run_snapshot(tmp_path, now=NOW)
    net.table["morimens.fandom.com/api.php"] = json.dumps({"query": {"statistics": {
        "pages": 5000, "articles": 900, "edits": 14000, "activeusers": 20}}})
    later = datetime(2026, 10, 4, 3, 0, 0, tzinfo=timezone.utc)  # 仍是北京时间 10-04
    bs.run_snapshot(tmp_path, now=later)
    rows = read(tmp_path, "fandom")
    assert len(rows) == 1 and rows[0]["metrics"]["pages"] == 5000
    assert rows[0]["ts"] == "2026-10-04T03:00:00Z"
    assert len(read(tmp_path, "discord")) == 2       # 两个邀请码各一行
    # 次日 = 新增一行
    bs.run_snapshot(tmp_path, only=["fandom"], now=datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc))
    assert [r["date"] for r in read(tmp_path, "fandom")] == ["2026-10-04", "2026-10-05"]


def test_dry_run_writes_nothing(net, tmp_path):
    res = bs.run_snapshot(tmp_path, dry_run=True, now=NOW)
    assert res["rows"] and not any(tmp_path.iterdir())


def test_year_split(net, tmp_path):
    bs.run_snapshot(tmp_path, only=["fandom"], now=datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc))
    # UTC 12-31 20:00 = 北京 2027-01-01 04:00 → 落 2027.jsonl
    assert (tmp_path / "fandom" / "2027.jsonl").exists()


# ----------------------------------------------------------------- 回填 ----
def test_backfill_granularity_and_years(net, tmp_path):
    res = bs.backfill_steamcharts(tmp_path)
    assert res["errors"] == {} and res["rows"] == 2 * len(SC_DATA)
    r24 = read(tmp_path, "steamcharts", "2024")
    assert {r["metrics"]["granularity"] for r in r24} == {"month"}
    assert {r["date"][:7] for r in r24} == {"2024-08", "2024-09"}
    r26 = [r for r in read(tmp_path, "steamcharts", "2026") if r["target"] == "global"]
    grans = [r["metrics"]["granularity"] for r in r26]
    assert grans == ["day", "day", "hour", "hour", "hour"]


def test_backfill_idempotent(net, tmp_path):
    bs.backfill_steamcharts(tmp_path)
    first = {y: (tmp_path / "steamcharts" / f"{y}.jsonl").read_text(encoding="utf-8")
             for y in ("2024", "2026")}
    bs.backfill_steamcharts(tmp_path)
    for y, text in first.items():
        assert (tmp_path / "steamcharts" / f"{y}.jsonl").read_text(encoding="utf-8") == text


def test_backfill_one_region_failure_isolated(net, tmp_path):
    net.table["steamcharts.com/app/4226130"] = bs.SourceError("404")
    res = bs.backfill_steamcharts(tmp_path)
    assert list(res["errors"]) == ["steamcharts/jp"]
    assert res["rows"] == len(SC_DATA)
    assert bs.main(["--out", str(tmp_path / "y"), "--backfill-steamcharts"]) == 1


def test_main_unknown_source():
    assert bs.main(["--only", "nope", "--dry-run"]) == 2
