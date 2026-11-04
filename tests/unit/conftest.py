"""单元测试公共夹具：每个用例独立的临时 SQLite。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """把 DB 与审计 JSONL 指到临时目录，并重建单例（隔离用例间状态）。"""
    monkeypatch.setenv("AIOPS_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AIOPS_AUDIT_JSONL", str(tmp_path / "audit.jsonl"))
    from src.common import db as db_mod

    db_mod.reset_db()
    yield db_mod
    db_mod.reset_db()
