"""Router 接口数据 Schema（Pydantic v2，design §1.2 四个端点）。

命名约定：*In = 请求体，*Out = 响应体；字段与 design §2 表列一一对应。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# POST /api/v1/alerts —— 告警入口（Monitor / 业务 webhook 共用，design §3.4）
# ---------------------------------------------------------------------------
Severity = Literal["critical", "warning", "info"]


class AlertIn(BaseModel):
    source: str = Field(
        min_length=1,
        description="告警来源：probe:process / probe:http / probe:log / probe:traffic / webhook / manual",
        examples=["probe:http"],
    )
    severity: Severity = Field(description="分级：critical / warning / info（design R1.4）")
    subject: str = Field(min_length=1, description="服务名", examples=["demo-app"])
    error_class: Optional[str] = Field(
        default=None,
        description="错误类归类：process_dead / http_5xx / ValueError / qps_over …",
        examples=["http_5xx"],
    )
    message: Optional[str] = Field(default=None, description="人类可读摘要，进卡片文案")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="原始载荷（含触发样本 sample，修复后复测重放的数据源，design §3.1）",
    )


class AlertOut(BaseModel):
    alert_id: int = Field(description="alert 表行 id")
    fingerprint: str = Field(description="sha256 指纹（design §3.2）")
    count: int = Field(description="同指纹聚合计数")
    deduped: bool = Field(description="True=命中降噪窗口（只计数未新建）")


# ---------------------------------------------------------------------------
# POST /api/v1/tasks/claim —— 任务认领（design §4.3）
# ---------------------------------------------------------------------------
TaskType = Literal["ops", "diag", "fix"]


class ClaimIn(BaseModel):
    worker_type: TaskType = Field(description="Worker 角色类型")
    worker_id: str = Field(min_length=1, description="Worker 实例标识（回传结果时校验一致）")


class ClaimedTask(BaseModel):
    id: int
    incident_id: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict, description="动作名与参数 / 诊断上下文")
    attempts: int = Field(description="派发次数（首派=1，租约过期重派=2）")
    lease_expires_at: str = Field(description="租约到期时间（UTC ISO8601），到期必须已回传结果")


class ClaimOut(BaseModel):
    task: Optional[ClaimedTask] = Field(
        default=None, description="None=当前无排队任务（Worker 轮询继续等）"
    )


# ---------------------------------------------------------------------------
# POST /api/v1/tasks/{task_id}/result —— 任务结果回传（design §4.3）
# ---------------------------------------------------------------------------
class TaskResultIn(BaseModel):
    worker_id: str = Field(min_length=1, description="必须与认领时一致，否则 403")
    status: Literal["succeeded", "failed"]
    result: dict[str, Any] = Field(
        default_factory=dict,
        description="执行结果：退出码 / 复检结论 / 根因 / PR 信息等（阶段 3+ 丰富）",
    )


class TaskResultOut(BaseModel):
    task_id: int
    status: str
    accepted: bool = Field(description="是否已被 Router 接受（False 时配合 HTTP 错误码）")


# ---------------------------------------------------------------------------
# GET /healthz
# ---------------------------------------------------------------------------
class HealthOut(BaseModel):
    status: str
    service: str
    time: str
