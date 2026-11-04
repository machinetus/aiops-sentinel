"""配置加载（design §9.2）。

分层约定：
- /etc/aiops/config.yaml       全局配置（bootstrap.sh 从 config/*.yaml.example 复制）
- 环境变量 AIOPS_CONFIG        可覆盖配置文件路径（测试 / 开发机用）
- 密钥不入本文件               统一走 /etc/aiops/secrets.env → systemd 注入环境变量

兼容性：配置文件里出现未知键不报错（向前兼容，方便增量加字段）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "/etc/aiops/config.yaml"


@dataclass
class RouterConfig:
    """router 段：监听与任务租约（design §1.2 / §4.3）。"""

    host: str = "127.0.0.1"          # 安全边界：只绑回环
    port: int = 18081                # 18081：目标 VM 上 8081 已被既有服务占用（2026-11-04 部署确认）
    lease_seconds: int = 300         # 任务认领租约
    claim_max_attempts: int = 2      # 租约过期重派次数上限


@dataclass
class AlertConfig:
    """alert 段：告警降噪窗口（design §3.2）。"""

    dedupe_window_seconds: int = 300


@dataclass
class MonitorConfig:
    """monitor 段：感知面上报地址（design §3）。"""

    router_url: str = "http://127.0.0.1:18081"


@dataclass
class LlmConfig:
    """llm 段（阶段 4 生效）。"""

    enabled: bool = False
    base_url: str = ""
    model: str = ""
    timeout_seconds: int = 60
    temperature: float = 0.1


@dataclass
class WorkerConfig:
    """worker 段：三角色开关（全关 = 只告警不处置，最快的回滚位）。"""

    ops_enabled: bool = False
    diag_enabled: bool = False
    fix_enabled: bool = False


@dataclass
class FeishuConfig:
    """feishu 段（阶段 2 生效；凭据读环境变量，不落配置文件）。"""

    enabled: bool = False


@dataclass
class AppConfig:
    router: RouterConfig = field(default_factory=RouterConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    raw: dict[str, Any] = field(default_factory=dict)  # 原始 YAML，供后续模块取扩展字段


def _section(data: dict[str, Any], key: str, cls: type) -> Any:
    """按 dataclass 字段过滤后构造配置段；未知键忽略并告警。"""
    known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    section = data.get(key) or {}
    unknown = set(section) - known
    if unknown:
        # 延迟 import 避免循环依赖：config 在 logging 初始化前加载
        import logging

        logging.getLogger("sentinel.config").warning(
            "config[%s] 存在未知配置项（已忽略）: %s", key, sorted(unknown)
        )
    return cls(**{k: v for k, v in section.items() if k in known})


def load_config(path: str | None = None) -> AppConfig:
    """加载配置；文件缺失/为空时全部走默认值（开发机可零配置运行）。"""
    config_path = Path(path or os.environ.get("AIOPS_CONFIG") or DEFAULT_CONFIG_PATH)
    data: dict[str, Any] = {}
    try:
        if config_path.is_file():
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 —— 配置损坏不应阻断启动，退回默认值
        import logging

        logging.getLogger("sentinel.config").exception(
            "配置文件加载失败，使用默认配置: %s", config_path
        )
    return AppConfig(
        router=_section(data, "router", RouterConfig),
        alert=_section(data, "alert", AlertConfig),
        monitor=_section(data, "monitor", MonitorConfig),
        llm=_section(data, "llm", LlmConfig),
        worker=_section(data, "worker", WorkerConfig),
        feishu=_section(data, "feishu", FeishuConfig),
        raw=data,
    )
