"""分诊规则 + 事件状态机单测（design §4.1 / §2.6 / §4.4）。"""

from __future__ import annotations

import json

from src.common.db import utc_now_iso
from src.router import triage


def _insert_alert(db, *, subject="demo-app", source="probe:process",
                  error_class="process_dead", severity="critical") -> int:
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO alert (fingerprint, source, severity, subject, error_class, "
        "payload_json, count, first_seen_at, last_seen_at, status) "
        "VALUES (?, ?, ?, ?, ?, '{}', 1, ?, ?, 'firing')",
        (f"fp-{subject}-{source}-{error_class}-{now}", source, severity,
         subject, error_class, now, now),
    )
    return int(cur.lastrowid or 0)


def _triage(db, **kw):
    defaults = dict(source="probe:process", severity="critical",
                    subject="demo-app", error_class="process_dead")
    defaults.update(kw)
    alert_id = _insert_alert(db, subject=defaults["subject"],
                             source=defaults["source"],
                             error_class=defaults["error_class"],
                             severity=defaults["severity"])
    return triage.triage_new_alert(db, alert_id=alert_id, **defaults)


def test_classify_rules():
    assert triage.classify("probe:process", "process_dead") == \
        ("process_down", "restart_demo_app")
    assert triage.classify("probe:http", "http_unreachable") == \
        ("process_down", "restart_demo_app")
    assert triage.classify("probe:traffic", "qps_over") == \
        ("traffic_anomaly", "set_rate_limit")
    assert triage.classify("probe:log", "ValueError") == ("app_error", None)
    assert triage.classify("manual", None) == ("manual", None)
    assert triage.classify("what:ever", None) == (None, None)


def test_triage_process_down_creates_incident_and_task(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    info = _triage(db)
    assert info["classification"] == "process_down"
    assert info["escalated"] is False
    inc = db.query_one("SELECT state, classification FROM incident WHERE id=?",
                       (info["incident_id"],))
    assert inc["state"] == "acting" and inc["classification"] == "process_down"
    task = db.query_one("SELECT type, status, payload_json FROM task WHERE id=?",
                        (info["task_id"],))
    assert task["type"] == "ops" and task["status"] == "queued"
    assert json.loads(task["payload_json"])["action"] == "restart_demo_app"


def test_triage_app_error_records_only(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    info = _triage(db, source="probe:log", error_class="ValueError", severity="warning")
    assert info["classification"] == "app_error"
    assert "task_id" not in info
    inc = db.query_one("SELECT state FROM incident WHERE id=?", (info["incident_id"],))
    assert inc["state"] == "open"


def test_triage_traffic_dispatch_rate_limit(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    info = _triage(db, source="probe:traffic", error_class="qps_over",
                   severity="warning")
    task = db.query_one("SELECT payload_json FROM task WHERE id=?", (info["task_id"],))
    assert json.loads(task["payload_json"])["action"] == "set_rate_limit"


def test_flapping_third_process_down_escalates(fresh_db):
    """15min 窗口内同 subject 第 3 个 process_down → 不派发、直接转人工。"""
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    _triage(db)                       # 第 1 个（acting）
    _triage(db)                       # 第 2 个
    info = _triage(db)                # 第 3 个 → flapping
    assert info["escalated"] is True
    assert "task_id" not in info
    inc = db.query_one("SELECT state, escalation_reason FROM incident WHERE id=?",
                       (info["incident_id"],))
    assert inc["state"] == "escalated"
    assert inc["escalation_reason"] and "疑似抖动" in inc["escalation_reason"]


def test_apply_result_succeeded_resolves(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    info = _triage(db)
    out = triage.apply_task_result(db, incident_id=info["incident_id"],
                                   task_type="ops", status="succeeded",
                                   result={"verify": {"passed": True, "attempts": 1}})
    assert out["state"] == "resolved"
    inc = db.query_one("SELECT state, resolved_at FROM incident WHERE id=?",
                       (info["incident_id"],))
    assert inc["state"] == "resolved" and inc["resolved_at"]


def test_apply_result_failed_escalates_with_reason(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    info = _triage(db)
    out = triage.apply_task_result(db, incident_id=info["incident_id"],
                                   task_type="ops", status="failed",
                                   result={"exit_code": 0,
                                           "verify": {"passed": False, "attempts": 3}})
    assert out["state"] == "escalated"
    assert "复检未通过" in out["reason"]
    inc = db.query_one("SELECT state, escalation_reason FROM incident WHERE id=?",
                       (info["incident_id"],))
    assert inc["state"] == "escalated"


def test_incident_subject_join(fresh_db):
    db = fresh_db.get_db()
    db.connect()
    db.init_schema()
    info = _triage(db, subject="demo-app")
    assert triage.incident_subject(db, info["incident_id"]) == "demo-app"
