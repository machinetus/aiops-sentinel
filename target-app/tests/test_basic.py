"""基础接口单测（沙箱回归的一部分，design §11）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from demo_app.main import app


def test_health_ok():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "demo-app"


def test_index_page_served():
    """业务前端单页可访问（状态条 + 下单 + 搜索）。"""
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Sentinel Demo Store" in r.text
        assert "amount" in r.text


def test_metrics_has_qps():
    with TestClient(app) as client:
        client.get("/health")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert isinstance(r.json()["qps"], float)


def test_search_returns_results():
    with TestClient(app) as client:
        r = client.get("/api/search", params={"q": "ops"})
        assert r.status_code == 200
        assert len(r.json()["results"]) == 5


def test_recent_orders_listed_after_create(monkeypatch):
    """业务页面订单列表：下单成功后可在 /api/orders 查到。"""
    monkeypatch.setenv("BUG_MODE", "off")
    with TestClient(app) as client:
        r = client.post("/api/orders",
                        json={"item": "t1", "amount": 3, "price": 2})
        assert r.status_code == 200
        oid = r.json()["order_id"]
        data = client.get("/api/orders").json()
        assert any(o["order_id"] == oid for o in data["orders"])


def test_metrics_includes_limits(monkeypatch):
    monkeypatch.setenv("BUG_MODE", "off")
    with TestClient(app) as client:
        data = client.get("/metrics").json()
        assert "limits" in data and "blocked_ips" in data["limits"]
