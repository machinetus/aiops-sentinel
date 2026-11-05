"""orders 缺陷矩阵单测（design §9.3）：type_bug 500 / off 400 / 正常 200。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from demo_app.main import app
from demo_app.orders import bug_mode, create_order


def test_valid_order_ok(monkeypatch):
    monkeypatch.setenv("BUG_MODE", "off")
    order = create_order({"item": "sku-1", "amount": 3, "price": 2})
    assert order["total"] == 6


def test_type_bug_mode_string_amount_raises(monkeypatch):
    """缺陷本体：type_bug 下字符串 amount → int('abc') 抛 ValueError。"""
    monkeypatch.setenv("BUG_MODE", "type_bug")
    assert bug_mode() == "type_bug"
    with pytest.raises(ValueError):
        create_order({"item": "sku-1", "amount": "abc", "price": 2})


def test_off_mode_string_amount_rejected(monkeypatch):
    monkeypatch.setenv("BUG_MODE", "off")
    with pytest.raises(HTTPException) as ei:
        create_order({"item": "sku-1", "amount": "abc", "price": 2})
    assert ei.value.status_code == 400


def test_endpoint_reflects_bug_mode(monkeypatch):
    """端到端：env=type_bug 时 POST /api/orders 返回 500；env=off 返回 400。"""
    monkeypatch.setenv("BUG_MODE", "type_bug")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/orders",
                           json={"item": "x", "amount": "abc", "price": 2}).status_code == 500
    monkeypatch.setenv("BUG_MODE", "off")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/orders",
                           json={"item": "x", "amount": "abc", "price": 2}).status_code == 400
