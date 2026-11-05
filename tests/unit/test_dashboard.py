"""运维控制台 API 单测（只读 dashboard，design 演示扩展）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.common.db import utc_now_iso
from src.router.dashboard import dashboard_app


def _seed(db):
    now = utc_now_iso()
    db.execute(
        "INSERT INTO alert (fingerprint, source, severity, subject, error_class, "
        "payload_json, count, first_seen_at, last_seen_at, status) "
        "VALUES ('fp1', 'probe:process', 'critical', 'demo-app', 'process_dead', "
        "'{}', 1, ?, ?, 'firing')", (now, now))
    db.execute(
        "INSERT INTO incident (alert_id, title, classification, state, severity, "
        "created_at, updated_at) VALUES (1, 'demo-app · process_down', "
        "'process_down', 'acting', 'critical', ?, ?)", (now, now))
    db.execute(
        "INSERT INTO task (incident_id, type, status, payload_json, worker_id, "
        "created_at, updated_at) VALUES (1, 'ops', 'running', "
        "'{\"action\": \"restart_demo_app\"}', 'w-ops-1', ?, ?)", (now, now))
    db.execute(
        "INSERT INTO action_log (incident_id, task_id, operator, action, "
        "detail_json, created_at) VALUES (1, 1, 'agent', 'task_claim', "
        "'{\"worker_id\": \"w-ops-1\"}', ?)", (now,))


def test_dashboard_index_served(fresh_db):
    with TestClient(dashboard_app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Sentinel 运维控制台" in r.text


def test_overview_counts(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    _seed(db)
    with TestClient(dashboard_app) as client:
        data = client.get("/api/overview").json()
    assert data["counts"]["firing_alerts"] == 1
    assert data["counts"]["open_incidents"] == 1
    assert data["counts"]["running_tasks"] == 1
    assert data["counts"]["queued_tasks"] == 0
    # worker 角色负载统计
    roles = {w["type"]: w for w in data["worker_roles"]}
    assert roles["ops"]["total"] == 1


def test_alerts_incidents_tasks_actions(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    _seed(db)
    with TestClient(dashboard_app) as client:
        alerts = client.get("/api/alerts").json()["alerts"]
        incidents = client.get("/api/incidents").json()["incidents"]
        tasks = client.get("/api/tasks").json()["tasks"]
        actions = client.get("/api/actions").json()["actions"]
    assert alerts[0]["error_class"] == "process_dead" and alerts[0]["status"] == "firing"
    assert incidents[0]["state"] == "acting" and incidents[0]["subject"] == "demo-app"
    assert tasks[0]["type"] == "ops" and tasks[0]["worker_id"] == "w-ops-1"
    assert actions[0]["action"] == "task_claim"


def test_dashboard_is_read_only_endpoints(fresh_db):
    """只读：dashboard 不提供任何写接口（仅 GET）。"""
    with TestClient(dashboard_app) as client:
        paths = client.get("/openapi.json").json()["paths"]
    methods = {m for p in paths.values() for m in p}
    assert methods <= {"get"}
