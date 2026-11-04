"""FixWorker（design §5.3）：补丁生成 → 沙箱 → draft PR。

八步流水线（阶段 5，确认通道=方案 A：GitHub PR review）：
1. worktree：sandbox/repo 克隆上建分支 aiops/fix-<incident>
2. 摘要上下文：根因 + 嫌疑文件原文（去行号）+ 测试文件
3. LLM 生成补丁 JSON（FIX_SYSTEM_PROMPT）
4. patcher.apply_patch 确定性校验与应用（路径白名单/唯一匹配/行数/必须含测试）
5. 沙箱：独立 venv + wheelhouse 离线安装 + pytest（180s 硬超时）
6. commit + push 分支（token 一次性 URL，不落盘）
7. GitHub API 建 draft PR
8. 回传结果（pr_number/pr_url/diff_stat/test_summary）→ Router 发飞书 PR 卡
   人工在 GitHub 合并后，Router 轮询检测并派发部署任务（自动部署+复测）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from ..llm.prompts import FIX_SYSTEM_PROMPT
from .patcher import PatchRejected, apply_patch

logger = logging.getLogger("sentinel.worker.fix")

GITHUB_API = "https://api.github.com"
MAX_FILE_LINES = 400
MAX_CONTEXT_FILES = 3
SANDBOX_TIMEOUT = 180


class FixOutput(BaseModel):
    changes: list[dict] = Field(min_length=1)
    test_command: str = "pytest -q"


def parse_fix_output(raw: str) -> FixOutput:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 补丁输出非 JSON: {exc}") from exc
    return FixOutput.model_validate(data)


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 60,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def run_sandbox(worktree: Path, wheelhouse: str, test_command: str) -> tuple[bool, str]:
    """沙箱：独立 venv + wheelhouse 离线安装 + pytest。整体硬超时 180s。"""
    started = time.monotonic()
    venv_dir = worktree / ".sbx-venv"
    py = venv_dir / "bin" / "python"
    try:
        if not py.exists():
            _run(["python3", "-m", "venv", str(venv_dir)], timeout=60)
        pip = venv_dir / "bin" / "pip"
        # 先装构建必需件（PEP517 editable 安装需要 setuptools/wheel，均来自离线 wheelhouse）
        prep = _run(
            [str(pip), "install", "--no-index", "--find-links", wheelhouse,
             "setuptools", "wheel"],
            cwd=worktree, timeout=60)
        if prep.returncode != 0:
            return False, f"沙箱构建件安装失败: {(prep.stdout + prep.stderr)[-300:]}"
        install = _run(
            [str(pip), "install", "--no-index", "--find-links", wheelhouse,
             "--no-build-isolation", "-e", "target-app"],
            cwd=worktree, timeout=90)
        if install.returncode != 0:
            return False, f"沙箱安装失败: {(install.stdout + install.stderr)[-300:]}"
        tests = _run(
            [str(py), "-m", "pytest", "target-app/tests", "-q", "--timeout=60"],
            cwd=worktree, timeout=SANDBOX_TIMEOUT)
        summary_line = ""
        for line in reversed((tests.stdout + tests.stderr).splitlines()):
            if "passed" in line or "failed" in line or "error" in line.lower():
                summary_line = line.strip()
                break
        ok = tests.returncode == 0
        elapsed = time.monotonic() - started
        return ok, f"{summary_line or '测试无输出'}（沙箱 {elapsed:.0f}s）"
    except subprocess.TimeoutExpired:
        return False, f"沙箱超时（>{SANDBOX_TIMEOUT}s）"


def _read_source(worktree: Path, rel: str, max_lines: int = MAX_FILE_LINES) -> str:
    path = worktree / rel
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "(不可读)"
    return "\n".join(lines[:max_lines])


def build_fix_context(payload: dict, worktree: Path) -> str:
    """装配修复上下文：根因 + 嫌疑文件原文（无行号，便于 old_text 逐字匹配）+ 测试列表。"""
    suspects = []
    for item in (payload.get("suspect_files") or [])[:MAX_CONTEXT_FILES]:
        rel = str(item.get("path") or "")
        if not rel.startswith("target-app/"):
            continue
        suspects.append({
            "path": rel,
            "line_hint": item.get("line"),
            "reason": item.get("reason"),
            "content": _read_source(worktree, rel),
        })
    tests_dir = worktree / "target-app" / "tests"
    test_files = sorted(p.name for p in tests_dir.glob("*.py")) if tests_dir.is_dir() else []
    context = {
        "root_cause": payload.get("root_cause"),
        "error_class": payload.get("error_class"),
        "suspect_files": suspects,
        "test_files": test_files,
        "sample": payload.get("sample"),
    }
    return json.dumps(context, ensure_ascii=False, indent=1)


class FixWorker:
    def __init__(self, router_url: str, llm, worker_id: str, *,
                 repo_dir: str = "/opt/aiops/sandbox/repo",
                 sandbox_root: str = "/opt/aiops/sandbox",
                 wheelhouse: str = "/opt/aiops/sandbox/wheelhouse",
                 github_repo: str = "machinetus/aiops-sentinel") -> None:
        self.router_url = router_url.rstrip("/")
        self.llm = llm
        self.worker_id = worker_id
        self.repo_dir = Path(repo_dir)
        self.sandbox_root = Path(sandbox_root)
        self.wheelhouse = wheelhouse
        self.github_repo = github_repo
        self.token = os.environ.get("GH_TOKEN", "")
        self.client = httpx.AsyncClient(timeout=30)

    # ---- 认领/回传 -------------------------------------------------------

    async def _claim(self) -> dict | None:
        try:
            resp = await self.client.post(
                f"{self.router_url}/api/v1/tasks/claim",
                json={"worker_type": "fix", "worker_id": self.worker_id})
            return (resp.json() or {}).get("task")
        except Exception:  # noqa: BLE001
            logger.exception("fix 认领失败")
            return None

    async def _report(self, task_id: int, status: str, result: dict) -> None:
        try:
            resp = await self.client.post(
                f"{self.router_url}/api/v1/tasks/{task_id}/result",
                json={"worker_id": self.worker_id, "status": status, "result": result})
            logger.info("修复回传 task=%s status=%s HTTP %s", task_id, status,
                        resp.status_code)
        except Exception:  # noqa: BLE001
            logger.exception("修复回传失败 task=%s", task_id)

    # ---- 主流程 ----------------------------------------------------------

    async def _handle(self, task: dict) -> None:
        task_id = int(task["id"])
        payload = task.get("payload") or {}
        incident_id = payload.get("incident_id") or task.get("incident_id")
        branch = f"aiops/fix-{incident_id}"
        wt = self.sandbox_root / f"wt-{incident_id}"
        logger.info("修复开始 task=%s incident=%s", task_id, incident_id)
        status, result = "failed", {}
        try:
            await asyncio.to_thread(self._prepare_worktree, wt, branch)
            # LLM 必须在主事件循环里 await（AsyncClient 不能跨事件循环）
            context = build_fix_context(payload, wt)
            raw = await self.llm.complete(FIX_SYSTEM_PROMPT, context)
            stats = apply_patch(wt, parse_fix_output(raw).changes)
            logger.info("补丁已应用: %s", stats.diff_stat)
            test_ok, test_summary = await asyncio.to_thread(
                run_sandbox, wt, self.wheelhouse, "pytest -q")
            if not test_ok:
                raise PatchRejected(f"沙箱测试未通过: {test_summary}")
            pr = await asyncio.to_thread(self._commit_push_pr, wt, branch,
                                         incident_id, payload)
            status = "succeeded"
            result = {
                "pr_number": pr["number"], "pr_url": pr["html_url"],
                "branch": branch, "diff_stat": stats.diff_stat,
                "test_summary": test_summary,
                "root_cause": payload.get("root_cause", ""),
            }
        except PatchRejected as exc:
            logger.error("补丁被拒 task=%s: %s", task_id, exc)
            result = {"patch_rejected": True, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 —— 任何失败都回传（fail-safe）
            logger.exception("修复失败 task=%s", task_id)
            result = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        finally:
            await asyncio.to_thread(self._cleanup_worktree, wt, branch)
        await self._report(task_id, status, result)

    def _prepare_worktree(self, wt: Path, branch: str) -> None:
        """同步部分：清理旧 worktree → fetch → 建新 worktree（不碰 LLM）。"""
        self._cleanup_worktree(wt, branch)
        _run(["git", "-C", str(self.repo_dir), "fetch", "origin", "main"], timeout=60)
        add = _run(["git", "-C", str(self.repo_dir), "worktree", "add",
                    str(wt), "-b", branch, "origin/main"], timeout=60)
        if add.returncode != 0:
            raise RuntimeError(f"worktree 创建失败: {add.stderr[-300:]}")

    def _commit_push_pr(self, wt: Path, branch: str, incident_id: int,
                        payload: dict) -> dict:
        if not self.token:
            raise RuntimeError("GH_TOKEN 未配置")
        # 清理沙箱 editable 安装产生的 egg-info，避免混入提交
        for egg in (wt / "target-app").glob("*.egg-info"):
            shutil.rmtree(egg, ignore_errors=True)
        # 显式 add target-app（new_file 是 untracked，commit -am 会漏）
        add = _run(["git", "-C", str(wt), "add", "-A", "target-app"], timeout=30)
        if add.returncode != 0:
            raise RuntimeError(f"git add 失败: {add.stderr[-300:]}")
        commit = _run(["git", "-C", str(wt), "-c", "user.name=sentinel-aiops",
                       "-c", "user.email=aiops@sentinel.local",
                       "commit", "-m", f"fix(incident#{incident_id}): "
                       f"{str(payload.get('root_cause') or 'auto fix')[:60]}"],
                      timeout=60)
        if commit.returncode != 0:
            raise RuntimeError(f"commit 失败: {commit.stderr[-300:]}")
        push = _run(["git", "-C", str(wt), "push",
                     f"https://x-access-token:{self.token}@github.com/"
                     f"{self.github_repo}.git", f"HEAD:{branch}"], timeout=120)
        if push.returncode != 0:
            raise RuntimeError(f"push 失败: {push.stderr[-300:]}")

        body = (f"## 自动修复提案（Sentinel AIOps）\n\n"
                f"- Incident: #{incident_id}\n"
                f"- 根因: {payload.get('root_cause')}\n\n"
                f"> 人工确认：审阅后 Merge 本 PR，Sentinel 将自动部署并复测。")
        # 注意：方案 A 由人工在 GitHub 直接 Merge 确认，必须是普通 PR（draft 不能 merge）
        resp = httpx.post(
            f"{GITHUB_API}/repos/{self.github_repo}/pulls",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json"},
            json={"title": f"[aiops] fix: incident#{incident_id}",
                  "head": branch, "base": "main", "body": body},
            timeout=30)
        if resp.status_code != 201:
            raise RuntimeError(f"建 PR 失败 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return {"number": data["number"], "html_url": data["html_url"]}

    def _cleanup_worktree(self, wt: Path, branch: str) -> None:
        try:
            _run(["git", "-C", str(self.repo_dir), "worktree", "remove",
                  "--force", str(wt)], timeout=30)
        except Exception:  # noqa: BLE001
            shutil.rmtree(wt, ignore_errors=True)
        try:
            _run(["git", "-C", str(self.repo_dir), "branch", "-D", branch], timeout=30)
        except Exception:  # noqa: BLE001
            pass

    async def run(self) -> None:
        logger.info("FixWorker 启动 worker_id=%s repo=%s", self.worker_id,
                    self.github_repo)
        while True:
            task = await self._claim()
            if task is None:
                await asyncio.sleep(2.0)
                continue
            await self._handle(task)
