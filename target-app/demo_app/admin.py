"""admin API（仅绑定 127.0.0.1:9101，Agent 专用受限安全接口，design §1.2/§6）。

白名单动作对应的副作用通道之二：
- POST /admin/limits {action: block|unblock|rate_limit|clear_rate, ...}
- GET  /admin/state
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .limits import registry

admin_app = FastAPI(title="demo-app admin", docs_url=None, redoc_url=None)


class LimitsIn(BaseModel):
    action: str = Field(description="block | unblock | rate_limit | clear_rate")
    ip: str | None = Field(default=None, description="block/unblock 必填")
    duration_min: float = Field(default=10, gt=0, description="生效时长（分钟）")
    qps: int | None = Field(default=None, description="rate_limit 必填")


@admin_app.get("/admin/state")
async def state() -> dict:
    return registry.snapshot()


@admin_app.post("/admin/limits")
async def set_limits(body: LimitsIn) -> dict:
    if body.action == "block":
        if not body.ip:
            raise HTTPException(400, "block 需要 ip")
        return registry.block_ip(body.ip, body.duration_min)
    if body.action == "unblock":
        if not body.ip:
            raise HTTPException(400, "unblock 需要 ip")
        return registry.unblock_ip(body.ip)
    if body.action == "rate_limit":
        if not body.qps:
            raise HTTPException(400, "rate_limit 需要 qps")
        return registry.set_rate_limit(body.qps, body.duration_min)
    if body.action == "clear_rate":
        return registry.clear_rate_limit()
    raise HTTPException(400, f"未知 action: {body.action}")
