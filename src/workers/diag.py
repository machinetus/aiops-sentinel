"""DiagWorker（design §5.2）：LLM 根因分析（只读，不碰执行面）。

输入装配（全部确定性代码完成，LLM 只见裁剪+脱敏后的数据）：
- 告警证据行（alerts.payload_json.evidence，monitor 采集）
- 应用日志尾部 200 行（脱敏：token/secret/authorization 掩码）
- 栈帧提到的源码文件（≤3 个，每个 ≤400 行，带行号便于定位 文件:行号）
- target-app 文件树

输出：严格 JSON（pydantic 校验 + 路径白名单过滤）→ 回传 Router 渲染根因卡片。
LLM 失败（超时/非 JSON/校验不过）→ task failed → Router 升级转人工（fail-safe）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from ..common.log import SECRET_PATTERN
from ..llm.prompts import DIAG_SYSTEM_PROMPT

logger = logging.getLogger("sentinel.worker.diag")

APP_LOG = os.environ.get("AIOPS_APP_LOG", "/var/log/demo-app/app.log")
DEPLOY_DIR = os.environ.get("AIOPS_DEPLOY_DIR", "/opt/aiops/deploy")
LOG_TAIL_LINES = 200
MAX_FILES = 3
MAX_FILE_LINES = 400

_STACK_FILE_RE = re.compile(r'File "([^"]+\.py)"')


class SuspectFile(BaseModel):
    path: str
    line: int | None = None
    reason: str = ""


class DiagOutput(BaseModel):
    root_cause: str
    error_class: str | None = None
    suspect_files: list[SuspectFile] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    fix_suggestion: str = ""
    need_code_fix: bool = False
    confidence: float = 0.0


def parse_diag_output(raw: str) -> DiagOutput:
    """解析 LLM 输出：容错 markdown 代码块 + 路径白名单过滤。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 输出非 JSON: {exc}") from exc
    out = DiagOutput.model_validate(data)
    # 路径白名单：只允许 target-app/（防 LLM 幻觉指向 agent 自身代码）
    out.suspect_files = [f for f in out.suspect_files
                         if f.path.startswith("target-app/")]
    return out


def _redact(text: str) -> str:
    return SECRET_PATTERN.sub(r"\1=***", text)


def _tail_lines(path: str, n: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [_redact(ln.rstrip("\n")) for ln in lines[-n:]]
    except OSError:
        return []


def _stack_files(evidence: list[str], log_tail: list[str], deploy_dir: str) -> list[str]:
    """从栈帧提取源码绝对路径（去重、限 deploy_dir 内、限 MAX_FILES）。"""
    seen: list[str] = []
    for line in list(evidence) + log_tail:
        for m in _STACK_FILE_RE.finditer(line):
            p = m.group(1)
            if p.startswith(deploy_dir) and p not in seen:
                seen.append(p)
    return seen[:MAX_FILES]


def _numbered(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[:MAX_FILE_LINES]
    except OSError:
        return "(文件不可读)"
    return "\n".join(f"{i:4d}  {_redact(ln.rstrip())}" for i, ln in enumerate(lines, 1))


def _repo_tree(deploy_dir: str) -> list[str]:
    root = Path(deploy_dir) / "target-app"
    try:
        return sorted(p.relative_to(deploy_dir).as_posix()
                      for p in root.rglob("*.py") if p.is_file())
    except OSError:
        return []


def build_diag_context(task_payload: dict, app_log: str = APP_LOG,
                       deploy_dir: str = DEPLOY_DIR) -> str:
    """装配 LLM 输入（JSON 字符串）。字段与 DIAG_SYSTEM_PROMPT 的输入约定一致。"""
    evidence = list(task_payload.get("evidence") or [])[:10]
    log_tail = _tail_lines(app_log, LOG_TAIL_LINES)
    stack_file_paths = _stack_files(evidence, log_tail, deploy_dir)
    context = {
        "incident": {
            "subject": task_payload.get("subject"),
            "error_class": task_payload.get("error_class"),
            "message": task_payload.get("message"),
            "incident_id": task_payload.get("incident_id"),
        },
        "alert_evidence": evidence,
        "log_tail": log_tail,
        "stack_files": [
            {"path": Path(p).relative_to(deploy_dir).as_posix(),
             "content": _numbered(p)}
            for p in stack_file_paths
        ],
        "repo_tree": _repo_tree(deploy_dir),
    }
    return json.dumps(context, ensure_ascii=False, indent=1)


class DiagWorker:
    def __init__(self, router_url: str, llm, worker_id: str,
                 app_log: str = APP_LOG, deploy_dir: str = DEPLOY_DIR) -> None:
        self.router_url = router_url.rstrip("/")
        self.llm = llm
        self.worker_id = worker_id
        self.app_log = app_log
        self.deploy_dir = deploy_dir
        self.client = httpx.AsyncClient(timeout=30)

    async def _claim(self) -> dict | None:
        try:
            resp = await self.client.post(
                f"{self.router_url}/api/v1/tasks/claim",
                json={"worker_type": "diag", "worker_id": self.worker_id})
            return (resp.json() or {}).get("task")
        except Exception:  # noqa: BLE001 —— Router 短暂不可用不退出
            logger.exception("diag 认领失败（Router 不可达？）")
            return None

    async def _report(self, task_id: int, status: str, result: dict) -> None:
        try:
            resp = await self.client.post(
                f"{self.router_url}/api/v1/tasks/{task_id}/result",
                json={"worker_id": self.worker_id, "status": status, "result": result})
            logger.info("诊断回传 task=%s status=%s HTTP %s", task_id, status,
                        resp.status_code)
        except Exception:  # noqa: BLE001
            logger.exception("诊断回传失败 task=%s", task_id)

    async def _handle(self, task: dict) -> None:
        task_id = int(task["id"])
        payload = task.get("payload") or {}
        logger.info("诊断 task=%s subject=%s error_class=%s",
                    task_id, payload.get("subject"), payload.get("error_class"))
        status, result = "failed", {}
        try:
            context = await asyncio.to_thread(
                build_diag_context, payload, self.app_log, self.deploy_dir)
            raw = await self.llm.complete(DIAG_SYSTEM_PROMPT, context)
            out = parse_diag_output(raw)
            status = "succeeded"
            result = out.model_dump()
            logger.info("诊断完成 task=%s 置信度=%.2f need_code_fix=%s",
                        task_id, out.confidence, out.need_code_fix)
        except Exception as exc:  # noqa: BLE001 —— 任何失败都回传 failed（fail-safe 转人工）
            logger.exception("诊断失败 task=%s", task_id)
            result = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        await self._report(task_id, status, result)

    async def run(self) -> None:
        logger.info("DiagWorker 启动 worker_id=%s router=%s", self.worker_id,
                    self.router_url)
        while True:
            task = await self._claim()
            if task is None:
                await asyncio.sleep(2.0)
                continue
            await self._handle(task)
