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
