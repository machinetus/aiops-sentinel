"""飞书卡片模板族（design §4.1/§6.1，lark_md + interactive card）。

卡片一律携带 incident/时间戳可追溯；本阶段（2）实际使用的只有告警卡，
其余按 design 结构先行落位，供阶段 3-6 消费。
"""

from __future__ import annotations

from datetime import datetime, timezone

SEVERITY_TEMPLATE = {"critical": "red", "warning": "orange", "info": "blue"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _div(md: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": md}}


def _note(text: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _card(title: str, template: str, elements: list[dict]) -> dict:
    return {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": template},
            "elements": elements}


# ---------------------------------------------------------------------------
# 告警卡（阶段 2）
# ---------------------------------------------------------------------------
def alert_card(*, subject: str, severity: str, error_class: str | None,
               message: str, count: int, alert_id: int, evidence: list[str] | None = None) -> dict:
    lines = [
        f"**服务**：{subject}",
        f"**分级**：{severity}　**错误类**：{error_class or '-'}",
        f"**摘要**：{message}",
    ]
    if count > 1:
        lines.append(f"**告警计数**：{count}（降噪窗口内聚合）")
    if evidence:
        quoted = "\n".join(f"> {ln[:200]}" for ln in evidence[:3])
        lines.append(f"**证据日志**：\n{quoted}")
    elements = [_div(l) for l in lines]
    elements.append(_note(f"alert_id={alert_id} · {_now()}"))
    return _card(f"🚨 告警 · {subject}", SEVERITY_TEMPLATE.get(severity, "grey"), elements)


# ---------------------------------------------------------------------------
# 处置结果卡（阶段 3）
# ---------------------------------------------------------------------------
def result_card(*, incident_id: int, subject: str, action: str,
                outcome: str, detail: str, cost_seconds: float | None = None) -> dict:
    cost = f"　**耗时**：{cost_seconds:.0f}s" if cost_seconds is not None else ""
    elements = [
        _div(f"**Incident**：#{incident_id}　**服务**：{subject}"),
        _div(f"**动作**：{action}　**结果**：{outcome}{cost}"),
        _div(f"**详情**：{detail}"),
        _note(f"incident #{incident_id} · {_now()}"),
    ]
    return _card(f"✅ 处置完成 · Incident #{incident_id}", "green", elements)


# ---------------------------------------------------------------------------
# 升级转人工卡（阶段 3）
# ---------------------------------------------------------------------------
def escalation_card(*, incident_id: int, subject: str, reason: str,
                    attempted: str = "") -> dict:
    elements = [
        _div(f"**Incident**：#{incident_id}　**服务**：{subject}"),
        _div(f"**升级原因**：{reason}"),
        _div(f"**已尝试**：{attempted or '无'}"),
        _div("**请人工接手排查**；自动处置已按 flapping/冷却策略暂停。"),
        _note(f"incident #{incident_id} · {_now()}"),
    ]
    return _card(f"🔴 转人工 · Incident #{incident_id}", "red", elements)


# ---------------------------------------------------------------------------
# 根因报告卡（阶段 4，DiagWorker）
# ---------------------------------------------------------------------------
def diag_card(*, incident_id: int, root_cause: str, suspect_files: list[dict],
              evidence: list[str], confidence: float, need_code_fix: bool) -> dict:
    files = "\n".join(f"- `{f['path']}:{f.get('line', '?')}` — {f.get('reason', '')}"
                      for f in suspect_files[:3]) or "- 无"
    quoted = "\n".join(f"> {ln[:200]}" for ln in evidence[:3]) or "> 无"
    elements = [
        _div(f"**Incident**：#{incident_id}"),
        _div(f"**根因**：{root_cause}"),
        _div(f"**嫌疑位置**：\n{files}"),
        _div(f"**证据**：\n{quoted}"),
        _div(f"**置信度**：{confidence:.2f}　**需要代码修复**：{'是' if need_code_fix else '否'}"),
        _note(f"LLM 根因分析 · incident #{incident_id} · {_now()}"),
    ]
    return _card(f"🔎 根因分析 · Incident #{incident_id}", "blue", elements)


# ---------------------------------------------------------------------------
# PR 确认卡（阶段 5，FixWorker；按钮 value 由 ConfirmService 消费）
# ---------------------------------------------------------------------------
def pr_confirm_card(*, incident_id: int, patch_id: int, root_cause: str,
                    trigger_sample: str, pr_number: int, pr_url: str,
                    diff_stat: str, test_summary: str) -> dict:
    elements = [
        _div(f"**Incident**：#{incident_id}"),
        _div(f"**根因**：{root_cause}"),
        _div(f"**触发样本**：{trigger_sample[:200]}"),
        _div(f"**PR**：[#{pr_number}]({pr_url}) `{diff_stat}` · 沙箱测试 `{test_summary}`"),
        {"tag": "action", "actions": [
            {"tag": "button", "text": {"content": "✅ 确认合并", "tag": "plain_text"},
             "type": "primary", "value": {"act": "pr_confirm", "patch_id": patch_id}},
            {"tag": "button", "text": {"content": "❌ 拒绝", "tag": "plain_text"},
             "type": "danger", "value": {"act": "pr_reject", "patch_id": patch_id}},
        ]},
        _note("按钮不可用时可回复：@agent 确认 PR#%d / @agent 拒绝 PR#%d · %s"
              % (pr_number, pr_number, _now())),
    ]
    return _card(f"🔧 修复待确认 · Incident #{incident_id}", "orange", elements)


# ---------------------------------------------------------------------------
# 事故时间线卡（阶段 6，@agent 报告）
# ---------------------------------------------------------------------------
def timeline_card(*, incident_id: int, title: str, state: str,
                  rows: list[str]) -> dict:
    body = "\n".join(rows) or "（暂无记录）"
    elements = [
        _div(f"**Incident**：#{incident_id}　**标题**：{title}　**状态**：{state}"),
        _div(f"**时间线**：\n{body}"),
        _note(f"incident #{incident_id} · {_now()}"),
    ]
    return _card(f"📋 事故时间线 · Incident #{incident_id}", "blue", elements)
