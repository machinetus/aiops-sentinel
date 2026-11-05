"""orders 缺陷矩阵单测（design §9.3）：非法 amount 400 / 正常 200。"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from demo_app.main import app
from demo_app.orders import bug_mode, create_order


def test_valid_order_ok(monkeypatch):
    monkeypatch.setenv("BUG_MODE", "off")
    order = create_order({"item": "sku-1", "amount": 3, "price": 2})
    assert order["total"] == 6


def test_type_bug_mode_string_amount_rejected(monkeypatch):
    """修复后：type_bug 下字符串 amount 同样返回 400，而非 500。"""
    monkeypatch.setenv("BUG_MODE", "type_bug")
    assert bug_mode() == "type_bug"
    with pytest.raises(HTTPException) as ei:
        create_order({"item": "sku-1", "amount": "abc", "price": 2})
    assert ei.value.status_code == 400


def test_off_mode_string_amount_rejected(monkeypatch):
    monkeypatch.setenv("BUG_MODE", "off")
    with pytest.raises(HTTPException) as ei:
        create_order({"item": "sku-1", "amount": "abc", "price": 2})
    assert ei.value.status_code == 400


def test_endpoint_rejects_invalid_amount(monkeypatch):
    """端到端：非法 amount 在 type_bug / off 模式下均返回 400。"""
    for mode in ("type_bug", "off"):
        monkeypatch.setenv("BUG_MODE", mode)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.post("/api/orders",
                               json={"item": "x", "amount": "abc", "price": 2}).status_code == 400


def test_endpoint_converts_valueerror_to_400(monkeypatch):
    """防御性兜底：业务层抛 ValueError 时端点返回 400，而非 500。"""
    monkeypatch.setenv("DEMO_APP_NO_ADMIN", "1")

    def boom(_body):
        raise ValueError("invalid literal for int() with base 10: 'abc'")

    monkeypatch.setattr("demo_app.main.create_order", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/api/orders",
                        json={"item": "sku-1", "amount": "abc", "price": 2})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid order payload"


def test_invalid_amount_does_not_emit_error_log(monkeypatch, caplog):
    """非法 amount 属于客户端错误，不应产生 ERROR 级别日志。"""
    monkeypatch.setenv("DEMO_APP_NO_ADMIN", "1")
    monkeypatch.setenv("BUG_MODE", "type_bug")
    caplog.set_level(logging.ERROR, logger="demo_app")
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/api/orders",
                        json={"item": "sku-1", "amount": "abc", "price": 2})
    assert r.status_code == 400
    assert [rec for rec in caplog.records
            if rec.name == "demo_app" and rec.levelno >= logging.ERROR] == []
