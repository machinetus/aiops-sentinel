"""OpsWorker 主进程（design §4.3 / §5.1）：

认领任务 → 白名单执行 → 复检（自证恢复）→ 回传结果。

- config.worker.ops_enabled=false 时进程直接退出（安全默认：全关 = 只告警不处置）
- 认领轮询 2s；执行与复检在线程池跑（动作可能耗时 60s+）
- whitelist_deny 以 failed + denied 标记回传，Router 侧落审计并告警
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

import httpx

from ..common.config import load_config
from ..common.log import setup_logging
from ..llm.client import LlmClient, LlmError
from .diag import DiagWorker
from .whitelist import WhitelistDenied, WhitelistExecutor

logger = logging.getLogger("sentinel.worker")

DEFAULT_ROUTER_URL = "http://127.0.0.1:18081"
POLL_INTERVAL_SECONDS = 2.0


class OpsWorker:
    def __init__(self, router_url: str, executor: WhitelistExecutor,
                 worker_id: str) -> None:
        self.router_url = router_url.rstrip("/")
        self.executor = executor
        self.worker_id = worker_id
        self.client = httpx.AsyncClient(timeout=30)

    async def _claim(self) -> dict | None:
        try:
            resp = await self.client.post(
                f"{self.router_url}/api/v1/tasks/claim",
                json={"worker_type": "ops", "worker_id": self.worker_id})
            return (resp.json() or {}).get("task")
        except Exception:  # noqa: BLE001 —— Router 短暂不可用不退出
            logger.exception("认领失败（Router 不可达？）")
            return None

    async def _report(self, task_id: int, status: str, result: dict) -> None:
        try:
            resp = await self.client.post(
                f"{self.router_url}/api/v1/tasks/{task_id}/result",
                json={"worker_id": self.worker_id, "status": status, "result": result})
            logger.info("结果回传 task=%s status=%s HTTP %s", task_id, status,
                        resp.status_code)
        except Exception:  # noqa: BLE001
            logger.exception("结果回传失败 task=%s", task_id)

    async def _handle(self, task: dict) -> None:
        task_id = int(task["id"])
        payload = task.get("payload") or {}
        action = payload.get("action")
        params = payload.get("params") or {}
        logger.info("执行 task=%s action=%s params=%s", task_id, action, params)

        status, result = "failed", {}
        try:
            outcome = await asyncio.to_thread(self.executor.execute, action, params)
            verify = await asyncio.to_thread(self.executor.verify, action)
            ok = outcome.ok and verify.get("passed", False)
            status = "succeeded" if ok else "failed"
            result = {"action": action, "kind": outcome.kind, **outcome.detail,
                      "verify": verify, "duration_s": outcome.duration_s}
        except WhitelistDenied as exc:
            # 三重门第三重拦截：记录并上报（Router 落 whitelist_deny 审计）
            logger.error("白名单拒绝 task=%s: %s", task_id, exc)
            result = {"action": action, "denied": True, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 —— 任何执行异常都回传失败，不悬挂
            logger.exception("执行异常 task=%s", task_id)
            result = {"action": action, "error": f"{type(exc).__name__}: {exc}"}

        await self._report(task_id, status, result)

    async def run(self) -> None:
        logger.info("OpsWorker 启动 worker_id=%s router=%s", self.worker_id, self.router_url)
        while True:
            task = await self._claim()
            if task is None:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            await self._handle(task)


async def main_async() -> None:
    setup_logging()
    config = load_config()
    if not config.worker.ops_enabled and not config.worker.diag_enabled:
        logger.info("worker.ops_enabled=false 且 diag_enabled=false，Worker 退出"
                    "（只告警不处置模式）")
        return
    router_url = os.environ.get("AIOPS_ROUTER_URL") or config.monitor.router_url
    host = socket.gethostname()
    runners: list = []

    if config.worker.ops_enabled:
        actions_path = os.environ.get("AIOPS_ACTIONS", "/etc/aiops/actions.yaml")
        executor = WhitelistExecutor.from_file(actions_path)
        runners.append(OpsWorker(router_url, executor,
                                 f"{host}-ops-{os.getpid()}").run())

    if config.worker.diag_enabled:
        try:
            llm = LlmClient.from_env(
                timeout=float(config.llm.timeout_seconds),
                temperature=float(config.llm.temperature))
        except LlmError as exc:
            logger.error("LLM 未就绪，diag 角色降级为不启用: %s", exc)
        else:
            runners.append(DiagWorker(router_url, llm,
                                      f"{host}-diag-{os.getpid()}").run())

    if not runners:
        logger.info("无可用 worker 角色，退出")
        return
    await asyncio.gather(*runners)


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
