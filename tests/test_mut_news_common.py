"""Mutation-testing harness for news_common (see setup.cfg [mutmut]).

news_common 是采集层共享工具单一真源（ARCH-01/02）：SSRF 守卫、日志脱敏、
多语言时间归一、item 构造、原子写盘——两套采集栈全部委托至此。守卫条件被翻转
= SSRF 放行内网；时间归一被扰动 = 全平台条目时序错乱。逻辑密集；唯一大常量表
_WBI_MIXIN_KEY_ENC_TAB 以「黄金值一击全钉」处理（任一表项变动即改变 mixin key，
无需逐项断言），不构成数据噪声（守密人 2026-07-11 裁定扩员）。

Hermetic：socket / requests / time.sleep 全部打桩，零网络。
Imports via PACKAGE path so mutmut's trampoline keys match.
"""
import json
from datetime import datetime, timedelta, UTC
from typing import ClassVar
import pytest

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

import projects.news.scripts.news_common as nc  # noqa: E402


# ── strip_html / redact_secrets ─────────────────────────────────────────────

def test_strip_html():
    assert nc.strip_html('<b>hi</b> <a href="x">there</a>') == 'hi there'
    assert nc.strip_html('') == ''
    assert nc.strip_html(None) == ''
    assert nc.strip_html('plain') == 'plain'


@pytest.mark.parametrize(('raw', 'masked'), [
    ('https://x/api?key=SECRET&q=1', 'https://x/api?key=***&q=1'),
    ('https://x?api_key=abc', 'https://x?api_key=***'),
    ('https://x?access_token=t.t-t', 'https://x?access_token=***'),
    ('Cookie=sess123 done', 'Cookie=*** done'),
    ('PASSWORD=hunter2&next=1', 'PASSWORD=***&next=1'),   # 大小写不敏感
    ('https://x?query=morimens', 'https://x?query=morimens'),  # 非敏感参数不动
])
def test_redact_secrets(raw, masked):
    assert nc.redact_secrets(raw) == masked


def test_redact_secrets_coerces_non_str():
    assert nc.redact_secrets(Exception('url?token=abc')) == 'url?token=***'


# ── parse_relative_time：多语言时间归一 ─────────────────────────────────────

def _close(iso, expected_dt, tol=8):
    got = datetime.fromisoformat(iso)
    return abs((got - expected_dt).total_seconds()) <= tol


def _now():
    return datetime.now(UTC)


def test_prt_empty_is_approximate():
    iso, approx = nc.parse_relative_time('')
    assert approx is True and _close(iso, _now())
    iso2, approx2 = nc.parse_relative_time(None)
    assert approx2 is True


def test_prt_epoch_seconds_and_millis():
    iso, approx = nc.parse_relative_time(1752200000)
    assert (iso, approx) == ('2025-07-11T02:13:20+00:00', False)
    # >1e11 视为毫秒：同一时刻的毫秒形态须归一到同一 ISO
    iso_ms, _ = nc.parse_relative_time(1752200000000)
    assert iso_ms == iso
    iso_str, _ = nc.parse_relative_time('1752200000')
    assert iso_str == iso


def test_prt_iso_passthrough_with_z():
    iso, approx = nc.parse_relative_time('2026-07-11T15:00:00Z')
    assert (iso, approx) == ('2026-07-11T15:00:00+00:00', False)


@pytest.mark.parametrize(('text', 'delta'), [
    ('刚刚', timedelta()),
    ('5分钟前', timedelta(minutes=5)),
    ('3小时前', timedelta(hours=3)),
    ('2天前', timedelta(days=2)),
    ('7분 전', timedelta(minutes=7)),
    ('2시간 전', timedelta(hours=2)),
    ('4일 전', timedelta(days=4)),
    ('9分前', timedelta(minutes=9)),
    ('6時間前', timedelta(hours=6)),
    ('3日前', timedelta(days=3)),
    ('45 minutes ago', timedelta(minutes=45)),
    ('2 hours ago', timedelta(hours=2)),
    ('Streamed 3 days ago', timedelta(days=3)),
    ('1 week ago', timedelta(weeks=1)),
    ('2 months ago', timedelta(days=60)),
    ('1 year ago', timedelta(days=365)),
    ('昨天 12:00', timedelta(days=1)),
    ('前天', timedelta(days=2)),
])
def test_prt_relative_forms(text, delta):
    iso, approx = nc.parse_relative_time(text)
    assert approx is False
    assert _close(iso, _now() - delta), f'{text}: {iso}'


def test_prt_absolute_dates():
    assert nc.parse_relative_time('2026.7.9') == ('2026-07-09T00:00:00+00:00', False)
    # 'YYYY-MM-DD' 先被 ISO 分支吃掉 → naive（与 UTC 正则分支行为不同，钉住现状）
    assert nc.parse_relative_time('2026-07-09') == ('2026-07-09T00:00:00', False)
    assert nc.parse_relative_time('2026/7/9') == ('2026-07-09T00:00:00+00:00', False)


def test_prt_month_day_rolls_back_when_future():
    now = _now()
    future = now + timedelta(days=40)
    iso, approx = nc.parse_relative_time(f'{future.month}.{future.day}')
    assert approx is False
    got = datetime.fromisoformat(iso)
    assert got.year == now.year - 1 and got <= now  # 未来日期回退一年


def test_prt_hhmm_today_or_yesterday():
    now = _now()
    past = now - timedelta(minutes=30)
    iso, approx = nc.parse_relative_time(f'{past.hour:02d}:{past.minute:02d}')
    assert approx is False
    assert _close(iso, past.replace(second=0, microsecond=0), tol=61)
    fut = now + timedelta(minutes=90)
    iso2, _ = nc.parse_relative_time(f'{fut.hour:02d}:{fut.minute:02d}')
    got2 = datetime.fromisoformat(iso2)
    assert got2 <= now  # 未来时刻回退到昨天


def test_prt_garbage_is_approximate():
    iso, approx = nc.parse_relative_time('not a time at all')
    assert approx is True and _close(iso, _now())


# ── make_item：标准条目构造 ─────────────────────────────────────────────────

def test_make_item_full_mapping():
    item = nc.make_item('<b>T</b>', ' <i>S</i> ', 'steam', 'Global', 't0', 'u',
                        engagement=7, is_hot=True, author=123, tags=['a'],
                        lang='en', content_type='video', media_url='m',
                        time_is_approximate=True, region='jp', archive_subtype='review')
    assert item == {
        'title': 'T', 'summary': 'S', 'source': 'steam', 'platform_region': 'Global',
        'lang': 'en', 'time': 't0', 'url': 'u', 'engagement': 7, 'is_hot': True,
        'author': '123', 'tags': ['a'], 'content_type': 'video', 'media_url': 'm',
        'time_is_approximate': True, 'region': 'jp', 'archive_subtype': 'review',
    }


def test_make_item_optional_fields_absent_by_default():
    item = nc.make_item(None, None, 's', 'r', 't', None)
    assert item['title'] == '' and item['summary'] == '' and item['url'] == ''
    assert item['tags'] == [] and item['author'] == ''
    for k in ('time_is_approximate', 'region', 'archive_subtype'):
        assert k not in item  # 缺省不落字段 → 归档回落旧扁平布局的契约


# ── SSRF 守卫：_resolve_safe_ip / is_safe_url ───────────────────────────────

def _fake_gai(*addrs, fail=False):
    def gai(host, port):
        if fail:
            raise nc.socket.gaierror('nx')
        return [(2, 1, 6, '', (a, 0)) for a in addrs]
    return gai


@pytest.mark.parametrize(('addrs', 'ok'), [
    (('93.184.216.34',), True),
    (('10.0.0.5',), False),                      # 私网
    (('127.0.0.1',), False),                     # 环回
    (('169.254.1.1',), False),                   # 链路本地
    (('224.0.0.1',), False),                     # 多播
    (('0.0.0.0',), False),  # noqa: S104  这是 SSRF 守卫的**被测输入**，不是监听地址
    (('93.184.216.34', '10.0.0.5'), False),      # 任一不安全即拒（AAAA 混入）
    (('2606:2800:220:1::1',), True),             # 公网 v6
    (('fe80::1',), False),                       # v6 链路本地
    (('::1',), False),                           # v6 环回
])
def test_resolve_safe_ip_matrix(monkeypatch, addrs, ok):
    monkeypatch.setattr(nc.socket, 'getaddrinfo', _fake_gai(*addrs))
    got = nc._resolve_safe_ip('h')
    assert (got == addrs[0]) if ok else (got is None)


def test_resolve_safe_ip_failures(monkeypatch):
    monkeypatch.setattr(nc.socket, 'getaddrinfo', _fake_gai(fail=True))
    assert nc._resolve_safe_ip('nx.example') is None
    monkeypatch.setattr(nc.socket, 'getaddrinfo', _fake_gai('not-an-ip'))
    assert nc._resolve_safe_ip('h') is None


def test_is_safe_url_scheme_and_host_gates(monkeypatch):
    monkeypatch.setattr(nc.socket, 'getaddrinfo', _fake_gai('93.184.216.34'))
    assert nc.is_safe_url('https://ok.example/x') is True
    assert nc.is_safe_url('ftp://ok.example/x') is False
    assert nc.is_safe_url('http://') is False
    monkeypatch.setattr(nc.socket, 'getaddrinfo', _fake_gai('10.0.0.5'))
    assert nc.is_safe_url('https://internal.example/') is False


# ── safe_get：逐跳重校验 + 跳数上限 ─────────────────────────────────────────

class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSession:
    """回放队列式 Session 桩：记录 GET 序列与全量 kwargs，弹出预设响应。"""
    # ClassVar 是刻意的：三条都是**跨实例共享**的录制队列（桩每次 GET 都 new 一个
    # 实例，记录必须落在类上）。不标注则读者会误以为是实例字段，改成实例属性会
    # 让断言全部读不到数据。用例之间由各自 setUp 显式清空。
    queue: ClassVar[list] = []
    calls: ClassVar[list] = []
    kwargs: ClassVar[list] = []

    def __init__(self):
        pass

    def mount(self, *_a, **_k):
        pass

    def get(self, url, **kw):
        _FakeSession.calls.append((url, kw.get('allow_redirects')))
        _FakeSession.kwargs.append(dict(kw))
        return _FakeSession.queue.pop(0)


@pytest.fixture
def fake_session(monkeypatch):
    _FakeSession.queue = []
    _FakeSession.calls = []
    _FakeSession.kwargs = []
    monkeypatch.setattr(nc.requests, 'Session', _FakeSession)
    monkeypatch.setattr(nc, '_resolve_safe_ip',
                        lambda host: None if 'evil' in host else '93.184.216.34')
    return _FakeSession


def test_safe_get_follows_redirect_with_revalidation(fake_session):
    fake_session.queue = [_Resp(302, {'Location': 'https://b.example/next'}), _Resp(200)]
    resp = nc.safe_get('https://a.example/x')
    assert resp.status_code == 200
    assert [u for u, _ in fake_session.calls] == \
        ['https://a.example/x', 'https://b.example/next']
    assert all(ar is False for _, ar in fake_session.calls)  # 自动重定向必须禁用


def test_safe_get_rejects_unsafe_redirect_target(fake_session):
    fake_session.queue = [_Resp(301, {'Location': 'https://evil.example/'})]
    with pytest.raises(ValueError, match='non-public'):
        nc.safe_get('https://a.example/x')


def test_safe_get_redirect_without_location(fake_session):
    fake_session.queue = [_Resp(303, {})]
    with pytest.raises(ValueError, match='Location'):
        nc.safe_get('https://a.example/x')


def test_safe_get_too_many_redirects(fake_session):
    fake_session.queue = [_Resp(307, {'Location': f'https://a.example/{i}'})
                          for i in range(nc.MAX_REDIRECTS + 1)]
    with pytest.raises(ValueError, match='too many redirects'):
        nc.safe_get('https://a.example/0')
    assert len(fake_session.calls) == nc.MAX_REDIRECTS + 1  # 恰好 N+1 跳后放弃


def test_safe_get_rejects_bad_scheme_upfront(fake_session):
    with pytest.raises(ValueError, match='scheme'):
        nc.safe_get('ftp://a.example/x')
    assert fake_session.calls == []


# ── get_with_retry：重试节拍 + header 合并 ──────────────────────────────────

class _RetrySession:
    # 同上：跨实例共享的录制列表，刻意挂在类上
    outcomes: ClassVar[list] = []
    calls: ClassVar[list] = []

    def __init__(self):
        self.max_redirects = None

    def get(self, url, params=None, headers=None, timeout=None):
        _RetrySession.calls.append({'headers': headers, 'max_redirects': self.max_redirects})
        out = _RetrySession.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _OkResp:
    status_code = 200

    def raise_for_status(self):
        pass


@pytest.fixture
def retry_env(monkeypatch):
    _RetrySession.outcomes = []
    _RetrySession.calls = []
    sleeps = []
    monkeypatch.setattr(nc.requests, 'Session', _RetrySession)
    monkeypatch.setattr(nc.time, 'sleep', sleeps.append)
    return _RetrySession, sleeps


def test_get_with_retry_success_first_try(retry_env):
    sess, sleeps = retry_env
    ok = _OkResp()
    sess.outcomes = [ok]
    assert nc.get_with_retry('https://x', headers={'A': '2'},
                             default_headers={'A': '1', 'B': '1'}) is ok
    assert sleeps == []
    assert sess.calls[0]['headers'] == {'A': '2', 'B': '1'}   # 显式头覆盖默认头
    assert sess.calls[0]['max_redirects'] == nc.MAX_REDIRECTS


def test_get_with_retry_backoff_then_success(retry_env):
    sess, sleeps = retry_env
    ok = _OkResp()
    sess.outcomes = [nc.requests.RequestException('a'),
                     nc.requests.RequestException('b'), ok]
    assert nc.get_with_retry('https://x') is ok
    assert sleeps == [1, 2]                                    # 间隔 1s/2s


def test_get_with_retry_exhausted_raises_last(retry_env):
    sess, sleeps = retry_env
    sess.outcomes = [nc.requests.RequestException(f'e{i}') for i in range(3)]
    with pytest.raises(nc.requests.RequestException, match='e2'):
        nc.get_with_retry('https://x')
    assert sleeps == [1, 2]                                    # 末次失败不再睡


# ── bilibili wbi：黄金值一击全钉 64 项混淆表 ────────────────────────────────

_IMG = 'abcdef0123456789abcdef0123456789'
_SUB = '9876543210fedcba9876543210fedcba'
_GOLDEN_MIXIN = 'bacc42199749fdc65ef883fd76826307'


class _JsonResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_wbi_mixin_key_golden(monkeypatch):
    nc._wbi_cache.clear()
    payload = {'data': {'wbi_img': {
        'img_url': f'https://i0.hdslb.com/bfs/wbi/{_IMG}.png',
        'sub_url': f'https://i0.hdslb.com/bfs/wbi/{_SUB}.png'}}}
    monkeypatch.setattr(nc.requests, 'get', lambda *a, **k: _JsonResp(payload))
    assert nc.get_wbi_mixin_key() == _GOLDEN_MIXIN
    assert len(_GOLDEN_MIXIN) == 32


def test_wbi_mixin_key_cache_and_fallback(monkeypatch):
    nc._wbi_cache.clear()
    nc._wbi_cache.update({'mixin_key': 'cached', 'ts': nc.time.time()})
    monkeypatch.setattr(nc.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('no refetch')))
    assert nc.get_wbi_mixin_key() == 'cached'                 # 30 分钟内走缓存
    nc._wbi_cache['ts'] = 0                                    # 过期 + 拉取失败 → 回退旧值
    monkeypatch.setattr(nc.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
    assert nc.get_wbi_mixin_key() == 'cached'
    nc._wbi_cache.clear()


def test_sign_wbi_params_golden(monkeypatch):
    monkeypatch.setattr(nc.time, 'time', lambda: 1752200000.9)
    signed = nc.sign_wbi_params({'keyword': 'morimens', 'page': 1}, _GOLDEN_MIXIN)
    assert signed['wts'] == 1752200000                         # int 截断
    assert signed['w_rid'] == '504dcad68cdb1eac9a3b476958cad21c'
    assert signed['keyword'] == 'morimens'                     # 原参数保留


def test_spi_cookies(monkeypatch):
    monkeypatch.setattr(nc.requests, 'get',
                        lambda *a, **k: _JsonResp({'data': {'b_3': 'x3', 'b_4': 'x4'}}))
    assert nc.bilibili_spi_cookies() == {'buvid3': 'x3', 'buvid4': 'x4'}
    monkeypatch.setattr(nc.requests, 'get',
                        lambda *a, **k: _JsonResp({'data': {'b_3': 'only3'}}))
    assert nc.bilibili_spi_cookies() == {'buvid3': 'only3'}
    monkeypatch.setattr(nc.requests, 'get',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('net')))
    assert nc.bilibili_spi_cookies() == {}


# ── dump_json_atomic：原子写盘 ──────────────────────────────────────────────

def test_dump_json_atomic_writes_and_cleans(tmp_path):
    target = tmp_path / 'deep' / 'out.json'
    nc.dump_json_atomic(target, {'k': '值'})
    assert json.loads(target.read_text(encoding='utf-8')) == {'k': '值'}
    assert list(target.parent.glob('.*.tmp')) == []            # 不留临时文件


def test_dump_json_atomic_failure_preserves_original(tmp_path):
    target = tmp_path / 'out.json'
    nc.dump_json_atomic(target, {'ok': 1})
    with pytest.raises(TypeError):
        nc.dump_json_atomic(target, {'bad': object()})
    assert json.loads(target.read_text(encoding='utf-8')) == {'ok': 1}  # 旧文件完好
    assert list(tmp_path.glob('.*.tmp')) == []                 # 半截临时文件已清


# ══ 首跑存活体击杀区（守密人 2026-07-11 扩员裁定的补断言轮）════════════════
# 以下断言按首跑存活类逐类补钉：签名默认值 / 实参常量 / 缓存语义 / 序列化
# 原样文本 / epoch 边界。stub 边界不可观测类（_PinnedIPAdapter 内部、真连接
# 行为）不在此杀，见 memory/testing-strategy.md 白名单。


def test_make_item_signature_defaults_pinned():
    # 击杀 make_item__mutmut_1..7：只给必填位，逐字段钉全部签名默认值
    item = nc.make_item('t', 's', 'src', 'pr', 'time0', 'u')
    assert item == {
        'title': 't', 'summary': 's', 'source': 'src', 'platform_region': 'pr',
        'lang': '', 'time': 'time0', 'url': 'u', 'engagement': 0, 'is_hot': False,
        'author': '', 'tags': [], 'content_type': 'text', 'media_url': '',
    }


def test_prt_epoch_boundary_and_overflow():
    # 击杀 ts>1e11 边界与 OverflowError 旗标翻转类
    iso_at, approx_at = nc.parse_relative_time(100000000000)      # ==1e11 → 按秒
    assert approx_at is False and iso_at.startswith('5138-')
    iso_over, _ = nc.parse_relative_time(100000000001)            # 刚过线 → 按毫秒
    assert iso_over.startswith('1973-')
    iso_bad, approx_bad = nc.parse_relative_time(10 ** 300)       # 溢出 → 回退近似
    assert approx_bad is True
    # int 0 为假值：走「空值」早退近似分支，绝不当 epoch 0 解析成 1970
    iso_zero, approx_zero = nc.parse_relative_time(0)
    assert approx_zero is True and not iso_zero.startswith('1970-')


def test_safe_get_passes_exact_kwargs(fake_session):
    # 击杀 safe_get 签名默认值/实参类：timeout=30 / stream=False / headers 透传
    fake_session.queue = [_Resp(200)]
    nc.safe_get('https://a.example/x', headers={'H': '1'})
    kw = fake_session.kwargs[0]
    assert kw == {'headers': {'H': '1'}, 'timeout': 30,
                  'stream': False, 'allow_redirects': False}


def test_safe_get_accepts_plain_http(fake_session):
    # 击杀 scheme 元组字符串变异（'http' → 大小写/加料变体）
    fake_session.queue = [_Resp(200)]
    assert nc.safe_get('http://a.example/x').status_code == 200


def test_get_with_retry_passes_exact_args(monkeypatch):
    # 击杀 get_with_retry 签名默认值/实参类：timeout=15 / retries=3 / params 透传
    recorded = {}

    class _S:
        def __init__(self):
            self.max_redirects = None

        def get(self, url, params=None, headers=None, timeout=None):
            recorded.update(url=url, params=params, headers=headers,
                            timeout=timeout, max_redirects=self.max_redirects)
            recorded['n'] = recorded.get('n', 0) + 1
            raise nc.requests.RequestException('always down')

    monkeypatch.setattr(nc.requests, 'Session', _S)
    monkeypatch.setattr(nc.time, 'sleep', lambda *_: None)
    with pytest.raises(nc.requests.RequestException):
        nc.get_with_retry('https://x', params={'p': 1})
    assert recorded['n'] == 3                                  # retries 默认恰为 3
    assert recorded['params'] == {'p': 1}
    assert recorded['timeout'] == 15
    assert recorded['max_redirects'] == nc.MAX_REDIRECTS


def test_wbi_fetch_hits_exact_endpoint(monkeypatch):
    # 击杀 wbi 端点 URL / timeout 实参变异：stub 录制实参并断言
    nc._wbi_cache.clear()
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, timeout=timeout)
        seen['n'] = seen.get('n', 0) + 1
        return _JsonResp({'data': {'wbi_img': {
            'img_url': f'https://i0.hdslb.com/bfs/wbi/{_IMG}.png',
            'sub_url': f'https://i0.hdslb.com/bfs/wbi/{_SUB}.png'}}})

    monkeypatch.setattr(nc.requests, 'get', fake_get)
    assert nc.get_wbi_mixin_key() == _GOLDEN_MIXIN
    assert seen['url'] == 'https://api.bilibili.com/x/web-interface/nav'
    assert seen['timeout'] == 10
    nc._wbi_cache.clear()


def test_wbi_stale_cache_refetches_new_value(monkeypatch):
    # 击杀缓存条件 and→or：过期缓存 + 可用后端必须取回新值而非续用旧值
    nc._wbi_cache.clear()
    nc._wbi_cache.update({'mixin_key': 'stale-old', 'ts': 0})
    monkeypatch.setattr(nc.requests, 'get', lambda *a, **k: _JsonResp(
        {'data': {'wbi_img': {
            'img_url': f'https://i0.hdslb.com/bfs/wbi/{_IMG}.png',
            'sub_url': f'https://i0.hdslb.com/bfs/wbi/{_SUB}.png'}}}))
    assert nc.get_wbi_mixin_key() == _GOLDEN_MIXIN             # 不是 'stale-old'
    assert nc._wbi_cache['ts'] > 0                              # 时间戳已刷新
    nc._wbi_cache.clear()


def test_wbi_fresh_cache_makes_zero_requests(monkeypatch):
    # 击杀缓存键字符串变异：新鲜缓存下后端调用数必须为 0（异常吞不掉计数）
    nc._wbi_cache.clear()
    nc._wbi_cache.update({'mixin_key': 'fresh', 'ts': nc.time.time()})
    n = {'calls': 0}

    def counting_get(*a, **k):
        n['calls'] += 1
        raise RuntimeError('must not be called')

    monkeypatch.setattr(nc.requests, 'get', counting_get)
    assert nc.get_wbi_mixin_key() == 'fresh'
    assert n['calls'] == 0
    nc._wbi_cache.clear()


def test_spi_hits_exact_endpoint(monkeypatch):
    # 击杀 spi 端点 URL / timeout 实参变异
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, timeout=timeout)
        return _JsonResp({'data': {'b_3': 'x3'}})

    monkeypatch.setattr(nc.requests, 'get', fake_get)
    assert nc.bilibili_spi_cookies() == {'buvid3': 'x3'}
    assert seen['url'] == 'https://api.bilibili.com/x/frontend/finger/spi'
    assert seen['timeout'] == 10


def test_dump_json_atomic_exact_serialization(tmp_path):
    # 击杀 indent=2 / ensure_ascii=False 变异：断言原样文本而非仅可解析
    target = tmp_path / 'exact.json'
    nc.dump_json_atomic(target, {'k': '值', 'n': [1, 2]})
    assert target.read_text(encoding='utf-8') == \
        '{\n  "k": "值",\n  "n": [\n    1,\n    2\n  ]\n}'


def test_dump_json_atomic_tmpfile_naming_and_dir(tmp_path, monkeypatch):
    # 击杀 mkstemp dir/prefix/suffix 实参变异：同目录 + .<名>. 前缀 + .tmp 后缀
    # （同目录是 os.replace 原子性的前提——跨文件系统即退化为拷贝）
    seen = {}
    real_mkstemp = nc.tempfile.mkstemp

    def spy_mkstemp(**kw):
        seen.update(kw)
        return real_mkstemp(**kw)

    monkeypatch.setattr(nc.tempfile, 'mkstemp', spy_mkstemp)
    target = tmp_path / 'named.json'
    nc.dump_json_atomic(target, {'a': 1})
    assert seen['dir'] == str(tmp_path)
    assert seen['prefix'] == '.named.json.'
    assert seen['suffix'] == '.tmp'


# ══ 二轮击杀：冻结时钟下的精确值断言（MM-DD / MM/DD / HH:MM 分支）═══════════
# 首跑+一轮后 parse_relative_time 仍存活 56：容差断言杀不动「置零字段 / 月日
# 映射 / 回退边界」类，且 MM-DD（短横/斜线）形态此前完全漏测。此处用 datetime
# 子类冻结 now，把这些分支钉成精确等值。


class _FrozenDT(datetime):
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


@pytest.fixture
def frozen_now(monkeypatch):
    def freeze(dt):
        _FrozenDT._frozen = dt
        monkeypatch.setattr(nc, 'datetime', _FrozenDT)
        return dt
    return freeze


def test_prt_month_day_dot_exact(frozen_now):
    frozen_now(datetime(2026, 7, 11, 10, 20, 30, 123456, tzinfo=UTC))
    # 过去的 MM.DD → 当年精确零点（钉 month/day 映射与 hour/min/sec/µs 全置零）
    assert nc.parse_relative_time('3.5') == ('2026-03-05T00:00:00+00:00', False)
    # 未来的 MM.DD → 回退恰一年
    assert nc.parse_relative_time('12.31') == ('2025-12-31T00:00:00+00:00', False)


def test_prt_month_day_dash_slash_exact(frozen_now):
    # 短横 / 斜线形态此前漏测（首跑 m=None 变异存活的病根）
    frozen_now(datetime(2026, 7, 11, 10, 20, 30, 123456, tzinfo=UTC))
    assert nc.parse_relative_time('3-5') == ('2026-03-05T00:00:00+00:00', False)
    assert nc.parse_relative_time('03/05') == ('2026-03-05T00:00:00+00:00', False)
    assert nc.parse_relative_time('12-31') == ('2025-12-31T00:00:00+00:00', False)
    assert nc.parse_relative_time('12/31') == ('2025-12-31T00:00:00+00:00', False)


def test_prt_month_day_equal_boundary_no_rollback(frozen_now):
    # dt == now 恰相等：> 判定不回退（>= 变异体在此翻车）
    frozen_now(datetime(2026, 7, 11, 0, 0, 0, 0, tzinfo=UTC))
    assert nc.parse_relative_time('7.11') == ('2026-07-11T00:00:00+00:00', False)
    assert nc.parse_relative_time('7-11') == ('2026-07-11T00:00:00+00:00', False)


def test_prt_hhmm_exact(frozen_now):
    frozen_now(datetime(2026, 7, 11, 10, 20, 30, 987654, tzinfo=UTC))
    # 过去时刻 → 当日精确到分（sec/µs 全置零）
    assert nc.parse_relative_time('09:15') == ('2026-07-11T09:15:00+00:00', False)
    # 未来时刻 → 回退恰一天
    assert nc.parse_relative_time('11:45') == ('2026-07-10T11:45:00+00:00', False)


def test_prt_hhmm_equal_boundary_no_rollback(frozen_now):
    frozen_now(datetime(2026, 7, 11, 10, 20, 0, 0, tzinfo=UTC))
    assert nc.parse_relative_time('10:20') == ('2026-07-11T10:20:00+00:00', False)


def test_prt_seconds_ago_exact(frozen_now):
    # 击杀 delta_map 'second' 键变异：此前英文相对时间漏测 seconds 单位
    frozen_now(datetime(2026, 7, 11, 10, 20, 30, 0, tzinfo=UTC))
    assert nc.parse_relative_time('30 seconds ago') == \
        ('2026-07-11T10:20:00+00:00', False)
    assert nc.parse_relative_time('2 weeks ago') == \
        ('2026-06-27T10:20:30+00:00', False)


# ══ 二轮击杀：safe_get 挂载/收尾/相对跳转 与 守卫实参录制 ═══════════════════


def test_safe_get_mounts_pinned_adapter_and_closes_redirects(monkeypatch):
    mounts = []

    class _RecSession(_FakeSession):
        def mount(self, prefix, adapter):
            mounts.append((prefix, type(adapter).__name__, adapter._pinned_ip))

    _FakeSession.queue = [_Resp(302, {'Location': 'https://a.example/n'}), _Resp(200)]
    _FakeSession.calls = []
    _FakeSession.kwargs = []
    redirect_resp = _FakeSession.queue[0]
    monkeypatch.setattr(nc.requests, 'Session', _RecSession)
    monkeypatch.setattr(nc, '_resolve_safe_ip', lambda host: '93.184.216.34')
    final = nc.safe_get('https://a.example/x')
    # 两种 scheme 前缀各挂 pinned adapter，且 pin 的是守卫解析出的那颗 IP
    assert mounts[:2] == [('http://', '_PinnedIPAdapter', '93.184.216.34'),
                          ('https://', '_PinnedIPAdapter', '93.184.216.34')]
    assert redirect_resp.closed is True      # 3xx 响应必须关闭（防连接泄漏）
    assert final.closed is False             # 最终响应留给调用方消费


def test_safe_get_resolves_relative_location(fake_session):
    # 相对 Location 须以当前 URL 为基解析（urljoin 实参序被换即翻车）
    fake_session.queue = [_Resp(301, {'Location': '/deeper/page'}), _Resp(200)]
    nc.safe_get('https://a.example/start/here')
    assert fake_session.calls[1][0] == 'https://a.example/deeper/page'


def test_resolve_safe_ip_calls_getaddrinfo_with_null_service(monkeypatch):
    # 击杀 getaddrinfo 实参变异：host 原样、service 必须为 None
    seen = {}

    def spy_gai(host, port):
        seen.update(host=host, port=port)
        return [(2, 1, 6, '', ('93.184.216.34', 0))]

    monkeypatch.setattr(nc.socket, 'getaddrinfo', spy_gai)
    assert nc._resolve_safe_ip('h.example') == '93.184.216.34'
    assert seen == {'host': 'h.example', 'port': None}


# ══ 二轮击杀：wbi 缓存精确态 / spi 缺 data 键与 headers 透传 ════════════════


def test_wbi_cache_exact_state_after_fetch(monkeypatch):
    nc._wbi_cache.clear()
    monkeypatch.setattr(nc.time, 'time', lambda: 1752200000.0)
    monkeypatch.setattr(nc.requests, 'get', lambda *a, **k: _JsonResp(
        {'data': {'wbi_img': {
            'img_url': f'https://i0.hdslb.com/bfs/wbi/{_IMG}.png',
            'sub_url': f'https://i0.hdslb.com/bfs/wbi/{_SUB}.png'}}}))
    assert nc.get_wbi_mixin_key() == _GOLDEN_MIXIN
    # 缓存全字段精确态：ts 必须等于取数时刻（+1/-1 变异皆翻车）
    assert nc._wbi_cache == {'mixin_key': _GOLDEN_MIXIN, 'ts': 1752200000.0}
    nc._wbi_cache.clear()


def test_wbi_and_spi_pass_headers_through(monkeypatch):
    nc._wbi_cache.clear()
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(headers)
        if 'nav' in url:
            return _JsonResp({'data': {'wbi_img': {
                'img_url': f'https://x/{_IMG}.png', 'sub_url': f'https://x/{_SUB}.png'}}})
        return _JsonResp({'data': {'b_3': 'x'}})

    monkeypatch.setattr(nc.requests, 'get', fake_get)
    nc.get_wbi_mixin_key(headers={'UA': 'erica'})
    nc.bilibili_spi_cookies(headers={'UA': 'erica'})
    assert seen == [{'UA': 'erica'}, {'UA': 'erica'}]
    nc._wbi_cache.clear()


def test_spi_missing_data_key_yields_empty(monkeypatch):
    monkeypatch.setattr(nc.requests, 'get', lambda *a, **k: _JsonResp({}))
    assert nc.bilibili_spi_cookies() == {}


# ══ 终轮击杀：等价类之外最后一批可确定性观测的存活体 ═══════════════════════


def test_resolve_safe_ip_rejects_reserved_range(monkeypatch):
    # 240.0.0.0/4 保留段不在 is_private 射程内——is_reserved 判断被布尔重排
    # 吞掉时（or→and）唯有此段能翻车
    monkeypatch.setattr(nc.socket, 'getaddrinfo', _fake_gai('240.0.0.1'))
    assert nc._resolve_safe_ip('h') is None


def test_is_safe_url_plain_http_and_host_passthrough(monkeypatch):
    # 击杀 is_safe_url 的 'http' 元组变异 + host 实参丢弃变异
    seen = {}

    def spy_gai(host, port):
        seen['host'] = host
        return [(2, 1, 6, '', ('93.184.216.34', 0))]

    monkeypatch.setattr(nc.socket, 'getaddrinfo', spy_gai)
    assert nc.is_safe_url('http://plain.example/x') is True
    assert seen['host'] == 'plain.example'         # host 必须原样传给解析器


def test_safe_get_follows_308(fake_session):
    # 308 Permanent Redirect 在跳转码元组末位，末位数字变异唯此能杀
    fake_session.queue = [_Resp(308, {'Location': 'https://a.example/n'}), _Resp(200)]
    assert nc.safe_get('https://a.example/x').status_code == 200
    assert len(fake_session.calls) == 2


def test_get_with_retry_url_reaches_session(monkeypatch):
    seen = {}

    class _S:
        def __init__(self):
            self.max_redirects = None

        def get(self, url, params=None, headers=None, timeout=None):
            seen['url'] = url
            return _OkResp()

    monkeypatch.setattr(nc.requests, 'Session', _S)
    nc.get_with_retry('https://exact.example/path')
    assert seen['url'] == 'https://exact.example/path'


def test_wbi_cache_expiry_boundary_exactly_1800s(monkeypatch):
    # 缓存寿命恰 1800 秒：< 判定视为过期须重取（<= / 1801 变异在此翻车）
    nc._wbi_cache.clear()
    nc._wbi_cache.update({'mixin_key': 'old', 'ts': 0.0})
    monkeypatch.setattr(nc.time, 'time', lambda: 1800.0)
    monkeypatch.setattr(nc.requests, 'get', lambda *a, **k: _JsonResp(
        {'data': {'wbi_img': {
            'img_url': f'https://x/{_IMG}.png', 'sub_url': f'https://x/{_SUB}.png'}}}))
    assert nc.get_wbi_mixin_key() == _GOLDEN_MIXIN  # 已过期：取新，非续 'old'
    nc._wbi_cache.clear()


def test_dump_json_atomic_creates_two_missing_levels(tmp_path):
    # parents=True 只有在缺两级以上目录时才与缺省行为可区分
    target = tmp_path / 'lv1' / 'lv2' / 'out.json'
    nc.dump_json_atomic(target, {'a': 1})
    assert json.loads(target.read_text(encoding='utf-8')) == {'a': 1}


def test_dump_json_atomic_fdopen_encoding_exact(tmp_path, monkeypatch):
    # 落盘编码必须显式小写 'utf-8'——不许吃 locale 缺省（云容器 locale 不可控）
    seen = {}
    real_fdopen = nc.os.fdopen

    def spy_fdopen(fd, mode, **kw):
        seen.update(mode=mode, **kw)
        return real_fdopen(fd, mode, **kw)

    monkeypatch.setattr(nc.os, 'fdopen', spy_fdopen)
    nc.dump_json_atomic(tmp_path / 'enc.json', {'k': '值'})
    assert seen == {'mode': 'w', 'encoding': 'utf-8'}
