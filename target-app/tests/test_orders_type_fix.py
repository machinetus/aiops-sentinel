'''Regression test for /api/orders amount type handling.'''

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from demo_app.main import app
from demo_app.orders import create_order


def test_create_order_string_amount_raises_400(monkeypatch):
    monkeypatch.setenv('BUG_MODE', 'type_bug')
    with pytest.raises(HTTPException) as exc_info:
        create_order({'item': 'x', 'amount': 'abc', 'price': 2})
    assert exc_info.value.status_code == 400


def test_endpoint_string_amount_returns_400_not_500(monkeypatch):
    monkeypatch.setenv('BUG_MODE', 'type_bug')
    monkeypatch.setenv('DEMO_APP_NO_ADMIN', '1')
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post('/api/orders', json={'item': 'x', 'amount': 'abc', 'price': 2})
    assert response.status_code == 400
    assert response.json()['detail'] == 'amount must be an integer'
