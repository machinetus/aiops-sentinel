"""白名单动作执行器（三重门的第三重：运行时校验；design §5.1 / §8）。

安全铁律：
- 只接受 actions.yaml 中定义的 action 名；命令 / URL / body 一律取自配置，
  绝不接受任务载荷里的命令字符串，绝不使用 shell 拼接
- 参数插值只允许配置中声明过的占位符（如 {ip}），且必须通过格式校验（IPv4）
- exec 型以数组形式 subprocess 执行（无 shell）；http 型仅打 127.0.0.1 admin API
- 任何越权请求抛 WhitelistDenied，由 Worker 记录 whitelist_deny 审计
"""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import yaml

logger = logging.getLogger("sentinel.worker.whitelist")

DEFAULT_ACTIONS_PATH = "/etc/aiops/actions.yaml"

# exec 型命令允许的首项前缀（sudoers 精确授权的镜像兜底；可注入便于测试）
ALLOWED_EXEC_PREFIXES = ("/usr/bin/sudo", "/usr/bin/systemctl", "/bin/true")

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class WhitelistDenied(Exception):
    """越权/未定义动作：拒绝执行并留审计。"""


@dataclass
class ExecOutcome:
    ok: bool
    kind: str                     # exec | http
    detail: dict = field(default_factory=dict)
    duration_s: float = 0.0


def _placeholders(value: Any) -> set[str]:
    """递归提取配置体中声明的占位符名。"""
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= _placeholders(v)
    elif isinstance(value, list):
        for v in value:
            found |= _placeholders(v)
    elif isinstance(value, str):
        found |= set(_PLACEHOLDER_RE.findall(value))
    return found


def _validate_param(name: str, value: Any) -> Any:
    """参数格式校验（白名单字段逐个校验，不通过即拒绝）。"""
    if name == "ip":
        try:
            return str(ipaddress.ip_address(str(value)))
        except ValueError as exc:
            raise WhitelistDenied(f"ip 参数非法: {value!r}") from exc
    if isinstance(value, (str, int, float)):
        return value
    raise WhitelistDenied(f"参数类型非法: {name}")


def _fill(value: Any, params: dict[str, Any]) -> Any:
    """用已校验参数填充占位符（仅配置声明的键，拒绝额外参数）。"""
    if isinstance(value, dict):
        return {k: _fill(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, params) for v in value]
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in params:
                raise WhitelistDenied(f"缺少参数: {key}")
            return str(params[key])
        return _PLACEHOLDER_RE.sub(repl, value)
    return value


class WhitelistExecutor:
    def __init__(self, actions: dict[str, dict],
                 allowed_prefixes: tuple[str, ...] | list[str] | None = None) -> None:
        self.actions = actions
        self.allowed_prefixes = tuple(allowed_prefixes or ALLOWED_EXEC_PREFIXES)

    @classmethod
    def from_file(cls, path: str = DEFAULT_ACTIONS_PATH) -> "WhitelistExecutor":
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except OSError:
            logger.warning("actions.yaml 不可读，白名单为空: %s", path)
            data = {}
        return cls(data.get("actions") or {})

    # ---- 执行 -----------------------------------------------------------

    def execute(self, action_name: str, params: dict[str, Any] | None = None) -> ExecOutcome:
        params = params or {}
        cfg = self.actions.get(action_name)
        if cfg is None:
            raise WhitelistDenied(f"未定义动作: {action_name}")

        # 运行时二次校验：参数只能是配置声明过的占位符
        declared = _placeholders(cfg.get("body")) | _placeholders(cfg.get("command"))
        extra = set(params) - declared
        if extra:
            raise WhitelistDenied(f"未声明参数: {sorted(extra)}")
        validated = {k: _validate_param(k, v) for k, v in params.items()}

        kind = cfg.get("type")
        started = time.monotonic()
        if kind == "exec":
            cmd = cfg.get("command") or []
            if not cmd or not all(isinstance(p, str) and p for p in cmd):
                raise WhitelistDenied(f"动作 {action_name} 的命令配置非法")
            if cmd[0] not in self.allowed_prefixes:
                # 只允许绝对路径白名单前缀（配置审查之外的兜底）
                raise WhitelistDenied(f"命令前缀不在允许范围: {cmd[0]}")
            proc = subprocess.run(  # noqa: S603 —— 数组执行、无 shell、命令来自白名单配置
                cmd, capture_output=True, text=True,
                timeout=float(cfg.get("timeout_seconds", 60)),
            )
            detail = {"command": " ".join(cmd), "exit_code": proc.returncode,
                      "output_tail": (proc.stdout + proc.stderr).strip()[-500:]}
            return ExecOutcome(proc.returncode == 0, "exec", detail,
                               round(time.monotonic() - started, 2))

        if kind == "http":
            url = cfg.get("url", "")
            if not url.startswith("http://127.0.0.1:"):
                # 安全边界：只能打本机 admin API
                raise WhitelistDenied(f"http 动作仅允许 127.0.0.1: {url}")
            body = _fill(cfg.get("body") or {}, validated)
            try:
                resp = httpx.post(url, json=body,
                                  timeout=float(cfg.get("timeout_seconds", 10)))
                detail = {"url": url, "request": body, "status_code": resp.status_code,
                          "response_tail": resp.text[:300]}
                ok = resp.status_code == 200
            except Exception as exc:  # noqa: BLE001 —— 网络异常按失败返回
                detail = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
                ok = False
            return ExecOutcome(ok, "http", detail, round(time.monotonic() - started, 2))

        raise WhitelistDenied(f"未知动作类型: {kind}")

    # ---- 复检（design D7：动作后自证恢复） ------------------------------

    def verify(self, action_name: str) -> dict:
        cfg = self.actions.get(action_name) or {}
        vcfg = cfg.get("verify")
        if not vcfg:
            return {"attempts": 0, "passed": True, "note": "无复检配置（执行成功即视为完成）"}
        attempts = int(cfg.get("verify_attempts", 3))
        interval = float(cfg.get("verify_interval", 10))
        url = vcfg.get("url", "")
        expect = int(vcfg.get("expect", 200))
        last_error = None
        for i in range(1, attempts + 1):
            try:
                resp = httpx.get(url, timeout=5)
                if resp.status_code == expect:
                    return {"attempts": i, "passed": True}
                last_error = f"status={resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_error = type(exc).__name__
            if i < attempts:
                time.sleep(interval)
        return {"attempts": attempts, "passed": False, "error": last_error}
