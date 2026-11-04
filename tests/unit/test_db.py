"""db.py 单测：建表 / WAL / 版本号 / 基本读写（implement.md 阶段 1 验证项）。"""

from __future__ import annotations

from src.common.db import SCHEMA_VERSION, Database, get_db, utc_now_iso

EXPECTED_TABLES = {"alert", "incident", "task", "action_log", "patch_proposal"}


def test_init_creates_all_five_tables(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    rows = db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= names


def test_wal_mode_enabled(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    mode = db.query_one("PRAGMA journal_mode;")[0]
    assert mode == "wal"


def test_schema_version_recorded(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    assert db.query_one("PRAGMA user_version;")[0] == SCHEMA_VERSION


def test_init_schema_is_idempotent(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    db.init_schema()  # 重复执行不报错
    rows = db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
    assert EXPECTED_TABLES <= {r["name"] for r in rows}


def test_task_roundtrip(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    now = utc_now_iso()
    db.execute(
        "INSERT INTO incident (title, classification, state, severity, created_at, updated_at) "
        "VALUES ('t', 'process_down', 'open', 'critical', ?, ?)",
        (now, now),
    )
    cur = db.execute(
        "INSERT INTO task (incident_id, type, status, payload_json, created_at, updated_at) "
        "VALUES (1, 'ops', 'queued', '{\"action\": \"restart_demo_app\"}', ?, ?)",
        (now, now),
    )
    task_id = cur.lastrowid
    row = db.query_one("SELECT incident_id, type, status, payload_json FROM task WHERE id = ?", (task_id,))
    assert row["incident_id"] == 1
    assert row["type"] == "ops"
    assert row["status"] == "queued"
    assert "restart_demo_app" in row["payload_json"]


def test_singleton_reset_rebuilds_with_env(fresh_db):
    """reset_db() 后按新 AIOPS_DB_PATH 重建（夹具已指向 tmp 目录）。"""
    db1 = fresh_db.get_db()
    db1.connect()
    db1.init_schema()
    fresh_db.reset_db()
    db2 = fresh_db.get_db()
    assert db1 is not db2
    assert db2.path != Database("/var/lib/aiops/aiops.db").path or db2.path.endswith("test.db")
