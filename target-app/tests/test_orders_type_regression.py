"""orders 类型缺陷回归测试：非法 amount 必须 400，不得冒泡为 500。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from demo_app.main import app
from demo_app.orders import create_order


def test_create_order_rejects_string_amount_in_all_modes(monkeypatch):
    for mode in ("type_bug", "off"):
        monkeypatch.setenv("BUG_MODE", mode)
        with pytest.raises(HTTPException) as ei:
            create_order({"item": "x", "amount": "abc", "price": 2})
        assert ei.value.status_code == 400


def test_endpoint_returns_400_for_sample_payload(monkeypatch):
    monkeypatch.setenv("DEMO_APP_NO_ADMIN", "1")
    for mode in ("type_bug", "off"):
        monkeypatch.setenv("BUG_MODE", mode)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/orders",
                json={"item": "x", "amount": "abc", "price": 2},
            )
        assert response.status_code == 400, response.text
        assert "amount must be an integer" in response.text
