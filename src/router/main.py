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
- 开发机手动：   python -m uvicorn src.router.main:app --host 127.0.0.1 --port 18081
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request

from ..common.audit import log_action
from ..common.config import AppConfig, load_config
from ..common.db import Database, get_db, utc_now_iso
from ..common.fingerprint import make_fingerprint
from ..common.log import setup_logging
from ..feishu.cards import (alert_card, diag_card, escalation_card,
                            pr_review_card, result_card)
from ..feishu.sender import get_sender
from . import triage
from .schemas import (
    AlertIn,
    AlertOut,
    AlertResolveIn,
    AlertResolveOut,
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
    app.state.feishu = get_sender(config.feishu.enabled)
    watch_task = asyncio.create_task(_pr_watch_loop())  # 阶段 5：PR 合并轮询
    logger.info("router 启动完成 db=%s lease=%ss dedupe=%ss feishu=%s",
                db.path, config.router.lease_seconds,
                config.alert.dedupe_window_seconds,
                "on" if app.state.feishu else "off")
    yield
    watch_task.cancel()
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
async def ingest_alert(alert_in: AlertIn, request: Request) -> AlertOut:
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
    # 飞书告警卡片（config.feishu.enabled 门控；未配置时降级为仅日志）
    sender = getattr(request.app.state, "feishu", None)
    if sender is not None:
        card = alert_card(subject=alert_in.subject, severity=alert_in.severity,
                          error_class=alert_in.error_class, message=alert_in.message
                          or alert_in.error_class or "异常",
                          count=1, alert_id=alert_id,
                          evidence=(alert_in.payload or {}).get("evidence"))
        asyncio.create_task(sender.send(card))
    else:
        logger.info("飞书未启用，告警 id=%s 仅落库", alert_id)

    # 阶段 3：分诊（仅新告警）→ incident/task；flapping 直接转人工
    with db.lock:
        triage_info = triage.triage_new_alert(
            db, alert_id=alert_id, source=alert_in.source,
            severity=alert_in.severity, subject=alert_in.subject,
            error_class=alert_in.error_class, message=alert_in.message,
            diag_enabled=config.worker.diag_enabled)
    if triage_info.get("escalated") and sender is not None:
        asyncio.create_task(sender.send(escalation_card(
            incident_id=triage_info["incident_id"], subject=alert_in.subject,
            reason=triage_info["reason"],
            attempted="flapping 保护：跳过自动重启")))
    return AlertOut(alert_id=alert_id, fingerprint=fingerprint, count=1, deduped=False)


# ---------------------------------------------------------------------------
# POST /api/v1/alerts/resolve —— 探针恢复上报（design §3.3）
# ---------------------------------------------------------------------------
@app.post("/api/v1/alerts/resolve", response_model=AlertResolveOut, tags=["alert"])
async def resolve_alert(body: AlertResolveIn) -> AlertResolveOut:
    """按指纹关闭 firing 告警；事件侧的 resolved 仍以任务复检/人工操作为准（§3.3）。"""
    db: Database = get_db()
    with db.lock:
        cur = db.execute(
            "UPDATE alert SET status = 'resolved' WHERE fingerprint = ? AND status = 'firing'",
            (body.fingerprint,),
        )
        resolved = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if resolved:
        logger.info("告警恢复 fp=%s 关闭 %d 条（%s）", body.fingerprint[:12], resolved,
                    body.reason)
    return AlertResolveOut(fingerprint=body.fingerprint, resolved_count=resolved)


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
async def submit_task_result(task_id: int, body: TaskResultIn, request: Request) -> TaskResultOut:
    """回传结果（认领协议另一半）：

    - 404 任务不存在；409 状态非 running（含重复回传）；403 worker 不匹配。
    - 阶段 3：驱动 incident 状态机（resolved / escalated，design §2.6）
      并发结果 / 升级卡片。
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
        task_type = row["type"]
        subject = triage.incident_subject(db, incident_id)
        task_payload = json.loads(row["payload_json"] or "{}")
        action = task_payload.get("action")
        denied = bool(body.result.get("denied"))

        if task_type == "diag" and body.status == "succeeded" and not denied:
            # 诊断成功：出根因卡；需要改码 + fix 开启 → 派发修复任务（单飞）
            outcome = {"state": "diagnosed"}
            if config.worker.fix_enabled and body.result.get("need_code_fix"):
                _dispatch_fix_task(db, incident_id=incident_id,
                                   diag_payload=task_payload,
                                   diag_result=body.result, subject=subject)
        elif task_type == "fix" and body.status == "succeeded" and not denied:
            # 修复成功：登记 patch_proposal → awaiting_human（等人工 GitHub 合并）
            now2 = utc_now_iso()
            db.execute(
                "INSERT INTO patch_proposal (incident_id, branch, pr_number, pr_url, "
                "root_cause, diff_stat, test_summary, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_confirm', ?, ?)",
                (incident_id, body.result.get("branch"),
                 body.result.get("pr_number"), body.result.get("pr_url"),
                 body.result.get("root_cause"), body.result.get("diff_stat"),
                 body.result.get("test_summary"), now2, now2))
            db.execute(
                "UPDATE incident SET state = 'awaiting_human', updated_at = ? WHERE id = ?",
                (now2, incident_id))
            outcome = {"state": "awaiting_human"}
        elif task_type == "ops" and action == "deploy_demo_app" \
                and body.status == "succeeded" and not denied:
            # 合并部署成功：锁外做复测重放，再迁移 incident 终态
            outcome = {"state": "deploy_pending_replay"}
        else:
            outcome = triage.apply_task_result(
                db, incident_id=incident_id, task_type=task_type,
                status=body.status, result=body.result)

    # 阶段 5：合并部署后复测（重放触发样本，期望非 5xx）
    if outcome["state"] == "deploy_pending_replay":
        replay_ok = await _replay_sample(task_payload.get("sample"))
        with db.lock:
            now3 = utc_now_iso()
            if replay_ok:
                db.execute(
                    "UPDATE patch_proposal SET status = 'merged', updated_at = ? "
                    "WHERE incident_id = ? AND status IN ('awaiting_confirm', 'deploying')",
                    (now3, incident_id))
                db.execute(
                    "UPDATE incident SET state = 'resolved', resolved_at = ?, "
                    "updated_at = ? WHERE id = ?", (now3, now3, incident_id))
                outcome = {"state": "resolved"}
            else:
                db.execute(
                    "UPDATE patch_proposal SET status = 'deploy_failed', updated_at = ? "
                    "WHERE incident_id = ?", (now3, incident_id))
                db.execute(
                    "UPDATE incident SET state = 'escalated', "
                    "escalation_reason = '合并部署后复测失败', updated_at = ? WHERE id = ?",
                    (now3, incident_id))
                outcome = {"state": "escalated", "reason": "合并部署后复测失败"}

    denied = bool(body.result.get("denied"))
    detail: dict = {"worker_id": body.worker_id, "task_type": task_type}
    for key in ("action", "kind", "command", "exit_code", "status_code",
                "verify", "duration_s", "output_tail", "error"):
        if key in body.result:
            detail[key] = body.result[key]
    log_action(db, operator="agent",
               action="whitelist_deny" if denied else f"task_{body.status}",
               incident_id=incident_id, task_id=task_id, detail=detail)

    sender = getattr(request.app.state, "feishu", None)
    if sender is not None:
        if outcome["state"] == "diagnosed":
            card = diag_card(
                incident_id=incident_id,
                root_cause=str(body.result.get("root_cause") or "（未给出）"),
                suspect_files=body.result.get("suspect_files") or [],
                evidence=body.result.get("evidence") or [],
                confidence=float(body.result.get("confidence") or 0),
                need_code_fix=bool(body.result.get("need_code_fix")))
        elif outcome["state"] == "awaiting_human":
            card = pr_review_card(
                incident_id=incident_id,
                pr_number=int(body.result.get("pr_number") or 0),
                pr_url=str(body.result.get("pr_url") or ""),
                root_cause=str(body.result.get("root_cause") or ""),
                diff_stat=str(body.result.get("diff_stat") or ""),
                test_summary=str(body.result.get("test_summary") or ""),
                branch=str(body.result.get("branch") or ""))
        elif outcome["state"] == "resolved":
            verify = body.result.get("verify") or {}
            card = result_card(
                incident_id=incident_id, subject=subject,
                action=str(body.result.get("action") or task_type),
                outcome="成功",
                detail=(f"复检 {verify.get('attempts', 0)} 次通过"
                        if verify.get("attempts") else "执行成功（无复检配置）"),
                cost_seconds=body.result.get("duration_s"))
        else:
            card = escalation_card(
                incident_id=incident_id, subject=subject,
                reason=outcome.get("reason", "自动处置失败"),
                attempted=str(body.result.get("action") or task_type))
        asyncio.create_task(sender.send(card))

    logger.info("任务回传 task=%s → %s incident=%s→%s",
                task_id, body.status, incident_id, outcome["state"])
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


# ---------------------------------------------------------------------------
# 阶段 5：修复闭环辅助（diag → fix → PR → 合并轮询 → deploy → 复测）
# ---------------------------------------------------------------------------
def _extract_sample(evidence: list[str]) -> dict | None:
    """从证据行（JSON 日志）提取触发样本，修复合并后复测重放用。"""
    for line in evidence or []:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("body"), dict):
            return {"method": "POST", "path": "/api/orders", "json": obj["body"]}
    return None


def _pick_sample(db: Database, incident_id: int) -> dict | None:
    """incident → alert.payload_json.evidence → 触发样本。"""
    row = db.query_one(
        "SELECT a.payload_json AS p FROM incident i JOIN alert a ON i.alert_id = a.id "
        "WHERE i.id = ?", (incident_id,))
    if not row:
        return None
    try:
        evidence = json.loads(row["p"] or "{}").get("evidence") or []
    except (json.JSONDecodeError, TypeError):
        return None
    return _extract_sample(evidence)


def _dispatch_fix_task(db: Database, *, incident_id: int, diag_payload: dict,
                       diag_result: dict, subject: str) -> int | None:
    """diag 成功且需要改码 → 单飞派发 fix 任务（调用方持有 db.lock）。"""
    existing = db.query_one(
        "SELECT id FROM task WHERE incident_id = ? AND type = 'fix' "
        "AND status IN ('queued', 'running')", (incident_id,))
    if existing:
        logger.info("incident=%s 已有在途 fix 任务（单飞跳过）", incident_id)
        return None
    payload = {
        "incident_id": incident_id,
        "subject": subject,
        "error_class": diag_payload.get("error_class"),
        "root_cause": diag_result.get("root_cause"),
        "suspect_files": diag_result.get("suspect_files") or [],
        "sample": _extract_sample(diag_payload.get("evidence") or []),
    }
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO task (incident_id, type, status, payload_json, created_at, updated_at) "
        "VALUES (?, 'fix', 'queued', ?, ?, ?)",
        (incident_id, json.dumps(payload, ensure_ascii=False), now, now))
    db.execute("UPDATE incident SET state = 'acting', updated_at = ? WHERE id = ?",
               (now, incident_id))
    task_id = int(cur.lastrowid or 0)
    logger.info("incident=%s 派发修复 task=%s", incident_id, task_id)
    return task_id


async def _replay_sample(sample: dict | None) -> bool:
    """合并后复测：重放触发样本，期望非 5xx（修复后应为 400 校验拒绝）。"""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            if not sample:
                resp = await client.get("http://127.0.0.1:18000/health")
                return resp.status_code == 200
            url = f"http://127.0.0.1:18000{sample.get('path', '/api/orders')}"
            resp = await client.request(sample.get("method", "POST"), url,
                                        json=sample.get("json"))
        ok = resp.status_code < 500
        logger.info("复测重放 %s → HTTP %s（%s）", url, resp.status_code,
                    "通过" if ok else "失败")
        return ok
    except Exception:  # noqa: BLE001
        logger.exception("复测重放异常")
        return False


async def _pr_watch_once() -> None:
    """轮询 awaiting_confirm 的 PR：merged → 派 deploy 任务；closed → 升级转人工。"""
    db = get_db()
    rows = db.query_all(
        "SELECT * FROM patch_proposal WHERE status = 'awaiting_confirm'")
    if not rows:
        return
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "machinetus/aiops-sentinel")
    for row in rows:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/pulls/{row['pr_number']}",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"})
            if resp.status_code != 200:
                logger.warning("PR #%s 查询失败 HTTP %s", row["pr_number"],
                               resp.status_code)
                continue
            pr = resp.json()
            now = utc_now_iso()
            if pr.get("merged"):
                with db.lock:
                    exists = db.query_one(
                        "SELECT id FROM task WHERE incident_id = ? AND type = 'ops' "
                        "AND status IN ('queued', 'running') "
                        "AND payload_json LIKE '%deploy_demo_app%'",
                        (row["incident_id"],))
                    if exists:
                        continue
                    sample = _pick_sample(db, row["incident_id"])
                    db.execute(
                        "INSERT INTO task (incident_id, type, status, payload_json, "
                        "created_at, updated_at) VALUES (?, 'ops', 'queued', ?, ?, ?)",
                        (row["incident_id"],
                         json.dumps({"action": "deploy_demo_app",
                                     "params": {}, "patch_id": row["id"],
                                     "sample": sample}, ensure_ascii=False),
                         now, now))
                    db.execute(
                        "UPDATE patch_proposal SET status = 'deploying', updated_at = ? "
                        "WHERE id = ?", (now, row["id"]))
                logger.info("PR #%s 已合并 → 派发 deploy 任务", row["pr_number"])
            elif pr.get("state") == "closed":
                with db.lock:
                    db.execute(
                        "UPDATE patch_proposal SET status = 'rejected', updated_at = ? "
                        "WHERE id = ?", (now, row["id"]))
                    db.execute(
                        "UPDATE incident SET state = 'escalated', "
                        "escalation_reason = 'PR 被关闭/拒绝', updated_at = ? "
                        "WHERE id = ?", (now, row["incident_id"]))
                logger.warning("PR #%s 被关闭 → incident=%s 升级转人工",
                               row["pr_number"], row["incident_id"])
                sender = getattr(app.state, "feishu", None)
                if sender is not None:
                    asyncio.create_task(sender.send(escalation_card(
                        incident_id=int(row["incident_id"]), subject="demo-app",
                        reason="修复 PR 被拒绝/关闭", attempted="人工拒绝")))
        except Exception:  # noqa: BLE001 —— 单条失败不影响其他 PR
            logger.exception("PR 轮询异常 #%s", row["pr_number"])


async def _pr_watch_loop() -> None:
    while True:
        try:
            await _pr_watch_once()
        except Exception:  # noqa: BLE001
            logger.exception("PR 轮询循环异常")
        await asyncio.sleep(30)


if __name__ == "__main__":  # python -m src.router.main（systemd 入口）
    import uvicorn

    setup_logging()
    uvicorn.run(app, host=config.router.host, port=config.router.port, log_level="info")
