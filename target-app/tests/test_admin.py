"""admin API 与限流注册表单测（block / unblock / rate_limit / 到期自动解除）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from demo_app.admin import admin_app
from demo_app.limits import LimitsRegistry


def test_block_and_unblock():
    reg = LimitsRegistry()
    reg.block_ip("1.2.3.4", duration_min=10)
    assert reg.check_ip("1.2.3.4") is True
    assert reg.check_ip("5.6.7.8") is False
    reg.unblock_ip("1.2.3.4")
    assert reg.check_ip("1.2.3.4") is False


def test_block_expires_automatically():
    import time
    reg = LimitsRegistry()
    reg.block_ip("1.2.3.4", duration_min=10)
    # 时间快进到过期点之后 → 惰性清除，自动解除
    assert reg.check_ip("1.2.3.4", now=time.time() + 10 * 60 + 1) is False


def test_rate_limit_triggers_and_expires():
    import time
    reg = LimitsRegistry()
    reg.set_rate_limit(qps=2, duration_min=5)
    now = time.time()
    assert reg.check_rate(now) is False            # 第 1 个
    assert reg.check_rate(now + 0.01) is False     # 第 2 个
    assert reg.check_rate(now + 0.02) is True      # 第 3 个 → 超 qps=2 → 429
    # 过期后自动解除
    assert reg.check_rate(now + 5 * 60 + 1) is False


def test_admin_api_flow():
    with TestClient(admin_app) as client:
        r = client.post("/admin/limits", json={"action": "block", "ip": "9.9.9.9",
                                               "duration_min": 10})
        assert r.status_code == 200
        assert "9.9.9.9" in r.json()["blocked_ips"]

        r = client.get("/admin/state")
        assert "9.9.9.9" in r.json()["blocked_ips"]

        r = client.post("/admin/limits", json={"action": "rate_limit", "qps": 50,
                                               "duration_min": 5})
        assert r.json()["rate_limit"]["qps"] == 50

        r = client.post("/admin/limits", json={"action": "unblock", "ip": "9.9.9.9"})
        assert r.json()["blocked_ips"] == {}

        r = client.post("/admin/limits", json={"action": "clear_rate"})
        assert r.json()["rate_limit"] is None

        r = client.post("/admin/limits", json={"action": "nonsense"})
        assert r.status_code == 400
