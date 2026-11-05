"""Sentinel 运维控制台（只读 dashboard，演示用）。

设计取舍（2026-11-05 演示需求）：
- 只读：仅 SELECT + systemctl is-active（只读查询），不写库、不执行副作用
- 独立端口 0.0.0.0:18080（演示需要宿主机浏览器访问；仅暴露只读视图，
  生产部署可改绑 127.0.0.1 或加认证）
- 与 Router 同进程：复用 SQLite 单例（读走锁，不破坏单写者纪律）
- 页面 2s 轮询 5 个只读接口：overview / alerts / incidents / tasks / actions
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..common.db import Database, get_db

_STATIC_DIR = Path(__file__).parent / "static"
SERVICES = ("aiops-router", "aiops-monitor", "aiops-worker", "demo-app")

dashboard_app = FastAPI(title="Sentinel Dashboard", docs_url=None, redoc_url=None)


def _svc_state(unit: str) -> str:
    try:
        proc = subprocess.run(["systemctl", "is-active", "--quiet", f"{unit}.service"],
                              timeout=3)
        return "active" if proc.returncode == 0 else "inactive"
    except Exception:  # noqa: BLE001 —— 查询失败按未知
        return "unknown"


@dashboard_app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "dashboard.html")


@dashboard_app.get("/api/overview")
async def overview() -> dict:
    db: Database = get_db()
    services = {name: _svc_state(name) for name in SERVICES}
    counts = {
        "firing_alerts": db.query_one(
            "SELECT COUNT(*) AS c FROM alert WHERE status='firing'")["c"],
        "open_incidents": db.query_one(
            "SELECT COUNT(*) AS c FROM incident WHERE state NOT IN "
            "('resolved','escalated')")["c"],
        "queued_tasks": db.query_one(
            "SELECT COUNT(*) AS c FROM task WHERE status='queued'")["c"],
        "running_tasks": db.query_one(
            "SELECT COUNT(*) AS c FROM task WHERE status='running'")["c"],
        "awaiting_human": db.query_one(
            "SELECT COUNT(*) AS c FROM patch_proposal WHERE status='awaiting_confirm'")["c"],
    }
    workers = db.query_all(
        "SELECT type, COUNT(*) AS total, "
        "SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END) AS open "
        "FROM task GROUP BY type ORDER BY type")
    return {"services": services, "counts": counts,
            "worker_roles": [dict(r) for r in workers]}


@dashboard_app.get("/api/alerts")
async def alerts(limit: int = 20) -> dict:
    db: Database = get_db()
    rows = db.query_all(
        "SELECT id, source, severity, subject, error_class, count, status, "
        "first_seen_at, last_seen_at FROM alert ORDER BY id DESC LIMIT ?",
        (limit,))
    return {"alerts": [dict(r) for r in rows]}


@dashboard_app.get("/api/incidents")
async def incidents(limit: int = 20) -> dict:
    db: Database = get_db()
    rows = db.query_all(
        "SELECT i.id, i.title, i.classification, i.state, i.severity, "
        "i.escalation_reason, i.created_at, i.updated_at, a.subject AS subject "
        "FROM incident i LEFT JOIN alert a ON i.alert_id = a.id "
        "ORDER BY i.id DESC LIMIT ?", (limit,))
    return {"incidents": [dict(r) for r in rows]}


@dashboard_app.get("/api/tasks")
async def tasks(limit: int = 25) -> dict:
    db: Database = get_db()
    rows = db.query_all(
        "SELECT id, incident_id, type, status, worker_id, attempts, "
        "created_at, updated_at, substr(result_json, 1, 160) AS result_preview "
        "FROM task ORDER BY id DESC LIMIT ?", (limit,))
    return {"tasks": [dict(r) for r in rows]}


@dashboard_app.get("/api/actions")
async def actions(limit: int = 25) -> dict:
    db: Database = get_db()
    rows = db.query_all(
        "SELECT id, incident_id, task_id, operator, action, "
        "substr(detail_json, 1, 160) AS detail_preview, created_at "
        "FROM action_log ORDER BY id DESC LIMIT ?", (limit,))
    return {"actions": [dict(r) for r in rows]}
