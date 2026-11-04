"""Router API 集成单测：healthz / 告警降噪 / 认领-回传全流程（含负路径）。

用 FastAPI TestClient 走完整 lifespan（真实建库），无外部依赖。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.common.db import utc_now_iso
from src.router.main import app


def test_healthz(fresh_db):
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "sentinel-router"


def test_alert_created_then_deduped(fresh_db):
    """同指纹第二次上报：不新建，count 递增（design §3.2 降噪）。"""
    body = {
        "source": "probe:http",
        "severity": "critical",
        "subject": "demo-app",
        "error_class": "http_5xx",
        "message": "health 连续 3 次失败",
        "payload": {"sample": "GET /health -> 503"},
    }
    with TestClient(app) as client:
        r1 = client.post("/api/v1/alerts", json=body)
        assert r1.status_code == 200
        j1 = r1.json()
        assert j1["deduped"] is False and j1["count"] == 1

        r2 = client.post("/api/v1/alerts", json=body)
        assert r2.status_code == 200
        j2 = r2.json()
        assert j2["deduped"] is True
        assert j2["count"] == 2
        assert j2["alert_id"] == j1["alert_id"]
        assert j2["fingerprint"] == j1["fingerprint"]

        # 库内验证：仍只有一行 alert
        db = fresh_db.get_db()
        assert db.query_one("SELECT COUNT(*) AS c FROM alert")["c"] == 1


def test_alert_different_fingerprint_creates_new_row(fresh_db):
    with TestClient(app) as client:
        common = {"severity": "critical", "subject": "demo-app"}
        client.post("/api/v1/alerts", json={**common, "source": "probe:http", "error_class": "http_5xx"})
        client.post("/api/v1/alerts", json={**common, "source": "probe:process", "error_class": "process_dead"})
        db = fresh_db.get_db()
        assert db.query_one("SELECT COUNT(*) AS c FROM alert")["c"] == 2


def test_alert_validation_rejects_bad_severity(fresh_db):
    with TestClient(app) as client:
        r = client.post("/api/v1/alerts", json={
            "source": "probe:http", "severity": "fatal", "subject": "demo-app",
        })
        assert r.status_code == 422  # Pydantic Literal 校验


def _seed_queued_task(db):
    now = utc_now_iso()
    db.execute(
        "INSERT INTO incident (title, classification, state, severity, created_at, updated_at) "
        "VALUES ('演示宕机', 'process_down', 'open', 'critical', ?, ?)",
        (now, now),
    )
    cur = db.execute(
        "INSERT INTO task (incident_id, type, status, payload_json, created_at, updated_at) "
        "VALUES (1, 'ops', 'queued', '{\"action\": \"restart_demo_app\"}', ?, ?)",
        (now, now),
    )
    return int(cur.lastrowid)


def test_claim_fifo_and_lease(fresh_db):
    with TestClient(app) as client:
        db = fresh_db.get_db()
        task_id = _seed_queued_task(db)

        r = client.post("/api/v1/tasks/claim",
                        json={"worker_type": "ops", "worker_id": "worker-1"})
        assert r.status_code == 200
        task = r.json()["task"]
        assert task is not None
        assert task["id"] == task_id
        assert task["type"] == "ops"
        assert task["attempts"] == 1
        assert task["payload"]["action"] == "restart_demo_app"
        assert task["lease_expires_at"]  # 租约已设置

        # 认领后库里状态与租约落库
        row = db.query_one("SELECT status, worker_id, lease_expires_at FROM task WHERE id = ?", (task_id,))
        assert row["status"] == "running"
        assert row["worker_id"] == "worker-1"
        assert row["lease_expires_at"] == task["lease_expires_at"]

        # 无更多任务 → task=None（Worker 继续轮询）
        r2 = client.post("/api/v1/tasks/claim",
                         json={"worker_type": "ops", "worker_id": "worker-2"})
        assert r2.json()["task"] is None

        # 审计已写 task_claim
        assert db.query_one(
            "SELECT COUNT(*) AS c FROM action_log WHERE action='task_claim'"
        )["c"] == 1


def test_result_flow_with_negative_paths(fresh_db):
    with TestClient(app) as client:
        db = fresh_db.get_db()
        task_id = _seed_queued_task(db)
        client.post("/api/v1/tasks/claim", json={"worker_type": "ops", "worker_id": "worker-1"})

        url = f"/api/v1/tasks/{task_id}/result"
        # 403：不是认领者回传
        assert client.post(url, json={"worker_id": "other", "status": "succeeded"}).status_code == 403
        # 成功回传
        ok = client.post(url, json={"worker_id": "worker-1", "status": "succeeded",
                                    "result": {"verify": "3/3 passed"}})
        assert ok.status_code == 200
        # 409：重复回传（已不是 running）
        assert client.post(url, json={"worker_id": "worker-1", "status": "succeeded"}).status_code == 409
        # 404：任务不存在
        assert client.post("/api/v1/tasks/999/result",
                           json={"worker_id": "worker-1", "status": "failed"}).status_code == 404

        row = db.query_one("SELECT status, result_json, lease_expires_at FROM task WHERE id = ?", (task_id,))
        assert row["status"] == "succeeded"
        assert "verify" in row["result_json"]
        assert row["lease_expires_at"] is None  # 终态任务租约清空
        # 审计已写 task_succeeded
        assert db.query_one(
            "SELECT COUNT(*) AS c FROM action_log WHERE action='task_succeeded'"
        )["c"] == 1


def test_result_on_queued_task_conflict(fresh_db):
    """未认领（仍 queued）直接回传 → 409。"""
    with TestClient(app) as client:
        db = fresh_db.get_db()
        task_id = _seed_queued_task(db)
        r = client.post(f"/api/v1/tasks/{task_id}/result",
                        json={"worker_id": "worker-1", "status": "succeeded"})
        assert r.status_code == 409
