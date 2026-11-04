"""demo-app 主应用：业务面（:18000）+ admin 面（127.0.0.1:9101，随 lifespan 启动）。

- JSON 行日志（systemd StandardOutput=append 落 /var/log/demo-app/app.log，
  供日志探针采集；错误行含栈与请求体，供诊断取证 / 修复后重放）
- /health /metrics /api/orders /api/search
- BUG_MODE=type_bug 时 /api/orders 存在类型缺陷（design §9.3）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .admin import admin_app
from .limits import registry
from .orders import bug_mode, create_order

logger = logging.getLogger("demo_app")


class JsonFormatter(logging.Formatter):
    """JSON 行格式：Monitor 日志探针 / DiagWorker 依赖结构化错误行。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = {k: v for k, v in record.__dict__.items()
                 if k not in ("args", "asctime", "created", "exc_info", "exc_text",
                              "filename", "funcName", "levelname", "levelno", "lineno",
                              "module", "msecs", "message", "msg", "name", "pathname",
                              "process", "processName", "relativeCreated", "stack_info",
                              "stacklevel", "thread", "threadName", "taskName")}
        payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    # admin 面随业务进程启动（同进程共享 limits.registry；仅绑本机回环）
    admin_config = uvicorn.Config(admin_app, host="127.0.0.1", port=9101,
                                  log_level="warning", access_log=False)
    admin_server = uvicorn.Server(admin_config)
    threading.Thread(target=admin_server.run, daemon=True, name="admin-9101").start()
    logger.info("demo-app started", extra={"event": "app_started",
                                           "bug_mode": bug_mode(),
                                           "business_port": 18000, "admin_port": 9101})
    yield
    admin_server.should_exit = True
    logger.info("demo-app stopped", extra={"event": "app_stopped"})


app = FastAPI(title="demo-app", lifespan=lifespan)


@app.middleware("http")
async def metrics_limits_logging(request: Request, call_next):
    registry.note_request()
    client_ip = request.client.host if request.client else "-"
    if registry.check_ip(client_ip):
        return JSONResponse({"error": "blocked", "ip": client_ip}, status_code=403)
    if registry.check_rate():
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled error", extra={
            "event": "request_error", "method": request.method,
            "path": request.url.path, "client_ip": client_ip})
        raise
    logger.info("request", extra={
        "event": "request", "method": request.method, "path": request.url.path,
        "status": response.status_code, "client_ip": client_ip,
        "ms": round((time.perf_counter() - start) * 1000, 1)})
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "demo-app", "bug_mode": bug_mode()}


@app.get("/metrics")
async def metrics() -> dict:
    return {"service": "demo-app", "qps": registry.qps()}


@app.post("/api/orders")
async def orders(request: Request):
    """下单。type_bug 模式下 amount 传非整数会 500（场景二触发源）。"""
    body = await request.json()
    try:
        return create_order(body)
    except Exception:
        # 显式记录请求体：日志探针捕获后作为修复复测（重放）的触发样本
        logger.error("order failed", extra={
            "event": "order_error", "body": body,
            "client_ip": request.client.host if request.client else "-"},
            exc_info=True)
        raise


@app.get("/api/search")
async def search(request: Request):
    """搜索（流量场景的压测目标）。"""
    q = request.query_params.get("q", "")
    return {"query": q, "results": [{"id": i, "title": f"item-{i}-{q}"} for i in range(5)]}
