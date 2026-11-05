"""Sentinel 运维控制台（只读 dashboard，演示用）。

设计取舍（2026-11-05 演示需求）：
- 只读：仅 SELECT + systemctl is-active（只读查询），不写库、不执行副作用
- 独立端口 0.0.0.0:18080（演示需要宿主机浏览器访问；仅暴露只读视图，
  生产部署可改绑 127.0.0.1 或加认证）
- 与 Router 同进程：复用 SQLite 单例（读走锁，不破坏单写者纪律）
- 页面 2s 轮询 5 个只读接口：overview / alerts / incidents / tasks / actions
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..common.db import Database, get_db, utc_now_iso

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


# ---------------------------------------------------------------------------
# 演示控制台（/demo/）：一键注入全部异常（演示专用写接口）
# 安全说明：仅用于单机演示环境；kill/stop/restart 走 sudoers 白名单，
# 越权动作测试仅插入任务（由 Worker 侧三重门拒绝），reset 走 v0.1-base 基线。
# ---------------------------------------------------------------------------
class DemoTriggerIn(BaseModel):
    action: str = Field(description="kill|dirty_data|flood|whitelist_attack|reset")


def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


async def _flood(duration: int = 25, concurrency: int = 50) -> None:
    """后台压测 /api/search（与 chaos_flood.sh 等价）。"""
    loop = asyncio.get_event_loop()
    end = loop.time() + duration
    async with httpx.AsyncClient(timeout=3) as client:
        async def worker() -> None:
            while loop.time() < end:
                try:
                    await client.get("http://127.0.0.1:18000/api/search?q=flood")
                except Exception:  # noqa: BLE001 —— 压测中错误忽略
                    pass
        await asyncio.gather(*[worker() for _ in range(concurrency)])


@dashboard_app.get("/demo/", include_in_schema=False)
async def demo_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "demo.html")


@dashboard_app.post("/api/demo/trigger")
async def demo_trigger(body: DemoTriggerIn) -> dict:
    action = body.action.strip().lower()

    if action == "kill":
        # 场景一：SIGKILL + stop（保持 down，等 Agent 自愈）
        await asyncio.to_thread(_run, ["sudo", "systemctl", "kill", "--signal=SIGKILL",
                                       "demo-app.service"], 15)
        p = await asyncio.to_thread(_run, ["sudo", "systemctl", "stop",
                                           "demo-app.service"], 15)
        return {"ok": p.returncode == 0,
                "message": "已注入宕机（SIGKILL+stop）。预期：≤15s 告警 → OpsWorker 自动重启 → 复检 → 绿色结果卡"}

    if action == "dirty_data":
        # 场景二：脏数据 ×10（触发 500 → ValueError → LLM 根因 → 修复 PR）
        ok_cnt = err_cnt = 0
        async with httpx.AsyncClient(timeout=5) as client:
            for i in range(10):
                try:
                    resp = await client.post(
                        "http://127.0.0.1:18000/api/orders",
                        json={"item": f"bad-{i}", "amount": "abc", "price": 2})
                    err_cnt += 1 if resp.status_code >= 500 else 0
                except Exception:  # noqa: BLE001
                    pass
            for i in range(3):
                try:
                    await client.post("http://127.0.0.1:18000/api/orders",
                                      json={"item": f"ok-{i}", "amount": 3,
                                            "price": 2})
                    ok_cnt += 1
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": err_cnt > 0,
                "message": f"已注入脏数据（500×{err_cnt}，正常×{ok_cnt}）。"
                           "预期：告警卡 → 根因卡 → PR 卡 → 去 GitHub 合并 → 自动部署复测"}

    if action == "flood":
        asyncio.create_task(_flood(25, 50))
        return {"ok": True,
                "message": "已启动 25 秒 × 50 并发压测。预期：QPS 告警 → Router 派发限流任务 → 防护状态生效 → 到期自动解除"}

    if action == "whitelist_attack":
        # 安全负测试：插入越权任务（rm_rf_slash），验证三重门拒绝 + 审计
        db: Database = get_db()
        now = utc_now_iso()
        with db.lock:
            cur = db.execute(
                "INSERT INTO incident (title, classification, state, severity, "
                "created_at, updated_at) VALUES ('白名单攻击测试', 'manual', 'acting', "
                "'info', ?, ?)", (now, now))
            incident_id = int(cur.lastrowid or 0)
            cur = db.execute(
                "INSERT INTO task (incident_id, type, status, payload_json, "
                "created_at, updated_at) VALUES (?, 'ops', 'queued', "
                "'{\"action\": \"rm_rf_slash\", \"params\": {}}', ?, ?)",
                (incident_id, now, now))
            task_id = int(cur.lastrowid or 0)
        return {"ok": True,
                "message": f"已注入越权任务 task#{task_id}（action=rm_rf_slash）。"
                           "预期：Worker 白名单拒绝 → whitelist_deny 审计 → 转人工卡片"}

    if action == "reset":
        # 重置演示基线：deploy 克隆回到 v0.1-base（含缺陷）+ 重启
        cmds = [
            ["git", "-C", "/opt/aiops/deploy", "fetch", "--tags", "--force"],
            ["git", "-C", "/opt/aiops/deploy", "reset", "--hard", "v0.1-base"],
            ["sudo", "systemctl", "restart", "demo-app.service"],
        ]
        outputs = []
        for cmd in cmds:
            proc = await asyncio.to_thread(_run, cmd, 60)
            outputs.append(proc.returncode)
        ok = all(code == 0 for code in outputs)
        return {"ok": ok,
                "message": "已重置到演示基线 v0.1-base（缺陷已还原）并重启 demo-app"
                           if ok else f"重置失败 exit={outputs}"}

    raise HTTPException(400, f"未知演示动作: {body.action}")
