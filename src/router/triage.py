"""Router 分诊规则 + 事件状态机（design §4.1 / §2.6）。

分诊：新告警（未被降噪）→ classification → playbook → incident/task。
状态机：task 结果回传 → incident 迁移（resolved / escalated）。

本阶段（3）落地：
- process_down          → ops restart_demo_app（复检由 Worker 内置）
- traffic_anomaly       → ops set_rate_limit（无复检配置，执行成功即 resolved）
- app_error             → 仅建 incident（诊断接线在阶段 4）
- flapping 保护（§4.4）  → 15min 内同 subject 第 3 次 process_down：不派发，直接转人工
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ..common.db import Database, utc_now_iso

logger = logging.getLogger("sentinel.router.triage")

FLAPPING_WINDOW_SECONDS = 900   # 15 分钟
FLAPPING_THRESHOLD = 2          # 已有 2 个 → 即将创建第 3 个即触发

# 日志探针等 app_error 类 incident 的状态说明（阶段 4 接 DiagWorker 后派发诊断任务）
_PENDING_DIAG = {"note": "app_error 已记录，诊断任务接线在阶段 4"}


def classify(source: str, error_class: str | None) -> tuple[str | None, str | None]:
    """告警特征 → (classification, ops action)；action=None 表示不自动处置。"""
    if source in ("probe:process", "probe:http"):
        return "process_down", "restart_demo_app"
    if source == "probe:traffic" and error_class == "qps_over":
        return "traffic_anomaly", "set_rate_limit"
    if source == "probe:log":
        return "app_error", None
    if source == "webhook":
        return "app_error", None
    if source == "manual":
        return "manual", None
    return None, None


def _recent_incident_count(db: Database, subject: str, classification: str) -> int:
    """近 15min 同 subject + classification 的 incident 数（flapping 判定用）。"""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=FLAPPING_WINDOW_SECONDS)).isoformat(timespec="seconds")
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM incident i JOIN alert a ON i.alert_id = a.id "
        "WHERE i.classification = ? AND a.subject = ? AND i.created_at >= ?",
        (classification, subject, cutoff),
    )
    return int(row["c"]) if row else 0


def triage_new_alert(db: Database, *, alert_id: int, source: str, severity: str,
                     subject: str, error_class: str | None,
                     message: str | None = None,
                     diag_enabled: bool = False) -> dict:
    """对一条新告警分诊并落库（调用方须持有 db.lock）。

    返回：{"incident_id", "classification", "task_id"?, "escalated", "reason"?}
    注意：仅 deduped=False 的新告警才会走到这里（降噪窗口保证 5min 内不重复）。
    """
    classification, action = classify(source, error_class)
    now = utc_now_iso()

    # 1) 分类兜底：未知特征只记录不处置
    if classification is None:
        cur = db.execute(
            "INSERT INTO incident (fingerprint, alert_id, title, classification, state, "
            "severity, created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
            (None, alert_id, f"{subject} 未知异常（仅记录）", "unknown", severity, now, now),
        )
        return {"incident_id": int(cur.lastrowid or 0), "classification": "unknown",
                "escalated": False}

    # 2) flapping 保护：process_down 短时间反复 → 停止自动重启，直接转人工
    if classification == "process_down" and \
            _recent_incident_count(db, subject, classification) >= FLAPPING_THRESHOLD:
        reason = (f"{subject} 近 {FLAPPING_WINDOW_SECONDS // 60} 分钟第 3 次宕机，"
                  "疑似抖动/深层故障，已停止自动重启")
        cur = db.execute(
            "INSERT INTO incident (fingerprint, alert_id, title, classification, state, "
            "severity, escalation_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'escalated', ?, ?, ?, ?)",
            (None, alert_id, f"{subject} 反复宕机（flapping）", classification,
             severity, reason, now, now),
        )
        incident_id = int(cur.lastrowid or 0)
        logger.warning("flapping 保护触发 incident=%s %s", incident_id, reason)
        return {"incident_id": incident_id, "classification": classification,
                "escalated": True, "reason": reason}

    # 3) app_error：diag 开启 → 派发诊断任务（incident=diagnosing）
    if classification == "app_error" and diag_enabled:
        evidence: list = []
        alert_row = db.query_one("SELECT payload_json FROM alert WHERE id = ?",
                                 (alert_id,))
        try:
            evidence = (json.loads(alert_row["payload_json"] or "{}").get("evidence")
                        or []) if alert_row else []
        except (json.JSONDecodeError, TypeError):
            evidence = []
        cur = db.execute(
            "INSERT INTO incident (fingerprint, alert_id, title, classification, state, "
            "severity, created_at, updated_at) VALUES (?, ?, ?, ?, 'diagnosing', ?, ?, ?)",
            (None, alert_id, f"{subject} · {classification}", classification,
             severity, now, now),
        )
        incident_id = int(cur.lastrowid or 0)
        diag_payload = {
            "alert_id": alert_id, "incident_id": incident_id, "subject": subject,
            "error_class": error_class, "message": message, "evidence": evidence,
        }
        cur = db.execute(
            "INSERT INTO task (incident_id, type, status, payload_json, created_at, "
            "updated_at) VALUES (?, 'diag', 'queued', ?, ?, ?)",
            (incident_id, json.dumps(diag_payload, ensure_ascii=False), now, now),
        )
        task_id = int(cur.lastrowid or 0)
        logger.info("incident=%s 分诊=app_error 派发诊断 task=%s", incident_id, task_id)
        return {"incident_id": incident_id, "classification": classification,
                "task_id": task_id, "escalated": False}

    # 4) 正常分诊：建 incident（处置类进 acting）
    incident_state = "acting" if action else "open"
    cur = db.execute(
        "INSERT INTO incident (fingerprint, alert_id, title, classification, state, "
        "severity, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (None, alert_id, f"{subject} · {classification}", classification,
         incident_state, severity, now, now),
    )
    incident_id = int(cur.lastrowid or 0)

    if action is None:
        logger.info("incident=%s 分诊=%s（无自动动作，%s）",
                    incident_id, classification, _PENDING_DIAG["note"])
        return {"incident_id": incident_id, "classification": classification,
                "escalated": False, "note": _PENDING_DIAG["note"]}

    # 4) 派发 ops 任务（单类型串行由 Worker 一次认领一个保证）
    import json as _json

    cur = db.execute(
        "INSERT INTO task (incident_id, type, status, payload_json, created_at, updated_at) "
        "VALUES (?, 'ops', 'queued', ?, ?, ?)",
        (incident_id, _json.dumps({"action": action, "params": {}}, ensure_ascii=False),
         now, now),
    )
    task_id = int(cur.lastrowid or 0)
    logger.info("incident=%s 分诊=%s 派发 task=%s action=%s",
                incident_id, classification, task_id, action)
    return {"incident_id": incident_id, "classification": classification,
            "task_id": task_id, "escalated": False}


def apply_task_result(db: Database, *, incident_id: int, task_type: str,
                      status: str, result: dict) -> dict:
    """任务结果 → incident 状态迁移（design §2.6）。返回迁移摘要供卡片渲染。"""
    now = utc_now_iso()
    if status == "succeeded":
        db.execute(
            "UPDATE incident SET state = 'resolved', resolved_at = ?, updated_at = ? "
            "WHERE id = ? AND state NOT IN ('resolved', 'escalated')",
            (now, now, incident_id),
        )
        logger.info("incident=%s → resolved（task %s succeeded）", incident_id, task_type)
        return {"state": "resolved"}

    reason = "自动处置失败"
    verify = (result or {}).get("verify") or {}
    if verify and not verify.get("passed", True):
        reason = f"复检未通过（尝试 {verify.get('attempts', '?')} 次）"
    elif (result or {}).get("exit_code") not in (None, 0):
        reason = f"动作执行失败（exit={result.get('exit_code')}）"
    db.execute(
        "UPDATE incident SET state = 'escalated', escalation_reason = ?, updated_at = ? "
        "WHERE id = ? AND state NOT IN ('resolved', 'escalated')",
        (reason, now, incident_id),
    )
    logger.warning("incident=%s → escalated（%s）", incident_id, reason)
    return {"state": "escalated", "reason": reason}


def incident_subject(db: Database, incident_id: int) -> str:
    """incident → alert.subject（卡片渲染用）。"""
    row = db.query_one(
        "SELECT a.subject AS subject FROM incident i JOIN alert a ON i.alert_id = a.id "
        "WHERE i.id = ?", (incident_id,))
    return row["subject"] if row else "unknown"
