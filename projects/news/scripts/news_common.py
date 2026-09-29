#!/usr/bin/env python3
"""
news_common.py — 采集层共享工具（ARCH-01/02 收敛单一归属）

两套采集栈（aggregator_collectors / global_collectors）历史上各自实现了 HTTP 重试、
HTML 去标签、item 构造等同名工具。本模块抽出其中**行为等价**的部分作为单一真源，
两栈委托至此，去除重复实现（审计 ARCH-02）。

收敛纪律（§6.1/§6.3）：仅收敛真正等价的 helper。HTTP 包装与 item 构造在两栈间
语义不同（global `_get` 对 4xx `raise_for_status`、aggregator `_get_with_retry`
对 4xx 返回响应；两栈 item 字段集不同），故**不强行合并**——保留各自实现并以
`# NOTE: divergent` 标注，见 global_collectors / aggregator_base。
"""

import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from datetime import datetime, timedelta, UTC
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from urllib3.util import connection as _urllib3_connection

# Redirect 跳数上限：safe_get 每跳重校验 is_safe_url，get_with_retry 防御纵深用同一上限。
MAX_REDIRECTS = 5


def env_int(name: str, default: int) -> int:
    """读环境变量为 int，取不到 / 取到废值一律回落 default（绝不抛）。

    为什么需要它：采集层原有 12 处 `int(os.environ.get('X', N))` 全在**模块顶层**。
    GitHub Actions 里 `env: X: ${{ inputs.x }}` 在 input 未填时注入的是**空字符串**
    （不是「未设置」），`int('')` 抛 ValueError —— 而它发生在 import 期，于是
    整个采集模块加载失败、一轮数据全灭，报错还是一句与业务无关的 invalid literal。
    实测：`HOURS_LOOKBACK="" python3 -c "import aggregator_base"` 直接 ValueError。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        print(f'[news_common] 环境变量 {name}={raw!r} 非整数，回落默认值 {default}')
        return default


def env_float(name: str, default: float) -> float:
    """env_int 的浮点版；同样绝不因废值抛异常。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        print(f'[news_common] 环境变量 {name}={raw!r} 非数值，回落默认值 {default}')
        return default


# 远端 XML 解析上限：Reddit Atom 单页实测 < 200 KB，5 MB 已是极宽的余量。
MAX_XML_BYTES = 5 * 1024 * 1024
_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def parse_xml_safely(xml_text, *, max_bytes: int = MAX_XML_BYTES):
    """解析**远端不可信** XML（Reddit RSS 等），带两道与依赖无关的护栏。

    背景：两处 RSS 解析直接 `xml.etree.ElementTree.fromstring(远端文本)`。
    ElementTree 不做外部实体解析，但对 DTD 内部实体的递归展开（billion laughs）
    与超大输入并无防护——一个被篡改或被劫持的 feed 就能把采集进程的内存吃光,
    而这条链路每 3 小时自动跑一轮、无人值守。

    不引入 defusedxml（新依赖），改用两条对 RSS/Atom 零误伤的护栏：
      ① 体积上限——正常 feed 与之相差两个数量级；
      ② 拒绝任何带 DOCTYPE / ENTITY 声明的文档——合法的 RSS/Atom **不需要** DTD,
         而 billion laughs 类攻击必须靠它。

    返回 ElementTree.Element；不合格输入抛 ValueError（调用方两处均已有 except 兜底）。
    """
    raw = xml_text.encode("utf-8", "replace") if isinstance(xml_text, str) else bytes(xml_text)
    if len(raw) > max_bytes:
        raise ValueError(f"XML 超出体积上限（{len(raw)} > {max_bytes} 字节），拒绝解析")
    if _DOCTYPE_RE.search(raw):
        raise ValueError("XML 含 DOCTYPE/ENTITY 声明，拒绝解析（RSS/Atom 无需 DTD）")
    return ET.fromstring(raw)  # noqa: S314  两道护栏已在上方拦下 DTD 与超大输入


def _resolve_safe_ip(host):
    """解析 host（IPv4+IPv6，经 socket.getaddrinfo），返回首个公网可达 IP 字符串；
    任一解析地址落私有/环回/链路本地/保留/多播/未指定段则视为不安全，返回 None。

    与旧实现（download_media 的 socket.gethostbyname）不同：getaddrinfo 同时覆盖 v4/v6，
    且对所有解析结果逐一校验——任一不安全即拒，杜绝 AAAA 内网混入。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    safe_ip = None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return None
        if safe_ip is None:
            safe_ip = addr
    return safe_ip


def is_safe_url(url: str) -> bool:
    """SSRF 守卫：仅放行 http/https 且解析后全部落公网 IP（v4/v6 均覆盖）。

    拒绝：非 http(s) scheme、无 host、解析失败、任一解析地址落
    私有/环回/链路本地/保留/多播/未指定段。SEC-02 守卫从 download_media 提至此共享层。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    return _resolve_safe_ip(host) is not None


class _PinnedIPAdapter(requests.adapters.HTTPAdapter):
    """把连接目标 IP pin 到「校验时已确认安全」的那个 IP，消除 resolve-then-fetch
    的 TOCTOU / DNS 重绑定窗口（R2-M1）：守卫解析一次得公网 IP，连接复用同一 IP，
    不给 requests 二次解析的机会。Host 头与 TLS SNI 仍用原域名（保证 vhost / 证书校验）。
    """

    def __init__(self, pinned_ip, *args, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        orig_create_connection = _urllib3_connection.create_connection
        pinned_ip = self._pinned_ip

        def patched_create_connection(address, *a, **kw):
            return orig_create_connection((pinned_ip, address[1]), *a, **kw)

        _urllib3_connection.create_connection = patched_create_connection
        try:
            return super().send(request, **kwargs)
        finally:
            _urllib3_connection.create_connection = orig_create_connection


def safe_get(url, *, headers=None, timeout=30, stream=False,
             max_redirects=MAX_REDIRECTS):
    """SSRF-safe GET：禁用自动重定向，手动逐跳重校验 is_safe_url，并把每跳连接
    pin 到校验通过的 IP（消除 TOCTOU/DNS 重绑定，R2-M1）。

    返回最终非 3xx 响应（requests.Response，stream 时 body 未消费）。
    任一跳 URL 不安全、3xx 缺/坏 Location、或超过 max_redirects → 抛 ValueError。
    传输层错误照常抛 requests.RequestException，由调用方处理。
    """
    current = url
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"unsafe url scheme/host: {current[:80]}")
        pinned_ip = _resolve_safe_ip(parsed.hostname)
        if pinned_ip is None:
            raise ValueError(f"unsafe url (non-public host): {current[:80]}")

        session = requests.Session()
        session.mount("http://", _PinnedIPAdapter(pinned_ip))
        session.mount("https://", _PinnedIPAdapter(pinned_ip))
        resp = session.get(current, headers=headers, timeout=timeout,
                           stream=stream, allow_redirects=False)

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError(f"redirect without Location: {current[:80]}")
            current = requests.compat.urljoin(current, location)
            continue
        return resp
    raise ValueError(f"too many redirects (>{max_redirects}): {url[:80]}")


_SENSITIVE_PARAM_RE = re.compile(
    r"((?:api_?key|key|access_?token|token|cookie|auth|secret|password)=)[^&\s'\"]+",
    re.IGNORECASE,
)


def redact_secrets(text) -> str:
    """日志/落盘脱敏（H3）：掩码 URL 查询参数中的敏感值（key=/token=/cookie= 等）。

    requests 异常文本含完整请求 URL（如 YouTube `key=<API key>`），采集器把异常
    直接写日志（公开 Actions 日志）或 source-health.json（入库），此处统一掩码。
    """
    return _SENSITIVE_PARAM_RE.sub(r"\1***", str(text))


def parse_relative_time(value):
    """把各平台时间字段归一为 ISO 字符串（H4 单一真源）。返回 (iso_string, is_approximate)。

    支持：
    - epoch 秒/毫秒（int/float/纯数字字符串；>1e11 视为毫秒）
    - ISO 字符串（含尾缀 Z）
    - 中文相对："刚刚"、"x分钟前"、"x小时前"、"x天前"、"昨天"、"前天"
    - 韩文相对："x분 전"、"x시간 전"、"x일 전"
    - 日文相对："x分前"、"x時間前"、"x日前"
    - 英文相对："x minutes/hours/days/... ago"、"Streamed x ago"
    - 绝对日期："YYYY-MM-DD"/"YYYY/MM/DD"/"YYYY.MM.DD"、"MM-DD"/"MM/DD"/"MM.DD"、"HH:MM"

    is_approximate=True 仅当输入为空或完全无法解析（回退为当前时间）。
    """
    now = datetime.now(UTC)
    if not value or not str(value).strip():
        return now.isoformat(), True

    # epoch 数字（naver writeDateTimestamp 为毫秒、zhihu created_time 为秒）
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            ts = float(value)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=UTC).isoformat(), False
        except (ValueError, OverflowError, OSError):
            return now.isoformat(), True

    s = str(value).strip()

    # Try ISO format first
    try:
        dt = datetime.fromisoformat(s)
        return dt.isoformat(), False
    except (ValueError, TypeError):
        pass

    # Chinese relative: "刚刚"
    if s == "刚刚":
        return now.isoformat(), False

    # Chinese: "x分钟前", "x小时前", "x天前"
    m = re.match(r"(\d+)\s*分钟前", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).isoformat(), False
    m = re.match(r"(\d+)\s*小时前", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).isoformat(), False
    m = re.match(r"(\d+)\s*天前", s)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat(), False

    # Korean: "x분 전", "x시간 전", "x일 전"
    m = re.match(r"(\d+)\s*분\s*전", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).isoformat(), False
    m = re.match(r"(\d+)\s*시간\s*전", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).isoformat(), False
    m = re.match(r"(\d+)\s*일\s*전", s)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat(), False

    # Japanese: "x分前", "x時間前", "x日前"
    m = re.match(r"(\d+)\s*分前", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).isoformat(), False
    m = re.match(r"(\d+)\s*時間前", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).isoformat(), False
    m = re.match(r"(\d+)\s*日前", s)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat(), False

    # English: "x minutes/hours/days/weeks ago", "Streamed x ago"
    m = re.match(r"(?:streamed\s+)?(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", s, re.IGNORECASE)
    if m:
        num = int(m.group(1))
        unit = m.group(2).lower()
        delta_map = {
            "second": timedelta(seconds=num), "minute": timedelta(minutes=num),
            "hour": timedelta(hours=num), "day": timedelta(days=num),
            "week": timedelta(weeks=num), "month": timedelta(days=num * 30),
            "year": timedelta(days=num * 365),
        }
        return (now - delta_map.get(unit, timedelta())).isoformat(), False

    # "昨天" / "前天"
    if "昨天" in s:
        return (now - timedelta(days=1)).isoformat(), False
    if "前天" in s:
        return (now - timedelta(days=2)).isoformat(), False

    # Arca.live style: "YYYY.MM.DD" or "MM.DD"
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", s)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
            return dt.isoformat(), False
        except ValueError:
            pass
    m = re.match(r"(\d{1,2})\.(\d{1,2})\s*$", s)
    if m:
        try:
            dt = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                             hour=0, minute=0, second=0, microsecond=0)
            if dt > now:
                dt = dt.replace(year=dt.year - 1)
            return dt.isoformat(), False
        except ValueError:
            pass

    # "YYYY-MM-DD" or "YYYY/MM/DD"
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
            return dt.isoformat(), False
        except ValueError:
            pass

    # "MM-DD" or "MM/DD" (current year)
    m = re.match(r"(\d{1,2})[-/](\d{1,2})\s*$", s)
    if m:
        try:
            dt = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                             hour=0, minute=0, second=0, microsecond=0)
            if dt > now:
                dt = dt.replace(year=dt.year - 1)
            return dt.isoformat(), False
        except ValueError:
            pass

    # Time-only: "HH:MM" (today)
    m = re.match(r"(\d{1,2}):(\d{2})\s*$", s)
    if m:
        try:
            dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                             second=0, microsecond=0)
            if dt > now:
                dt -= timedelta(days=1)
            return dt.isoformat(), False
        except ValueError:
            pass

    return now.isoformat(), True


def strip_html(text: str) -> str:
    """移除 HTML 标签，返回纯文本。空值安全。

    两栈先前各有 `_strip_html`(global) / `strip_html_tags`(aggregator_base)，
    实现等价（`re.sub(r'<[^>]+>', '', text)` + 空值守卫），此处统一。
    """
    return re.sub(r"<[^>]+>", "", text) if text else ""


def get_with_retry(url, params=None, headers=None, timeout=15, retries=3,
                   default_headers=None):
    """带重试的 GET（间隔 1s/2s）；对任意非 2xx `raise_for_status`。

    对应 global_collectors._get 的语义（4xx/5xx 均抛错重试），供需要该语义的采集器复用。
    aggregator_base._get_with_retry 故意采用不同语义（4xx 直接返回响应），不在此收敛。
    """
    h = {**(default_headers or {}), **(headers or {})}
    last_exc = None
    for attempt in range(retries):
        try:
            # R2-L2 防御纵深：限制重定向跳数（默认 requests 上限 30 → 收紧到
            # MAX_REDIRECTS），避免被外部端点经长重定向链拖向意外 sink。
            session = requests.Session()
            session.max_redirects = MAX_REDIRECTS
            resp = session.get(url, params=params, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries - 1:
                raise
            time.sleep(attempt + 1)
    raise last_exc  # unreachable, satisfies type checker


# ── Bilibili 风控配套（spi cookie + wbi 签名）────────────────────────────────
# 2026-06 起 B 站搜索接口对伪造 buvid 返回风控 HTML（非 JSON），必须：
# 1) 用 /x/frontend/finger/spi 取服务端签发的 buvid3/buvid4
# 2) 走 /x/web-interface/wbi/search/type 并做 wbi 签名
# 两套采集栈（aggregator / global）共用此实现（ARCH-02 收敛纪律）。

_WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
_wbi_cache = {}  # {'mixin_key': str, 'ts': float}


def bilibili_spi_cookies(headers=None, timeout=10):
    """从 /x/frontend/finger/spi 获取服务端签发的 buvid3/buvid4。失败返回 {}。"""
    try:
        resp = requests.get(
            'https://api.bilibili.com/x/frontend/finger/spi',
            headers=headers, timeout=timeout,
        )
        data = resp.json().get('data', {})
        cookies = {}
        if data.get('b_3'):
            cookies['buvid3'] = data['b_3']
        if data.get('b_4'):
            cookies['buvid4'] = data['b_4']
        return cookies
    except Exception:
        return {}


def get_wbi_mixin_key(headers=None, timeout=10):
    """获取并缓存 wbi mixin key（30 分钟刷新）。失败时回退用旧缓存或 None。"""
    now = time.time()
    if _wbi_cache.get('mixin_key') and now - _wbi_cache.get('ts', 0) < 1800:
        return _wbi_cache['mixin_key']
    try:
        resp = requests.get(
            'https://api.bilibili.com/x/web-interface/nav',
            headers=headers, timeout=timeout,
        )
        wbi = resp.json().get('data', {}).get('wbi_img', {})
        img_key = wbi['img_url'].rsplit('/', 1)[1].split('.')[0]
        sub_key = wbi['sub_url'].rsplit('/', 1)[1].split('.')[0]
        raw = img_key + sub_key
        _wbi_cache['mixin_key'] = ''.join(raw[i] for i in _WBI_MIXIN_KEY_ENC_TAB)[:32]
        _wbi_cache['ts'] = now
        return _wbi_cache['mixin_key']
    except Exception:
        return _wbi_cache.get('mixin_key')


def sign_wbi_params(params, mixin_key):
    """为请求参数附加 wts + w_rid wbi 签名。"""
    import hashlib
    from urllib.parse import urlencode
    params = dict(params)
    params['wts'] = int(time.time())
    query = urlencode(sorted(params.items()))
    # md5 是 Bilibili WBI 协议**规定**的摘要算法，不可替换；usedforsecurity=False
    # 表明银芯并非拿它当安全原语，同时保证 FIPS 模式下不抛 ValueError。
    params['w_rid'] = hashlib.md5((query + mixin_key).encode(), usedforsecurity=False).hexdigest()
    return params


def make_item(title, summary, source, platform_region, time_str, url,
              engagement=0, is_hot=False, author="", tags=None, lang="",
              content_type="text", media_url="", time_is_approximate=False,
              region=None, archive_subtype=None):
    """创建标准化信息条目（与 global_collectors._make_item 等价的单一真源）。

    aggregator 栈通过 aggregator_base.validate_news_item 走另一套 item 形状与校验路径，
    字段集不同，故不在此收敛。

    region / archive_subtype 为甲方案归档分层字段（2026-06-21 采集源命名规范）：采集器
    显式标注时才写入，缺省不落字段 → archive_platforms 回落旧扁平布局，不带字段的源零破坏。
    region = 区服（global/jp/cn/volunteer…）；archive_subtype = 内容子类（review/news/
    discussion/post/strategy/video/comments）。归档落点 <平台>[/<区服>][/<类型>]/日期.json。
    """
    item = {
        "title": strip_html(title or "").strip(),
        "summary": strip_html(summary or "").strip(),
        "source": source,
        "platform_region": platform_region,
        "lang": lang,
        "time": time_str,
        "url": url or "",
        "engagement": engagement,
        "is_hot": is_hot,
        "author": str(author or ""),
        "tags": tags or [],
        "content_type": content_type,
        "media_url": media_url,
    }
    if time_is_approximate:
        item["time_is_approximate"] = True
    if region:
        item["region"] = region
    if archive_subtype:
        item["archive_subtype"] = archive_subtype
    return item


def dump_json_atomic(path, obj, *, indent=2):
    """原子写 JSON —— 先写同目录临时文件，再 os.replace 落位。

    news.json / news-raw.json 等输出由多个脚本先后写入（aggregator → collect_global），
    直接 open('w')+dump 若在写一半时崩溃/被中断，会留下半截损坏文件，前端读到即白屏。
    同目录临时文件 + os.replace 在同一文件系统上是原子替换：要么旧文件、要么完整新文件，
    永不出现半截态。单一真源，所有 output/data 层 JSON 写入应走此函数。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 采集条目校验链路（2026-08-22 从退役的 aggregator_base 迁入）
# ---------------------------------------------------------------------------
# 守密人 2026-08-22 裁定「采集 → 直接入湖」：新闻流编排退役，但校验链路是数据质量
# 保障而非展示件——丢弃计数是一等健康指标（2026-07-02 P0-3；taptap_review 曾单轮被
# 丢 108 条、连续 12 天无人察觉），经 write_validation_drops 落盘、由
# silent_sources_audit --strict 参与告警门控。整段迁入采集层共享真源，
# 顺带把原「AC 才校验、GC 不校验」的覆盖缺口补平：现在唯一入口 collect_global
# 对全部采集条目一律校验（原 aggregator_base 注释里挂账的「未来 GC 增设校验点须接入」）。
import logging as _logging

_log = _logging.getLogger('news_common.validate')

# Valid source identifiers — 从 sources.py 单一真相源派生（含 SOURCE_ALIASES 的
# 归一化前原始名，如 steam_review）。
from sources import KNOWN_SOURCES as _KNOWN_SOURCES, SOURCE_ALIASES as _SOURCE_ALIASES  # noqa: E402

VALID_SOURCES = set(_KNOWN_SOURCES) | set(_SOURCE_ALIASES)

# Required fields for each news item
REQUIRED_FIELDS = {'title', 'source', 'time', 'engagement'}

def sanitize_url(url):
    """Validate and normalize URL scheme."""
    if not url:
        return ''
    url = url.strip()
    # Normalize http to https for known platforms
    if url.startswith(('http://www.bilibili.com', 'http://bilibili.com')):
        url = url.replace('http://', 'https://', 1)
    # Basic URL validation
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https', ''):
        return ''
    return url


def sanitize_summary(summary):
    """Clean up summary text, removing placeholder values."""
    if not summary:
        return ''
    summary = summary.strip()
    # Filter out placeholder/empty summaries
    if summary in ('-', '--', '无', 'N/A', 'null', 'none', '暂无'):
        return ''
    return strip_html(summary)


def validate_news_item(item):
    """
    Validate a single news item. Returns (is_valid, cleaned_item).
    Checks required fields, sanitizes text, normalizes URLs.
    """
    if not isinstance(item, dict):
        return False, None

    # Check required fields
    for field in REQUIRED_FIELDS:
        if field not in item or item[field] is None or (isinstance(item[field], str) and not item[field]):
            _log.warning(f'Validation: missing required field "{field}" in item: {item.get("title", "unknown")[:50]}')
            return False, None

    # Validate source
    if item['source'] not in VALID_SOURCES:
        _log.warning(f'Validation: unknown source "{item["source"]}" for: {item["title"][:50]}')
        return False, None

    # Validate engagement is a non-negative number
    try:
        engagement = int(item['engagement'])
        engagement = max(engagement, 0)
    except (ValueError, TypeError):
        engagement = 0

    # Validate time format (ISO 8601)
    try:
        if isinstance(item['time'], str):
            datetime.fromisoformat(item['time'])
    except (ValueError, TypeError):
        _log.warning(f'Validation: invalid time format for: {item["title"][:50]}')
        return False, None

    # Build cleaned item
    cleaned = {
        'title': strip_html(str(item['title']).strip()),
        'summary': sanitize_summary(item.get('summary', '')),
        'source': item['source'],
        'time': item['time'],
        'url': sanitize_url(item.get('url', '')),
        'engagement': engagement,
        'is_hot': bool(item.get('is_hot', False)),
        'author': strip_html(str(item.get('author', '')).strip()),
        'tags': [strip_html(str(t).strip()) for t in item.get('tags', []) if t and str(t).strip()],
    }

    # Preserve source-specific extra fields
    if 'language' in item:
        cleaned['language'] = str(item['language'])
    if 'metadata' in item and isinstance(item['metadata'], dict):
        cleaned['metadata'] = item['metadata']
    # Preserve media fields for image archival
    if item.get('media_url'):
        cleaned['media_url'] = sanitize_url(item['media_url'])
        cleaned['content_type'] = item.get('content_type', 'image')
    if item.get('lang'):
        cleaned['lang'] = str(item['lang'])
    # 甲方案归档分层字段（2026-06-21 采集源命名规范）：AC 栈 item 经此白名单重建，
    # 须显式放行 region/archive_subtype，否则 archive_platforms 分桶失据（缺省不落，回落扁平）。
    if item.get('region'):
        cleaned['region'] = str(item['region'])
    if item.get('archive_subtype'):
        cleaned['archive_subtype'] = str(item['archive_subtype'])
    # 近似时间标记（2026-09-18 守密人裁定放行）：采集器解析不出真实发布时间时填 now()
    # 并置此标记，`archive_platforms.item_key` 据它把这种时刻**排除出去重键**。白名单
    # 不放行 → 标记在此蒸发 → 每轮不同的 now()（精确到微秒）成了条目身份，同一条内容
    # 每轮都是新条目（实测 taptap 574 条实为 48 条、weibo 815 条实为 184 条），且读方
    # 再也分不清「真实发布时刻」与「采集时刻」。2026-08-22「全部条目一律校验」后此漏
    # 覆盖全平台，对账见 Public-Info-Pool/Resource/data-diagnostics/
    # community-archive-date-confusion-20260917.md D3。
    if item.get('time_is_approximate'):
        cleaned['time_is_approximate'] = True

    # Title must not be empty after sanitization
    if not cleaned['title']:
        return False, None

    return True, cleaned


def validate_all_news(items):
    """Validate and clean a list of news items. Returns list of valid items.

    被丢弃条目按源计数进 VALIDATION_DROPS（2026-07-02 P0-3「静默丢弃升格为
    一等指标」）：此前丢弃只打 WARNING 进 CI 日志，taptap_review 曾单轮被
    丢 108 条、连续 12 天无人察觉。计数经 write_validation_drops 落盘，由
    silent_sources_audit 并入 source-health 并参与 --strict 告警门控。
    """
    valid_items = []
    invalid_count = 0

    for item in items:
        is_valid, cleaned = validate_news_item(item)
        if is_valid:
            valid_items.append(cleaned)
        else:
            invalid_count += 1
            src = item.get('source', 'unknown') if isinstance(item, dict) else 'malformed'
            VALIDATION_DROPS[src] = VALIDATION_DROPS.get(src, 0) + 1

    if invalid_count > 0:
        _log.warning(f'Validation: {invalid_count} invalid items filtered out of {len(items)} total')

    _log.info(f'Validation: {len(valid_items)} valid items out of {len(items)} total')
    return valid_items


# 本次运行的校验丢弃计数（源 -> 条数）。跨源累计，run 结束由 aggregator 落盘。
VALIDATION_DROPS: dict = {}
# 跨轮状态（每轮读旧值累计）：落 data/ 进 git，绝不放运行期工作根——随目录蒸发
# 等于健康侧计数每轮从零重建。
def _validation_drops_path():
    import archive_layout
    return archive_layout.news_state_root() / 'validation-drops.json'


def write_validation_drops(path=None):
    """把本次运行的校验丢弃计数写盘（无丢弃也写零值文件，供健康侧稳定消费）。"""
    import json as _json
    path = Path(path) if path else _validation_drops_path()
    payload = {
        'generated_at': datetime.now().astimezone().isoformat(),
        'total_dropped': sum(VALIDATION_DROPS.values()),
        'by_source': dict(sorted(VALIDATION_DROPS.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload
