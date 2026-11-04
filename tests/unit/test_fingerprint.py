"""fingerprint.py 单测：确定性、字段敏感性、格式（design §3.2）。"""

from __future__ import annotations

import re

from src.common.fingerprint import make_fingerprint

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_fingerprint_is_sha256_hex():
    fp = make_fingerprint("probe:http", "demo-app", "http_5xx")
    assert HEX64.match(fp)


def test_fingerprint_deterministic():
    a = make_fingerprint("probe:http", "demo-app", "http_5xx")
    b = make_fingerprint("probe:http", "demo-app", "http_5xx")
    assert a == b


def test_fingerprint_differs_per_field():
    base = make_fingerprint("probe:http", "demo-app", "http_5xx")
    assert base != make_fingerprint("probe:process", "demo-app", "http_5xx")  # source 不同
    assert base != make_fingerprint("probe:http", "other-app", "http_5xx")    # subject 不同
    assert base != make_fingerprint("probe:http", "demo-app", "ValueError")   # error_class 不同


def test_fingerprint_none_error_class_stable():
    assert make_fingerprint("webhook", "demo-app", None) == make_fingerprint("webhook", "demo-app", None)
    assert make_fingerprint("webhook", "demo-app", None) != make_fingerprint("webhook", "demo-app", "X")
