"""飞书指令解析（design §4.2）：规则优先、LLM 兜底。

纯解析逻辑（无 IO），入站长连接接线在阶段 6——待飞书自建应用凭据就绪：
receiver 收到 @消息 → parse_intent() → 命中规则直接派发；未命中走 LLM → 低置信度反问。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sentinel.feishu.intent")

# 规则表（design §4.2）：(正则, intent)；大小写不敏感，先清理 @mention
_RULES: list[tuple[str, str]] = [
    (r"^(状态|status)\s*$", "query.status"),
    (r"重启.*(demo[-_]?app)|restart", "ops.restart_demo_app"),
    (r"诊断|diag", "diag"),
    (r"修复|fix", "fix"),
    (r"报告\s*([0-9]+)?", "query.timeline"),
    (r"确认.*PR\s*#?\s*([0-9]+)", "pr_confirm"),
    (r"拒绝.*PR\s*#?\s*([0-9]+)", "pr_reject"),
    (r"^(帮助|help)$", "help"),
]

_MENTION_RE = re.compile(r"@_user_\d+")   # 飞书 @ mention 占位符


@dataclass
class Intent:
    intent: str = "unknown"
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    source: str = "rule"      # rule | llm | fallback


def _clean(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def parse_intent(text: str) -> Intent:
    """规则优先；未命中返回 unknown（由调用方决定是否走 LLM 兜底）。"""
    cleaned = _clean(text)
    if not cleaned:
        return Intent(intent="unknown", confidence=0.0)
    for pattern, intent in _RULES:
        m = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if m:
            params: dict[str, Any] = {}
            if intent == "query.timeline" and m.group(1):
                params["incident_id"] = int(m.group(1))
            if intent in ("pr_confirm", "pr_reject") and m.group(1):
                params["pr_number"] = int(m.group(1))
            return Intent(intent=intent, params=params, confidence=1.0,
                          source="rule")
    return Intent(intent="unknown", confidence=0.0)


INTENT_LLM_PROMPT = """\
你是运维指令解析器。把用户消息解析为 JSON（只输出 JSON）：
{"intent": "ops.restart_demo_app|diag|fix|query.status|query.timeline|pr_confirm|pr_reject|help|unknown",
 "params": {"pr_number": 0, "incident_id": 0},
 "confidence": 0.0}
params 只在相关时给出。无法判断时 intent=unknown、confidence 给低分。
"""


async def classify_with_llm(llm, text: str) -> Intent:
    """LLM 兜底解析（规则未命中时）。失败返回 unknown（fail-safe 反问澄清）。"""
    try:
        raw = await llm.complete(INTENT_LLM_PROMPT, _clean(text))
        data = json.loads(raw.strip().strip("`").removeprefix("json").strip())
        return Intent(intent=str(data.get("intent", "unknown")),
                      params=dict(data.get("params") or {}),
                      confidence=float(data.get("confidence", 0.0)),
                      source="llm")
    except Exception:  # noqa: BLE001 —— 兜底失败不阻断，反问澄清
        logger.exception("LLM 意图解析失败")
        return Intent(intent="unknown", confidence=0.0, source="fallback")
