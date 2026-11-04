"""白名单执行器单测（三重门第三重，design §5.1/§8）。

覆盖：未定义动作 / 命令前缀越界 / 未声明参数 / ip 格式校验 /
      exec 数组执行 / http 仅限回环 / 无复检配置。
"""

from __future__ import annotations

import pytest

from src.workers import whitelist
from src.workers.whitelist import WhitelistDenied, WhitelistExecutor

ACTIONS = {
    "noop": {"type": "exec", "command": ["/bin/true"]},
    "bad_prefix": {"type": "exec", "command": ["/bin/sh", "-c", "id"]},
    "block_ip": {
        "type": "http", "method": "POST",
        "url": "http://127.0.0.1:9101/admin/limits",
        "body": {"action": "block", "ip": "{ip}", "duration_min": 10},
    },
    "evil_url": {"type": "http", "url": "http://8.8.8.8/steal", "body": {}},
    "restart": {
        "type": "exec",
        "command": ["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "demo-app.service"],
        "verify": {"type": "http", "url": "http://127.0.0.1:18000/health", "expect": 200},
        "verify_attempts": 1, "verify_interval": 0,
    },
}


@pytest.fixture()
def executor():
    return WhitelistExecutor(ACTIONS)


def test_unknown_action_denied(executor):
    with pytest.raises(WhitelistDenied):
        executor.execute("rm_rf_slash", {})


def test_command_prefix_out_of_allowlist_denied(executor):
    """配置被篡改成 /bin/sh 也必须被运行时兜底拦截。"""
    with pytest.raises(WhitelistDenied) as ei:
        executor.execute("bad_prefix", {})
    assert "前缀" in str(ei.value)


def test_undeclared_params_denied(executor):
    with pytest.raises(WhitelistDenied) as ei:
        executor.execute("block_ip", {"ip": "1.2.3.4", "cmd": "rm -rf /"})
    assert "未声明参数" in str(ei.value)


def test_invalid_ip_denied(executor):
    with pytest.raises(WhitelistDenied):
        executor.execute("block_ip", {"ip": "1.2.3.4; rm -rf /"})


def test_http_action_only_loopback(executor):
    with pytest.raises(WhitelistDenied) as ei:
        executor.execute("evil_url", {})
    assert "127.0.0.1" in str(ei.value)


def test_exec_noop_ok():
    """跨平台：用当前解释器替代 /bin/true（Windows 本地也能跑）。"""
    import sys

    ex = WhitelistExecutor(
        {"noop": {"type": "exec", "command": [sys.executable, "-c", "pass"]}},
        allowed_prefixes=[sys.executable],
    )
    outcome = ex.execute("noop", {})
    assert outcome.ok and outcome.kind == "exec" and outcome.detail["exit_code"] == 0


def test_http_action_filled_body(monkeypatch, executor):
    captured = {}

    class FakeResp:
        status_code = 200
        text = '{"blocked_ips": {"1.2.3.4": 600}}'

    def fake_post(url, json, timeout):  # noqa: A002
        captured["url"], captured["body"] = url, json
        return FakeResp()

    monkeypatch.setattr(whitelist.httpx, "post", fake_post)
    outcome = executor.execute("block_ip", {"ip": "1.2.3.4"})
    assert outcome.ok and captured["body"]["ip"] == "1.2.3.4"
    assert captured["url"] == "http://127.0.0.1:9101/admin/limits"


def test_verify_without_config_passes(executor):
    assert executor.verify("noop") == {"attempts": 0, "passed": True,
                                       "note": "无复检配置（执行成功即视为完成）"}


def test_verify_success(monkeypatch, executor):
    class FakeResp:
        status_code = 200

    monkeypatch.setattr(whitelist.httpx, "get", lambda url, timeout: FakeResp())
    out = executor.verify("restart")
    assert out == {"attempts": 1, "passed": True}


def test_verify_failure_after_attempts(monkeypatch, executor):
    class FakeResp:
        status_code = 503

    monkeypatch.setattr(whitelist.httpx, "get", lambda url, timeout: FakeResp())
    out = executor.verify("restart")
    assert out["passed"] is False and out["attempts"] == 1
