"""DiagWorker 单测：输出解析 / 上下文装配 / 失败语义（design §5.2/§7）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.workers.diag import (DiagWorker, build_diag_context,
                              parse_diag_output)

VALID = {
    "root_cause": "orders.py:36 未校验 amount 类型，int('abc') 抛 ValueError",
    "error_class": "ValueError",
    "suspect_files": [
        {"path": "target-app/demo_app/orders.py", "line": 36, "reason": "int(amount)"},
        {"path": "src/router/main.py", "line": 1, "reason": "幻觉路径应被过滤"},
    ],
    "evidence": ["ValueError: invalid literal for int() with base 10: 'abc'"],
    "fix_suggestion": "校验 amount 类型并返回 400",
    "need_code_fix": True,
    "confidence": 0.9,
}


def test_parse_valid_filters_path_whitelist():
    out = parse_diag_output(json.dumps(VALID))
    assert out.root_cause and out.need_code_fix is True
    # 非 target-app/ 的路径被过滤
    assert [f.path for f in out.suspect_files] == ["target-app/demo_app/orders.py"]


def test_parse_markdown_fence_tolerated():
    raw = "```json\n" + json.dumps(VALID) + "\n```"
    assert parse_diag_output(raw).confidence == 0.9


def test_parse_invalid_json_raises():
    with pytest.raises(ValueError):
        parse_diag_output("不是 JSON")


def test_parse_missing_required_field_raises():
    bad = dict(VALID)
    del bad["root_cause"]
    with pytest.raises(Exception):
        parse_diag_output(json.dumps(bad))


def test_build_context_includes_evidence_and_stack_files(tmp_path):
    deploy = tmp_path / "deploy"
    app_dir = deploy / "target-app" / "demo_app"
    app_dir.mkdir(parents=True)
    orders = app_dir / "orders.py"
    orders.write_text("def create_order():\n    total = int(amount)\n", encoding="utf-8")
    log = tmp_path / "app.log"
    log.write_text(
        '{"level": "ERROR", "msg": "order failed", "exc": "Traceback...\\n'
        f'  File \\"{orders}\\", line 2, in create_order\\n'
        "ValueError: invalid literal for int() with base 10: 'abc'\"}\n"
        'token=secret-value-123 should be redacted\n',
        encoding="utf-8")

    payload = {"subject": "demo-app", "error_class": "ValueError",
               "message": "m", "incident_id": 7,
               "evidence": [f'File "{orders}", line 2, in create_order']}
    ctx = json.loads(build_diag_context(payload, str(log), str(deploy)))
    assert ctx["incident"]["incident_id"] == 7
    assert ctx["alert_evidence"]
    assert ctx["stack_files"] and ctx["stack_files"][0]["path"] == \
        "target-app/demo_app/orders.py"
    assert "int(amount)" in ctx["stack_files"][0]["content"]
    assert "target-app/demo_app/orders.py" in ctx["repo_tree"]
    # 脱敏：token 值不得出现在上下文
    assert "secret-value-123" not in json.dumps(ctx, ensure_ascii=False)


class _FakeLlm:
    def __init__(self, raw: str | None = None, error: Exception | None = None):
        self.raw = raw
        self.error = error

    async def complete(self, system_prompt: str, user_content: str) -> str:
        if self.error:
            raise self.error
        return self.raw or ""


def _make_worker(tmp_path, llm):
    deploy = tmp_path / "deploy"
    (deploy / "target-app").mkdir(parents=True)
    log = tmp_path / "app.log"
    log.write_text("", encoding="utf-8")
    worker = DiagWorker("http://router.invalid", llm, "w-diag",
                        app_log=str(log), deploy_dir=str(deploy))
    reports = []

    async def fake_report(task_id, status, result):
        reports.append((task_id, status, result))

    worker._report = fake_report  # type: ignore[assignment]
    return worker, reports


def test_worker_success_reports_diag_result(tmp_path):
    worker, reports = _make_worker(tmp_path, _FakeLlm(json.dumps(VALID)))
    asyncio.run(worker._handle({"id": 5, "payload": {"subject": "demo-app",
                                                     "error_class": "ValueError"}}))
    task_id, status, result = reports[0]
    assert task_id == 5 and status == "succeeded"
    assert result["need_code_fix"] is True and result["confidence"] == 0.9


def test_worker_llm_failure_reports_failed(tmp_path):
    worker, reports = _make_worker(tmp_path, _FakeLlm(error=RuntimeError("timeout")))
    asyncio.run(worker._handle({"id": 6, "payload": {"subject": "demo-app"}}))
    _, status, result = reports[0]
    assert status == "failed" and "timeout" in result["error"]
