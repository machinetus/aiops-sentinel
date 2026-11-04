"""SQLite 数据层（design §2：alert / incident / task / action_log / patch_proposal）。

约定（design D2）：
- SQLite 单写者：只有 aiops-router 进程写库；monitor / worker 一律经 Router
  内部 API 读写，绝不直连数据库。
- WAL 模式 + busy_timeout=5000：读写不互阻塞，偶发锁等待自动重试。
- 时间戳统一 UTC ISO8601 字符串（utc_now_iso()）。

迁移约定（implement.md 阶段 1）：SCHEMA_VERSION + PRAGMA user_version；
后续阶段如需改表，递增版本号并在 init_schema() 里按版本补迁移语句。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = "/var/lib/aiops/aiops.db"
SCHEMA_VERSION = 1

# 五表 + 索引，与 design §2 逐列对应
SCHEMA_SQL = """
-- 2.1 alert：告警（指纹降噪由 Router /api/v1/alerts 实现）
CREATE TABLE IF NOT EXISTS alert (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT    NOT NULL,              -- sha256(source|subject|error_class)
    source        TEXT    NOT NULL,              -- probe:process/http/log/traffic、webhook、manual
    severity      TEXT    NOT NULL,              -- critical / warning / info
    subject       TEXT    NOT NULL,              -- 服务名，如 demo-app
    error_class   TEXT,                          -- process_dead / http_5xx / ValueError / qps_over …
    payload_json  TEXT    NOT NULL DEFAULT '{}', -- 原始载荷（含触发样本，供复测重放）
    count         INTEGER NOT NULL DEFAULT 1,    -- 同指纹聚合计数
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'firing'  -- firing / resolved
);
CREATE INDEX IF NOT EXISTS idx_alert_fp_status    ON alert (fingerprint, status);
CREATE INDEX IF NOT EXISTS idx_alert_status_seen  ON alert (status, last_seen_at);

-- 2.2 incident：事件（状态机见 design §2.6，阶段 3 接入）
CREATE TABLE IF NOT EXISTS incident (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint       TEXT,
    alert_id          INTEGER,
    title             TEXT,
    classification    TEXT,                        -- process_down / app_error / traffic_anomaly / manual_*
    state             TEXT    NOT NULL DEFAULT 'open',
    severity          TEXT,
    escalation_reason TEXT,                        -- 升级原因（失败/超时/人工拒绝）
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_incident_state         ON incident (state);
CREATE INDEX IF NOT EXISTS idx_incident_class_created ON incident (classification, created_at);

-- 2.3 task：任务（claim 协议见 design §4.3）
CREATE TABLE IF NOT EXISTS task (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id      INTEGER NOT NULL,
    type             TEXT    NOT NULL,             -- ops / diag / fix
    status           TEXT    NOT NULL DEFAULT 'queued',  -- queued/running/succeeded/failed/cancelled
    payload_json     TEXT,
    result_json      TEXT,
    worker_id        TEXT,
    lease_expires_at TEXT,                         -- 认领租约到期时间
    attempts         INTEGER NOT NULL DEFAULT 0,   -- 派发次数（重派最多 1 次）
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_type_status ON task (type, status);

-- 2.4 action_log：审计（双落：本表 + audit.jsonl，见 audit.py；design D15）
CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER,
    task_id     INTEGER,
    operator    TEXT NOT NULL,                     -- agent / human:<标识>
    action      TEXT NOT NULL,                     -- task_claim / task_succeeded / whitelist_deny …
    detail_json TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_log_incident ON action_log (incident_id, created_at);

-- 2.5 patch_proposal：修复提案（单飞：同 incident 至多一条 awaiting_confirm）
CREATE TABLE IF NOT EXISTS patch_proposal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  INTEGER NOT NULL,
    branch       TEXT,                             -- aiops/fix-<incident>
    pr_number    INTEGER,
    pr_url       TEXT,
    root_cause   TEXT,
    diff_stat    TEXT,                             -- 如 "+14 −2 (2 files)"
    test_summary TEXT,                             -- 如 "8 passed in 3.2s"
    status       TEXT NOT NULL,                    -- generating/testing/awaiting_confirm/merged/rejected/failed
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patch_incident_status ON patch_proposal (incident_id, status);
"""


def utc_now_iso() -> str:
    """UTC ISO8601 时间戳（秒级），全库统一时钟口径。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """进程内单连接 + 写锁：单写者纪律在本类的锁下收敛（Router 独占）。"""

    def __init__(self, path: str | None = None) -> None:
        # 延迟读环境变量：单测可通过 AIOPS_DB_PATH 指到临时文件
        self.path = path or os.environ.get("AIOPS_DB_PATH", DEFAULT_DB_PATH)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def lock(self) -> threading.RLock:
        """读-改-写序列必须持有该锁（Router 端点内使用）。"""
        return self._lock

    # -- 生命周期 ----------------------------------------------------------

    def connect(self) -> None:
        if self._conn is not None:
            return
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：写不阻塞读（design D2）
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.commit()

    def init_schema(self) -> None:
        """建表（幂等）+ 版本号落位。"""
        assert self._conn is not None, "先调用 connect()"
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            current = self._conn.execute("PRAGMA user_version;").fetchone()[0]
            if current < SCHEMA_VERSION:
                # TODO(后续阶段): 按版本补迁移语句（implement.md 迁移约定）
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- 基础读写（均为同步调用；单机演示量级足够，阶段 3 如有瓶颈再改 to_thread）--

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        assert self._conn is not None, "先调用 connect()"
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        assert self._conn is not None, "先调用 connect()"
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        assert self._conn is not None, "先调用 connect()"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# 进程级单例（Router 全局共用；测试用 reset_db() 重建）
# ---------------------------------------------------------------------------
_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database()
        return _db


def reset_db() -> None:
    """仅测试使用：丢弃单例，下次 get_db() 按当前环境变量重建。"""
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
        _db = None
