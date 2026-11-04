"""Sentinel Router 主程序（design §1.2 / §4）。

阶段 1 范围（implement.md）：
- GET  /healthz                      存活检查
- POST /api/v1/alerts                告警入口 + 指纹降噪（design §3.2；分诊建事件属阶段 3）
- POST /api/v1/tasks/claim           任务认领 + 租约（design §4.3）
- POST /api/v1/tasks/{id}/result     任务结果回传（认领协议的另一半，可端到端测试）

安全边界（design §8）：仅绑定 127.0.0.1（见 deploy/systemd/aiops-router.service）；
SQLite 单写者 → 本进程是全系统唯一写库方，严禁多实例运行。

启动方式：
- VM（systemd）：ExecStart = /opt/aiops/venv/bin/python -m src.router.main
- 开发机手动：   python -m uvicorn src.router.main:app --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException

from ..common.audit import log_action
from ..common.config import AppConfig, load_config
from ..common.db import Database, get_db, utc_now_iso
from ..common.fingerprint import make_fingerprint
from ..common.log import setup_logging
from .schemas import (
    AlertIn,
    AlertOut,
    ClaimIn,
    ClaimOut,
    ClaimedTask,
    HealthOut,
    TaskResultIn,
    TaskResultOut,
)

logger = logging.getLogger("sentinel.router")

# 配置在进程启动时加载一次（改配置需 restart，见 systemd 单元注释）
config: AppConfig = load_config()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时连库建表，退出时关闭（FastAPI 生命周期钩子）。"""
    setup_logging()
    db = get_db()
    db.connect()
    db.init_schema()
    logger.info("router 启动完成 db=%s lease=%ss dedupe=%ss",
                db.path, config.router.lease_seconds, config.alert.dedupe_window_seconds)
    yield
    db.close()
    logger.info("router 已停止")


app = FastAPI(
    title="Sentinel Router",
    version="0.1.0",
    description="AIOps 智能运维 Agent · 调度中心（SQLite 单写者）",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# GET /healthz
# ---------------------------------------------------------------------------
@app.get("/healthz", response_model=HealthOut, tags=["system"])
async def healthz() -> HealthOut:
    return HealthOut(status="ok", service="sentinel-router", time=utc_now_iso())


# ---------------------------------------------------------------------------
# POST /api/v1/alerts —— 告警入口 + 指纹降噪
# ---------------------------------------------------------------------------
@app.post("/api/v1/alerts", response_model=AlertOut, tags=["alert"])
async def ingest_alert(alert_in: AlertIn) -> AlertOut:
    """降噪规则（design §3.2）：

    同指纹在 dedupe_window_seconds 窗口内 → 不新建告警，count+=1 并刷新
    last_seen_at；窗口滑过（距 last_seen 超窗）→ 视为新告警插入。

    TODO(阶段3): 新告警（deduped=False）在此触发分诊规则表 → 建 incident/task。
    """
    db: Database = get_db()
    fingerprint = make_fingerprint(alert_in.source, alert_in.subject, alert_in.error_class)
    now = utc_now_iso()
    window = config.alert.dedupe_window_seconds

    with db.lock:  # 查-改-写原子性：单写者锁
        row = db.query_one(
            "SELECT id, count, last_seen_at FROM alert "
            "WHERE fingerprint = ? AND status = 'firing' "
            "ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        )
        if row is not None and _within_window(row["last_seen_at"], now, window):
            count = int(row["count"]) + 1
            db.execute(
                "UPDATE alert SET count = ?, last_seen_at = ? WHERE id = ?",
                (count, now, row["id"]),
            )
            logger.info("告警降噪命中 fp=%s count=%s", fingerprint[:12], count)
            return AlertOut(alert_id=int(row["id"]), fingerprint=fingerprint,
                            count=count, deduped=True)

        cur = db.execute(
            "INSERT INTO alert (fingerprint, source, severity, subject, error_class, "
            "payload_json, count, first_seen_at, last_seen_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'firing')",
            (
                fingerprint,
                alert_in.source,
                alert_in.severity,
                alert_in.subject,
                alert_in.error_class,
                json.dumps(alert_in.payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        alert_id = int(cur.lastrowid or 0)

    logger.warning("新告警 id=%s fp=%s %s/%s/%s", alert_id, fingerprint[:12],
                   alert_in.source, alert_in.subject, alert_in.error_class)
    # TODO(阶段2): 接入飞书告警卡片发送（config.feishu.enabled 门控）
    return AlertOut(alert_id=alert_id, fingerprint=fingerprint, count=1, deduped=False)


# ---------------------------------------------------------------------------
# POST /api/v1/tasks/claim —— 任务认领
# ---------------------------------------------------------------------------
@app.post("/api/v1/tasks/claim", response_model=ClaimOut, tags=["task"])
async def claim_task(body: ClaimIn) -> ClaimOut:
    """FIFO 认领（design §4.3）：同类型最早 queued 任务 → running + 租约。

    - 无任务返回 task=null（Worker 每 2s 轮询）。
    - 同类型串行由「Worker 一次只处理一个任务」保证；租约过期重派在阶段 3
      由状态机 sweeper 接入（attempts 达 claim_max_attempts 后升级转人工）。
    """
    db: Database = get_db()
    now = utc_now_iso()
    lease_expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=config.router.lease_seconds)
    ).isoformat(timespec="seconds")

    with db.lock:
        row = db.query_one(
            "SELECT * FROM task WHERE type = ? AND status = 'queued' ORDER BY id LIMIT 1",
            (body.worker_type,),
        )
        if row is None:
            return ClaimOut(task=None)
        db.execute(
            "UPDATE task SET status = 'running', worker_id = ?, lease_expires_at = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (body.worker_id, lease_expires_at, now, row["id"]),
        )
        claimed = ClaimedTask(
            id=int(row["id"]),
            incident_id=int(row["incident_id"]),
            type=row["type"],
            payload=json.loads(row["payload_json"] or "{}"),
            attempts=int(row["attempts"]) + 1,
            lease_expires_at=lease_expires_at,
        )

    log_action(db, operator="agent", action="task_claim", incident_id=claimed.incident_id,
               task_id=claimed.id,
               detail={"worker_type": body.worker_type, "worker_id": body.worker_id})
    logger.info("任务派发 task=%s type=%s → %s", claimed.id, claimed.type, body.worker_id)
    return ClaimOut(task=claimed)


# ---------------------------------------------------------------------------
# POST /api/v1/tasks/{task_id}/result —— 结果回传
# ---------------------------------------------------------------------------
@app.post("/api/v1/tasks/{task_id}/result", response_model=TaskResultOut, tags=["task"])
async def submit_task_result(task_id: int, body: TaskResultIn) -> TaskResultOut:
    """回传结果（认领协议另一半）：

    - 404 任务不存在；409 状态非 running（含重复回传）；403 worker 不匹配。
    - TODO(阶段3): 按 result 驱动 incident 状态机
      （acting→verifying→resolved / escalated，design §2.6）。
    """
    db: Database = get_db()
    now = utc_now_iso()

    with db.lock:
        row = db.query_one("SELECT * FROM task WHERE id = ?", (task_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] != "running":
            raise HTTPException(status_code=409,
                                detail=f"task status is '{row['status']}', expect 'running'")
        if row["worker_id"] != body.worker_id:
            raise HTTPException(status_code=403, detail="worker_id mismatch")
        db.execute(
            "UPDATE task SET status = ?, result_json = ?, lease_expires_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (body.status, json.dumps(body.result, ensure_ascii=False), now, task_id),
        )
        incident_id = int(row["incident_id"])

    log_action(db, operator="agent", action=f"task_{body.status}",
               incident_id=incident_id, task_id=task_id,
               detail={"worker_id": body.worker_id})
    logger.info("任务回传 task=%s → %s", task_id, body.status)
    return TaskResultOut(task_id=task_id, status=body.status, accepted=True)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _within_window(last_seen_at: str, now_iso: str, window_seconds: int) -> bool:
    """last_seen_at 距 now 是否在降噪窗口内（容忍无时区的 naive 时间戳）。"""
    try:
        last = datetime.fromisoformat(last_seen_at)
        now = datetime.fromisoformat(now_iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() <= window_seconds
    except ValueError:
        return False  # 时间戳异常 → 按新告警处理，不吞告警


if __name__ == "__main__":  # python -m src.router.main（systemd 入口）
    import uvicorn

    setup_logging()
    uvicorn.run(app, host=config.router.host, port=config.router.port, log_level="info")
