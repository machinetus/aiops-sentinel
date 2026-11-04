"""飞书指令解析单测（design §4.2 规则表）。"""

from __future__ import annotations

from src.feishu.intent import Intent, parse_intent


def test_status():
    assert parse_intent("状态").intent == "query.status"
    assert parse_intent("status").intent == "query.status"


def test_restart_rule():
    assert parse_intent("重启 demo-app").intent == "ops.restart_demo_app"
    assert parse_intent("restart demo_app").intent == "ops.restart_demo_app"


def test_diag_fix():
    assert parse_intent("诊断一下 incident 5").intent == "diag"
    assert parse_intent("修复这个问题").intent == "fix"


def test_timeline_with_incident_id():
    it = parse_intent("报告 12")
    assert it.intent == "query.timeline" and it.params["incident_id"] == 12
    assert parse_intent("报告").params == {}


def test_confirm_reject_pr():
    it = parse_intent("确认 PR#13")
    assert it.intent == "pr_confirm" and it.params["pr_number"] == 13
    it = parse_intent("拒绝 PR 19")
    assert it.intent == "pr_reject" and it.params["pr_number"] == 19


def test_help():
    assert parse_intent("帮助").intent == "help"
    assert parse_intent("help").intent == "help"


def test_mention_cleanup_and_unknown():
    assert parse_intent("@_user_1 状态").intent == "query.status"
    assert parse_intent("@_user_1").intent == "unknown"
    assert parse_intent("今天天气不错").intent == "unknown"


def test_rule_confidence_full():
    it = parse_intent("状态")
    assert isinstance(it, Intent) and it.confidence == 1.0 and it.source == "rule"
