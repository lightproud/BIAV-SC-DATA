import os
import socket
import unittest
from datetime import datetime, timedelta, UTC
from unittest import mock

import requests

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import news_common


class TestStripHtml(unittest.TestCase):
    def test_removes_tags(self):
        self.assertEqual(news_common.strip_html("<a href='x'>link</a> text"), "link text")

    def test_empty_and_none_safe(self):
        self.assertEqual(news_common.strip_html(""), "")
        self.assertEqual(news_common.strip_html(None), "")

    def test_plain_text_untouched(self):
        self.assertEqual(news_common.strip_html("a < b and c"), "a < b and c")


class TestMakeItem(unittest.TestCase):
    def test_basic_fields_and_defaults(self):
        item = news_common.make_item(
            "Title", "Summary", "reddit", "global", "2026-06-09T00:00:00+00:00",
            "https://r.com/1",
        )
        self.assertEqual(item["title"], "Title")
        self.assertEqual(item["summary"], "Summary")
        self.assertEqual(item["source"], "reddit")
        self.assertEqual(item["engagement"], 0)
        self.assertFalse(item["is_hot"])
        self.assertEqual(item["tags"], [])
        self.assertEqual(item["content_type"], "text")
        self.assertEqual(item["media_url"], "")
        # flag is omitted entirely when not approximate
        self.assertNotIn("time_is_approximate", item)

    def test_html_stripped_and_trimmed(self):
        item = news_common.make_item(
            " <b>T</b> ", "<p>S</p>", "nga", "cn", "2026-06-09T00:00:00+00:00", "u")
        self.assertEqual(item["title"], "T")
        self.assertEqual(item["summary"], "S")

    def test_none_title_and_url_safe(self):
        item = news_common.make_item(None, None, "nga", "cn", "t", None)
        self.assertEqual(item["title"], "")
        self.assertEqual(item["summary"], "")
        self.assertEqual(item["url"], "")

    def test_author_coerced_to_str(self):
        item = news_common.make_item("T", "", "nga", "cn", "t", "u", author=123)
        self.assertEqual(item["author"], "123")

    def test_time_is_approximate_flag_set(self):
        item = news_common.make_item("T", "", "nga", "cn", "t", "u",
                                     time_is_approximate=True)
        self.assertIs(item["time_is_approximate"], True)


class TestValidateNewsItem(unittest.TestCase):
    """守 `validate_news_item` 的白名单重建**不吃掉归档必需的字段**。

    该函数不是过滤器而是**重建器**：它丢掉白名单外的一切。所以每一个下游真正要读的
    字段都得在这里有一条往返测试——否则字段没了也没人红，只有几个月后对着归档层做
    对账时才发现（2026-09-17 实测：全量档案层 58,207 条里带 `time_is_approximate`
    者 0 条，采集层却在 20 处认真地打了这个标记）。
    """

    BASE = {
        'title': 'T', 'source': 'reddit', 'engagement': 3,
        'time': '2026-09-18T00:00:00+00:00', 'url': 'https://example.com/1',
    }

    def _clean(self, **extra):
        ok, cleaned = news_common.validate_news_item({**self.BASE, **extra})
        self.assertTrue(ok)
        return cleaned

    def test_approximate_flag_survives(self):
        """`time_is_approximate` 必须活着到归档层：`archive_platforms.item_key` 据它
        把「猜出来的时刻」排除出去重键，标记一丢，每轮不同的 now() 就成了条目身份。"""
        self.assertIs(self._clean(time_is_approximate=True)['time_is_approximate'], True)

    def test_approximate_flag_absent_when_not_set(self):
        self.assertNotIn('time_is_approximate', self._clean())

    def test_archive_layout_fields_survive(self):
        cleaned = self._clean(region='jp', archive_subtype='review')
        self.assertEqual(cleaned['region'], 'jp')
        self.assertEqual(cleaned['archive_subtype'], 'review')

    def test_make_item_roundtrip_keeps_flag(self):
        """采集器真正走的那条路：make_item 打标 → validate 重建 → 标记仍在。"""
        item = news_common.make_item(
            'T', '', 'reddit', 'global', '2026-09-18T00:00:00+00:00',
            'https://example.com/2', engagement=1, time_is_approximate=True)
        ok, cleaned = news_common.validate_news_item(item)
        self.assertTrue(ok)
        self.assertIs(cleaned['time_is_approximate'], True)


def _addrinfo(*ips):
    """Build socket.getaddrinfo-shaped results for the given IP strings."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class TestIsSafeUrl(unittest.TestCase):
    """SSRF guard — DNS resolution is mocked, no real lookups."""

    def _check(self, url, ips=None, side_effect=None):
        with mock.patch.object(news_common.socket, "getaddrinfo",
                               side_effect=side_effect,
                               return_value=_addrinfo(*(ips or []))) as gai:
            result = news_common.is_safe_url(url)
        return result, gai

    def test_public_ip_accepted(self):
        result, _ = self._check("https://example.com/page", ips=["93.184.216.34"])
        self.assertTrue(result)

    def test_http_scheme_also_allowed(self):
        result, _ = self._check("http://example.com", ips=["93.184.216.34"])
        self.assertTrue(result)

    def test_private_10_rejected(self):
        result, _ = self._check("https://internal.example.com", ips=["10.0.0.5"])
        self.assertFalse(result)

    def test_loopback_127_rejected(self):
        result, _ = self._check("https://localhost", ips=["127.0.0.1"])
        self.assertFalse(result)

    def test_link_local_169_254_rejected(self):
        # AWS metadata endpoint class
        result, _ = self._check("http://metadata.example.com", ips=["169.254.169.254"])
        self.assertFalse(result)

    def test_private_192_168_rejected(self):
        result, _ = self._check("https://router.example.com", ips=["192.168.1.1"])
        self.assertFalse(result)

    def test_any_private_in_mixed_results_rejects_all(self):
        # One safe + one private resolution must reject (DNS rebinding guard).
        result, _ = self._check("https://evil.example.com",
                                ips=["93.184.216.34", "10.0.0.1"])
        self.assertFalse(result)

    def test_ipv6_loopback_rejected(self):
        result, _ = self._check("https://v6.example.com", ips=["::1"])
        self.assertFalse(result)

    def test_non_http_scheme_rejected_without_dns(self):
        result, gai = self._check("ftp://example.com/file")
        self.assertFalse(result)
        gai.assert_not_called()

    def test_file_scheme_rejected(self):
        result, _ = self._check("file:///etc/passwd")
        self.assertFalse(result)

    def test_missing_host_rejected(self):
        result, _ = self._check("http://")
        self.assertFalse(result)

    def test_malformed_url_rejected(self):
        result, _ = self._check("not a url at all")
        self.assertFalse(result)

    def test_dns_failure_rejected(self):
        result, _ = self._check("https://nxdomain.example.com",
                                side_effect=socket.gaierror("NXDOMAIN"))
        self.assertFalse(result)

    def test_unparseable_addr_rejects(self):
        # getaddrinfo returns a string that ip_address() cannot parse → reject.
        bad = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("garbage", 0))]
        with mock.patch.object(news_common.socket, "getaddrinfo", return_value=bad):
            self.assertFalse(news_common.is_safe_url("https://weird.example.com"))


class TestRedactSecrets(unittest.TestCase):
    def test_masks_api_key_value(self):
        out = news_common.redact_secrets("https://api/x?key=SECRET123&q=morimens")
        self.assertIn("key=***", out)
        self.assertNotIn("SECRET123", out)
        self.assertIn("q=morimens", out)

    def test_masks_token_and_cookie(self):
        self.assertIn("token=***", news_common.redact_secrets("a?token=abc"))
        self.assertIn("cookie=***", news_common.redact_secrets("a?cookie=xyz"))

    def test_non_string_coerced(self):
        self.assertEqual(news_common.redact_secrets(12345), "12345")

    def test_plain_text_unchanged(self):
        self.assertEqual(news_common.redact_secrets("no secrets here"), "no secrets here")


class TestParseRelativeTime(unittest.TestCase):
    def _hours_delta(self, iso):
        dt = datetime.fromisoformat(iso)
        return (datetime.now(UTC) - dt).total_seconds() / 3600

    def test_empty_is_approximate(self):
        iso, approx = news_common.parse_relative_time("")
        self.assertTrue(approx)
        iso2, approx2 = news_common.parse_relative_time(None)
        self.assertTrue(approx2)

    def test_epoch_seconds(self):
        iso, approx = news_common.parse_relative_time(1_700_000_000)
        self.assertFalse(approx)
        self.assertTrue(iso.startswith("2023-"))

    def test_epoch_milliseconds_scaled(self):
        iso, approx = news_common.parse_relative_time(1_700_000_000_000)
        self.assertFalse(approx)
        self.assertTrue(iso.startswith("2023-"))

    def test_numeric_string_epoch(self):
        iso, approx = news_common.parse_relative_time("1700000000")
        self.assertFalse(approx)

    def test_overflow_epoch_falls_back(self):
        iso, approx = news_common.parse_relative_time(10**20)
        self.assertTrue(approx)

    def test_iso_string_roundtrip(self):
        iso, approx = news_common.parse_relative_time("2026-06-09T12:00:00Z")
        self.assertFalse(approx)
        self.assertEqual(datetime.fromisoformat(iso).year, 2026)

    def test_chinese_just_now(self):
        iso, approx = news_common.parse_relative_time("刚刚")
        self.assertFalse(approx)
        self.assertLess(self._hours_delta(iso), 1)

    def test_chinese_relative_units(self):
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("3小时前")[0]), 3, delta=0.1)
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("2天前")[0]), 48, delta=0.1)
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("30分钟前")[0]), 0.5, delta=0.1)

    def test_korean_relative(self):
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("5시간 전")[0]), 5, delta=0.1)
        self.assertFalse(news_common.parse_relative_time("10분 전")[1])
        self.assertFalse(news_common.parse_relative_time("2일 전")[1])

    def test_japanese_relative(self):
        self.assertFalse(news_common.parse_relative_time("3分前")[1])
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("4時間前")[0]), 4, delta=0.1)
        self.assertFalse(news_common.parse_relative_time("1日前")[1])

    def test_english_relative_and_streamed(self):
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("2 hours ago")[0]), 2, delta=0.1)
        self.assertFalse(news_common.parse_relative_time("Streamed 3 days ago")[1])
        self.assertFalse(news_common.parse_relative_time("5 minutes ago")[1])

    def test_chinese_yesterday_day_before(self):
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("昨天 12:00")[0]), 24, delta=0.5)
        self.assertAlmostEqual(
            self._hours_delta(news_common.parse_relative_time("前天")[0]), 48, delta=0.5)

    def test_dotted_full_date(self):
        iso, approx = news_common.parse_relative_time("2025.03.15")
        self.assertFalse(approx)
        self.assertEqual(datetime.fromisoformat(iso).month, 3)

    def test_dashed_full_date(self):
        iso, approx = news_common.parse_relative_time("2024-12-25")
        self.assertFalse(approx)
        self.assertEqual(datetime.fromisoformat(iso).day, 25)

    def test_garbage_is_approximate(self):
        iso, approx = news_common.parse_relative_time("complete nonsense ###")
        self.assertTrue(approx)

    def test_dotted_mm_dd_rolls_back_year_if_future(self):
        # A MM.DD in the future relative to now must roll back one year.
        future = datetime.now(UTC) + timedelta(days=40)
        iso, approx = news_common.parse_relative_time(f"{future.month:02d}.{future.day:02d}")
        self.assertFalse(approx)
        self.assertLessEqual(datetime.fromisoformat(iso), datetime.now(UTC))

    def test_invalid_dotted_full_date_falls_through(self):
        # Month 13 fails datetime() → ValueError swallowed → approximate fallback.
        iso, approx = news_common.parse_relative_time("2025.13.40")
        self.assertTrue(approx)

    def test_slashed_full_date(self):
        iso, approx = news_common.parse_relative_time("2024/07/04")
        self.assertFalse(approx)
        self.assertEqual(datetime.fromisoformat(iso).month, 7)

    def test_invalid_slashed_full_date_falls_through(self):
        iso, approx = news_common.parse_relative_time("2024/02/30")
        self.assertTrue(approx)

    def test_mm_dd_slashed_current_year(self):
        # Use a clearly past date so no year-rollback ambiguity.
        iso, approx = news_common.parse_relative_time("01/02")
        self.assertFalse(approx)

    def test_invalid_mm_dd_falls_through(self):
        iso, approx = news_common.parse_relative_time("13/40")
        self.assertTrue(approx)

    def test_time_only_hh_mm(self):
        iso, approx = news_common.parse_relative_time("08:30")
        self.assertFalse(approx)
        # Resolves to today (or yesterday if 08:30 is still in the future).
        self.assertLessEqual(datetime.fromisoformat(iso), datetime.now(UTC))

    def test_invalid_hh_mm_falls_through(self):
        iso, approx = news_common.parse_relative_time("99:99")
        self.assertTrue(approx)


class TestGetWithRetry(unittest.TestCase):
    def _resp(self, status=200):
        r = mock.MagicMock()
        r.status_code = status
        r.raise_for_status = mock.MagicMock()
        return r

    def test_success_first_try(self):
        sess = mock.MagicMock()
        sess.get.return_value = self._resp(200)
        with mock.patch.object(news_common.requests, "Session", return_value=sess):
            out = news_common.get_with_retry("https://x.com", retries=3)
        self.assertIs(out.status_code, 200)
        sess.get.assert_called_once()

    def test_retries_then_succeeds(self):
        good = self._resp(200)
        sess = mock.MagicMock()
        sess.get.side_effect = [requests.ConnectionError("boom"), good]
        with mock.patch.object(news_common.requests, "Session", return_value=sess), \
                mock.patch.object(news_common.time, "sleep"):
            out = news_common.get_with_retry("https://x.com", retries=3)
        self.assertIs(out, good)
        self.assertEqual(sess.get.call_count, 2)

    def test_exhausts_retries_and_raises(self):
        # retries=2 → range(2) → exactly 2 attempts, then re-raise.
        sess = mock.MagicMock()
        sess.get.side_effect = requests.ConnectionError("down")
        with mock.patch.object(news_common.requests, "Session", return_value=sess), \
                mock.patch.object(news_common.time, "sleep"):
            with self.assertRaises(requests.RequestException):
                news_common.get_with_retry("https://x.com", retries=2)
        self.assertEqual(sess.get.call_count, 2)


class TestBilibiliHelpers(unittest.TestCase):
    def setUp(self):
        # Reset wbi cache between tests.
        news_common._wbi_cache.clear()

    def test_spi_cookies_extracts_buvids(self):
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"b_3": "B3", "b_4": "B4"}}
        with mock.patch.object(news_common.requests, "get", return_value=resp):
            out = news_common.bilibili_spi_cookies()
        self.assertEqual(out, {"buvid3": "B3", "buvid4": "B4"})

    def test_spi_cookies_failure_returns_empty(self):
        with mock.patch.object(news_common.requests, "get",
                               side_effect=requests.ConnectionError("x")):
            self.assertEqual(news_common.bilibili_spi_cookies(), {})

    def test_wbi_mixin_key_computed_and_cached(self):
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        }}}
        with mock.patch.object(news_common.requests, "get", return_value=resp) as g:
            key = news_common.get_wbi_mixin_key()
            self.assertEqual(len(key), 32)
            # Second call served from cache (no second request).
            key2 = news_common.get_wbi_mixin_key()
        self.assertEqual(key, key2)
        g.assert_called_once()

    def test_wbi_mixin_key_failure_returns_cached_or_none(self):
        with mock.patch.object(news_common.requests, "get",
                               side_effect=requests.ConnectionError("x")):
            self.assertIsNone(news_common.get_wbi_mixin_key())

    def test_sign_wbi_params_adds_signature(self):
        signed = news_common.sign_wbi_params({"q": "morimens"}, "mixinkey")
        self.assertIn("wts", signed)
        self.assertIn("w_rid", signed)
        self.assertEqual(len(signed["w_rid"]), 32)  # md5 hex
        self.assertEqual(signed["q"], "morimens")


class TestPinnedIPAdapter(unittest.TestCase):
    def test_send_pins_connection_then_restores(self):
        from urllib3.util import connection as conn
        orig = conn.create_connection
        adapter = news_common._PinnedIPAdapter("93.184.216.34")
        captured = {}

        def fake_super_send(request, **kwargs):
            # During send, create_connection must be the patched version that
            # rewrites the target host to the pinned IP.
            conn.create_connection(("example.com", 443))
            return "RESP"

        with mock.patch.object(news_common.requests.adapters.HTTPAdapter, "send",
                               side_effect=fake_super_send), \
                mock.patch.object(conn, "create_connection",
                                  side_effect=lambda addr, *a, **k: captured.setdefault("addr", addr)):
            out = adapter.send(mock.MagicMock())
        self.assertEqual(out, "RESP")
        # Pinned IP substituted for the original host.
        self.assertEqual(captured["addr"][0], "93.184.216.34")
        # Original create_connection restored after send (finally block).
        self.assertIs(conn.create_connection, orig)


class TestSafeGet(unittest.TestCase):
    def test_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError):
            news_common.safe_get("ftp://example.com/x")

    def test_rejects_unsafe_host(self):
        with mock.patch.object(news_common, "_resolve_safe_ip", return_value=None):
            with self.assertRaises(ValueError):
                news_common.safe_get("https://internal.example.com")

    def test_returns_non_redirect_response(self):
        resp = mock.MagicMock()
        resp.status_code = 200
        sess = mock.MagicMock()
        sess.get.return_value = resp
        with mock.patch.object(news_common, "_resolve_safe_ip", return_value="93.184.216.34"), \
                mock.patch.object(news_common.requests, "Session", return_value=sess):
            out = news_common.safe_get("https://example.com/page")
        self.assertIs(out, resp)

    def test_follows_redirect_to_safe_target(self):
        redirect = mock.MagicMock()
        redirect.status_code = 302
        redirect.headers = {"Location": "https://example.com/final"}
        final = mock.MagicMock()
        final.status_code = 200
        sess = mock.MagicMock()
        sess.get.side_effect = [redirect, final]
        with mock.patch.object(news_common, "_resolve_safe_ip", return_value="93.184.216.34"), \
                mock.patch.object(news_common.requests, "Session", return_value=sess):
            out = news_common.safe_get("https://example.com/start")
        self.assertIs(out, final)

    def test_redirect_without_location_raises(self):
        redirect = mock.MagicMock()
        redirect.status_code = 301
        redirect.headers = {}
        sess = mock.MagicMock()
        sess.get.return_value = redirect
        with mock.patch.object(news_common, "_resolve_safe_ip", return_value="93.184.216.34"), \
                mock.patch.object(news_common.requests, "Session", return_value=sess):
            with self.assertRaises(ValueError):
                news_common.safe_get("https://example.com/start")

    def test_too_many_redirects_raises(self):
        redirect = mock.MagicMock()
        redirect.status_code = 302
        redirect.headers = {"Location": "https://example.com/next"}
        sess = mock.MagicMock()
        sess.get.return_value = redirect
        with mock.patch.object(news_common, "_resolve_safe_ip", return_value="93.184.216.34"), \
                mock.patch.object(news_common.requests, "Session", return_value=sess):
            with self.assertRaises(ValueError):
                news_common.safe_get("https://example.com/loop", max_redirects=2)


if __name__ == "__main__":
    unittest.main()


# ── env_int / env_float 容错读取（扫描修复 2026-07-28）────────────────────────
# 采集层原有 12 处 `int(os.environ.get('X', N))` 全在**模块顶层**。GitHub Actions 里
# `env: X: ${{ inputs.x }}` 在 input 未填时注入的是**空字符串**（不是「未设置」），
# `int('')` 抛 ValueError —— 发生在 import 期，整个采集模块加载失败、一轮数据全灭,
# 报错还是一句与业务无关的 invalid literal。实测复现：
#   HOURS_LOOKBACK="" python3 -c "import aggregator_base"  → ValueError
class TestEnvIntFloat(unittest.TestCase):
    def test_unset_returns_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(news_common.env_int('X_UNSET', 7), 7)
            self.assertEqual(news_common.env_float('X_UNSET', 0.5), 0.5)

    def test_empty_string_returns_default(self):
        """回归核心：空串是 Actions 未填 input 的实际注入值，绝不能抛。"""
        with mock.patch.dict(os.environ, {'X_E': '', 'X_F': '   '}, clear=True):
            self.assertEqual(news_common.env_int('X_E', 24), 24)
            self.assertEqual(news_common.env_int('X_F', 24), 24)
            self.assertEqual(news_common.env_float('X_E', 0.2), 0.2)

    def test_garbage_returns_default_without_raising(self):
        with mock.patch.dict(os.environ, {'X_G': 'abc'}, clear=True):
            self.assertEqual(news_common.env_int('X_G', 500), 500)
            self.assertEqual(news_common.env_float('X_G', 1.5), 1.5)

    def test_valid_value_wins(self):
        with mock.patch.dict(os.environ, {'X_V': ' 72 ', 'X_FV': '0.35'}, clear=True):
            self.assertEqual(news_common.env_int('X_V', 24), 72)
            self.assertAlmostEqual(news_common.env_float('X_FV', 0.2), 0.35)

    def test_zero_is_honoured_not_treated_as_missing(self):
        """0 是有效值（HOURS_LOOKBACK=0 用于触发自适应回溯），不得当缺失回落。"""
        with mock.patch.dict(os.environ, {'X_Z': '0'}, clear=True):
            self.assertEqual(news_common.env_int('X_Z', 24), 0)


# ── 远端 XML 护栏（扫描修复 2026-07-28）───────────────────────────────────────
# 两处 Reddit RSS 解析原先直接 ET.fromstring(远端文本)。ElementTree 不做外部实体
# 解析，但对 DTD 内部实体的递归展开（billion laughs）与超大输入并无防护——一个被
# 篡改或被劫持的 feed 就能把采集进程内存吃光，而这条链路每 3 小时无人值守跑一轮。
# 不引入 defusedxml（新依赖），改用对 RSS/Atom 零误伤的两道护栏。
class TestParseXmlSafely(unittest.TestCase):
    def test_normal_atom_parses(self):
        root = news_common.parse_xml_safely(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>')
        self.assertTrue(root.tag.endswith('feed'))
        self.assertEqual(len(list(root)), 1)

    def test_doctype_rejected(self):
        """billion laughs 必须靠 DTD；合法 RSS/Atom 从不需要 DTD，故整类拒掉。"""
        payload = '<!DOCTYPE lolz [<!ENTITY a "xxxxxxxxxx">]><f>&a;</f>'
        with self.assertRaises(ValueError):
            news_common.parse_xml_safely(payload)

    def test_bare_entity_declaration_rejected(self):
        with self.assertRaises(ValueError):
            news_common.parse_xml_safely('<!ENTITY x "y"><f/>')

    def test_oversize_rejected(self):
        big = '<f>' + ('x' * (news_common.MAX_XML_BYTES + 10)) + '</f>'
        with self.assertRaises(ValueError):
            news_common.parse_xml_safely(big)

    def test_size_cap_is_configurable(self):
        with self.assertRaises(ValueError):
            news_common.parse_xml_safely('<f>hello</f>', max_bytes=4)

    def test_malformed_xml_still_raises_not_silently_empty(self):
        """畸形 feed 必须抛（两处调用方都有 except 兜底），不得静默返回空树。"""
        # 钉死具体类型：assertRaises(Exception) 连 AttributeError / TypeError 都算通过,
        # 于是「护栏把自己写崩了」也能骗过这条断言。
        from xml.etree.ElementTree import ParseError
        with self.assertRaises(ParseError):
            news_common.parse_xml_safely('<f><unclosed></f>')
