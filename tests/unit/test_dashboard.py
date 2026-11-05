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


def test_dashboard_demo_page_served(fresh_db):
    with TestClient(dashboard_app) as client:
        r = client.get("/demo/")
        assert r.status_code == 200
        assert "演示控制台" in r.text


def test_demo_trigger_unknown_action_400(fresh_db):
    with TestClient(dashboard_app) as client:
        r = client.post("/api/demo/trigger", json={"action": "nonsense"})
        assert r.status_code == 400


def test_demo_trigger_whitelist_attack_inserts_task(fresh_db):
    """越权攻击测试：插入 queued 任务（Worker 侧三重门会拒绝）。"""
    db0 = fresh_db.get_db()
    db0.connect()
    db0.init_schema()
    with TestClient(dashboard_app) as client:
        r = client.post("/api/demo/trigger", json={"action": "whitelist_attack"})
        body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    db = fresh_db.get_db()
    task = db.query_one("SELECT type, status, payload_json FROM task "
                        "WHERE status='queued' ORDER BY id DESC LIMIT 1")
    assert task and "rm_rf_slash" in task["payload_json"]


def test_demo_trigger_kill_uses_whitelisted_commands(fresh_db, monkeypatch):
    """kill 场景：必须走 sudoers 白名单内的命令（kill/stop demo-app）。"""
    import src.router.dashboard as dash

    calls = []

    class FakeProc:
        returncode = 0

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(dash, "_run", fake_run)
    with TestClient(dashboard_app) as client:
        r = client.post("/api/demo/trigger", json={"action": "kill"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls[0] == ["sudo", "systemctl", "kill", "--signal=SIGKILL",
                        "demo-app.service"]
    assert calls[1] == ["sudo", "systemctl", "stop", "demo-app.service"]
