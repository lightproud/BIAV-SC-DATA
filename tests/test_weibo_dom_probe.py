"""T109 微博 DOM 骨架埋点：净化函数不泄露文本的离线单测（样本自造）。"""
import logging

import playwright_collectors as pc


def test_sanitize_redacts_chinese_digits_email_host():
    raw = ('articles=3 ## A0:article.card>a{href=https://m.weibo.cn/u/1234567890}(t12)'
           '|span(t3)用户名张三 联系 test.user@example.com 单号12345678 短号123')
    out = pc.sanitize_dom_skeleton(raw)
    assert '张' not in out and '用户名' not in out
    assert 'test.user' not in out and 'example.com' not in out and '@' not in out
    assert 'm.weibo.cn' not in out
    assert '1234567890' not in out and '12345678' not in out
    assert '123' in out  # 短数字（行数/长度）保留
    assert not any(ord(c) > 127 for c in out)


def test_sanitize_keeps_shape_tokens_and_limits_length():
    out = pc.sanitize_dom_skeleton('a{href=//H/detail/N}(t5)' + 'x' * 9000, limit=4000)
    assert '//H/detail/N' in out and len(out) == 4000


def test_sanitize_idempotent_on_clean():
    s = 'article.card[data-id]>a{href=/u/N}(t4)'
    assert pc.sanitize_dom_skeleton(s) == s


def test_probe_logs_sanitized_and_swallows_errors(monkeypatch, caplog):
    class Page:
        def evaluate(self, js):
            return 'articles=1 ## A0:div(t9)中文泄露 99999999@qq.com'

    monkeypatch.setenv('WEIBO_DOM_PROBE', '1')
    with caplog.at_level(logging.INFO):
        pc._log_weibo_dom_probe(Page())
    msg = ''.join(r.getMessage() for r in caplog.records)
    assert '微博 DOM 骨架' in msg and '中文泄露' not in msg and 'qq.com' not in msg

    class Bad:
        def evaluate(self, js):
            raise RuntimeError('boom')
    pc._log_weibo_dom_probe(Bad())  # 不抛


def test_probe_switch_off(monkeypatch):
    class Page:
        def evaluate(self, js):
            raise AssertionError('不应调用')
    monkeypatch.setenv('WEIBO_DOM_PROBE', '0')
    pc._log_weibo_dom_probe(Page())
