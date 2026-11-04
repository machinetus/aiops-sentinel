"""审计双落（design D15 / §2.4）：

- SQLite `action_log` 表：供时间线报告（@agent 报告 n）查询；
- /var/log/aiops/audit.jsonl：append-only，供归档与事后审计（logrotate 按周）。

铁律：审计只存元数据（动作/命令/退出码/摘要），不存提示词与密钥明文；
JSONL 落盘失败不得阻断主流程（SQLite 为准）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from .db import Database, utc_now_iso

AUDIT_JSONL_PATH = os.environ.get("AIOPS_AUDIT_JSONL", "/var/log/aiops/audit.jsonl")


def log_action(
    db: Database,
    *,
    operator: str,
    action: str,
    incident_id: int | None = None,
    task_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """记录一条审计（agent 与 human 动作统一入口）。

    operator: "agent" 或 "human:<open_id/备注>"（design §2.4）
    action:   task_claim / task_succeeded / whitelist_deny / state_transition …
    返回 action_log 行 id。
    """
    created_at = utc_now_iso()
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    cur = db.execute(
        "INSERT INTO action_log (incident_id, task_id, operator, action, detail_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (incident_id, task_id, operator, action, detail_json, created_at),
    )
    row_id = int(cur.lastrowid or 0)

    # JSONL 双落：尽力而为，失败不影响主流程
    try:
        dir_name = os.path.dirname(AUDIT_JSONL_PATH)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        entry = {
            "id": row_id,
            "incident_id": incident_id,
            "task_id": task_id,
            "operator": operator,
            "action": action,
            "detail": detail,
            "created_at": created_at,
        }
        with open(AUDIT_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return row_id
