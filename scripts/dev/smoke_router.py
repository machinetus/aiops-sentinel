"""Router 骨架端到端冒烟脚本（implement.md 阶段 1 验证项 4）。

前置：Router 已运行（systemd 或手动 uvicorn，默认 http://127.0.0.1:18081）。
用途：不依赖 Monitor/Worker，直接验证「告警降噪 + 认领/回传协议 + 审计」全链路。

用法（VM 上）：
    python /opt/aiops/venv/bin/python scripts/dev/smoke_router.py
    # 或开发机：python scripts/dev/smoke_router.py
可选环境变量：
    SENTINEL_ROUTER_URL   缺省 http://127.0.0.1:18081
    AIOPS_DB_PATH         缺省 /var/lib/aiops/aiops.db（与 Router 进程一致）

成功输出 SMOKE PASS；任何一步失败输出 SMOKE FAIL:<步骤> 并以非零码退出。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("SENTINEL_ROUTER_URL", "http://127.0.0.1:18081")
DB_PATH = os.environ.get("AIOPS_DB_PATH", "/var/lib/aiops/aiops.db")


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """极简 HTTP 客户端（不引第三方库，VM 裸跑可用）。"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _seed_queued_task() -> int:
    """直接向 SQLite 播一条 queued 任务（WAL 允许与 Router 进程并发读写）。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute(
        "INSERT INTO incident (title, classification, state, severity, created_at, updated_at) "
        "VALUES ('冒烟演示宕机', 'process_down', 'open', 'critical', ?, ?)",
        (now, now),
    )
    cur = conn.execute(
        "INSERT INTO task (incident_id, type, status, payload_json, created_at, updated_at) "
        "VALUES (1, 'ops', 'queued', '{\"action\": \"restart_demo_app\", \"smoke\": true}', ?, ?)",
        (now, now),
    )
    task_id = int(cur.lastrowid or 0)
    conn.commit()
    conn.close()
    return task_id


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        print(f"SMOKE FAIL:{name}")
        sys.exit(1)


def main() -> None:
    print(f"[smoke] Router={BASE} DB={DB_PATH}")

    # 1. 健康检查（必须校验 service 字段：防止端口打错打到别的服务上——2026-11-04 实测教训）
    status, body = _req("GET", "/healthz")
    step("healthz", status == 200 and body.get("status") == "ok"
         and body.get("service") == "sentinel-router",
         json.dumps(body, ensure_ascii=False))

    # 2. 告警降噪：首条新建，第二条命中窗口只计数
    alert = {"source": "probe:http", "severity": "critical", "subject": "demo-app",
             "error_class": "http_5xx", "message": "smoke", "payload": {"sample": "GET /health"}}
    s1, a1 = _req("POST", "/api/v1/alerts", alert)
    step("alert#1 新建", s1 == 200 and a1["deduped"] is False and a1["count"] == 1, str(a1))
    s2, a2 = _req("POST", "/api/v1/alerts", alert)
    step("alert#2 降噪", s2 == 200 and a2["deduped"] is True and a2["count"] == 2
         and a2["alert_id"] == a1["alert_id"], str(a2))

    # 3. 任务认领（FIFO + 租约）
    task_id = _seed_queued_task()
    s3, c = _req("POST", "/api/v1/tasks/claim", {"worker_type": "ops", "worker_id": "smoke-worker"})
    task = c.get("task") or {}
    step("task claim", s3 == 200 and task.get("id") == task_id
         and task.get("lease_expires_at"), str(task))
    s3b, c2 = _req("POST", "/api/v1/tasks/claim", {"worker_type": "ops", "worker_id": "smoke-worker"})
    step("claim 空队列", s3b == 200 and c2.get("task") is None, str(c2))

    # 4. 负路径：非认领者回传 → 403
    s4, _ = _req("POST", f"/api/v1/tasks/{task_id}/result",
                 {"worker_id": "someone-else", "status": "succeeded"})
    step("result 403 防伪造", s4 == 403, f"HTTP {s4}")

    # 5. 正常回传
    s5, r = _req("POST", f"/api/v1/tasks/{task_id}/result",
                 {"worker_id": "smoke-worker", "status": "succeeded",
                  "result": {"verify": "3/3 passed", "smoke": True}})
    step("task result", s5 == 200 and r.get("accepted") is True, str(r))

    # 6. 审计留痕：task_claim + task_succeeded 至少各一条
    conn = sqlite3.connect(DB_PATH, timeout=10)
    rows = conn.execute(
        "SELECT action, COUNT(*) FROM action_log WHERE task_id = ? GROUP BY action",
        (task_id,),
    ).fetchall()
    conn.close()
    actions = {a: n for a, n in rows}
    step("审计双落", actions.get("task_claim", 0) >= 1 and actions.get("task_succeeded", 0) >= 1,
         str(actions))

    print("SMOKE PASS")


if __name__ == "__main__":
    main()
