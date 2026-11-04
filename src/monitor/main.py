"""Monitor 主循环（design §3）：四类探针 → Router 告警/恢复上报。

- 配置：/etc/aiops/probes.yaml（env AIOPS_PROBES 可覆盖）；缺省用内置 demo-app 探针集
- 上报：POST {router}/api/v1/alerts（新告警）与 /api/v1/alerts/resolve（恢复）
- Router URL：env AIOPS_ROUTER_URL 或 config monitor.router_url，缺省 http://127.0.0.1:18081
- 日志探针以文件 offset 增量读取，只统计新增错误行；命中行作为 evidence/触发样本上报
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
import yaml

from ..common.fingerprint import make_fingerprint
from ..common.log import setup_logging
from .probes import (HttpProbeState, LogProbeState, ProcessProbeState,
                     TrafficProbeState)

logger = logging.getLogger("sentinel.monitor")

DEFAULT_PROBES_YAML = "/etc/aiops/probes.yaml"
DEFAULT_ROUTER_URL = "http://127.0.0.1:18081"

# 缺省探针集（/etc/aiops/probes.yaml 缺失时兜底，开发机友好）
BUILTIN_PROBES = [
    {"type": "process", "subject": "demo-app", "interval": 15},
    {"type": "http", "subject": "demo-app", "url": "http://127.0.0.1:18000/health",
     "expect": 200, "consecutive_failures": 3, "interval": 15},
    {"type": "log", "subject": "demo-app", "path": "/var/log/demo-app/app.log",
     "error_pattern": "ERROR|Traceback", "rate_per_min": 5, "interval": 30},
    {"type": "traffic", "subject": "demo-app",
     "metrics_url": "http://127.0.0.1:18000/metrics", "qps_threshold": 50, "interval": 10},
]


def load_probes(path: str | None = None) -> list[dict]:
    probe_path = Path(path or os.environ.get("AIOPS_PROBES") or DEFAULT_PROBES_YAML)
    try:
        if probe_path.is_file():
            data = yaml.safe_load(probe_path.read_text(encoding="utf-8")) or {}
            probes = data.get("probes") or []
            if probes:
                return probes
    except Exception:  # noqa: BLE001 —— 配置损坏退回内置集
        logger.exception("probes.yaml 加载失败，使用内置探针集")
    return [dict(p) for p in BUILTIN_PROBES]


class Monitor:
    def __init__(self, probes_cfg: list[dict], router_url: str) -> None:
        self.router_url = router_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=8)
        self._tasks: list[asyncio.Task] = []
        self._states: dict[tuple, object] = {}          # (type, subject) -> 状态机
        self._log_offsets: dict[str, int] = {}          # log path -> offset
        self._cfg = probes_cfg

    # ---- 上报 -----------------------------------------------------------

    async def report_alert(self, *, source: str, severity: str, subject: str,
                           error_class: str | None, message: str,
                           payload: dict | None = None) -> None:
        body = {"source": source, "severity": severity, "subject": subject,
                "error_class": error_class, "message": message,
                "payload": payload or {}}
        try:
            resp = await self.client.post(f"{self.router_url}/api/v1/alerts", json=body)
            data = resp.json() if resp.status_code == 200 else {}
            logger.warning("告警上报 %s/%s/%s → %s", source, subject, error_class, data)
        except Exception:  # noqa: BLE001 —— Router 短暂不可用不应打断探针
            logger.exception("告警上报失败（Router 不可达？）")

    async def report_recover(self, fingerprint: str, message: str) -> None:
        try:
            resp = await self.client.post(f"{self.router_url}/api/v1/alerts/resolve",
                                          json={"fingerprint": fingerprint,
                                                "reason": message})
            logger.info("恢复上报 %s → HTTP %s %s", fingerprint[:12],
                        resp.status_code, resp.text[:120])
        except Exception:  # noqa: BLE001
            logger.exception("恢复上报失败")

    # ---- 各探针周期任务 ---------------------------------------------------

    async def _probe_loop(self, cfg: dict) -> None:
        ptype = cfg["type"]
        subject = cfg.get("subject", "demo-app")
        interval = int(cfg.get("interval", 15))
        source = f"probe:{ptype}"
        state = self._states.setdefault((ptype, subject), self._make_state(cfg))

        logger.info("探针启动 %s subject=%s interval=%ss", ptype, subject, interval)
        while True:
            try:
                if ptype == "process":
                    await self._tick_process(source, subject, state)
                elif ptype == "http":
                    await self._tick_http(source, subject, state, cfg)
                elif ptype == "log":
                    await self._tick_log(source, subject, state, cfg)
                elif ptype == "traffic":
                    await self._tick_traffic(source, subject, state, cfg)
                else:
                    logger.error("未知探针类型: %s", ptype)
                    return
            except Exception:  # noqa: BLE001 —— 单轮失败不影响后续周期
                logger.exception("探针 %s/%s 本轮异常", ptype, subject)
            await asyncio.sleep(interval)

    def _make_state(self, cfg: dict):
        ptype = cfg["type"]
        subject = cfg.get("subject", "demo-app")
        if ptype == "process":
            return ProcessProbeState(subject)
        if ptype == "http":
            return HttpProbeState(subject, int(cfg.get("consecutive_failures", 3)))
        if ptype == "log":
            return LogProbeState(subject, cfg.get("error_pattern", "ERROR|Traceback"),
                                 float(cfg.get("rate_per_min", 5)),
                                 int(cfg.get("interval", 30)))
        if ptype == "traffic":
            return TrafficProbeState(subject, float(cfg.get("qps_threshold", 50)))
        raise ValueError(f"未知探针类型: {ptype}")

    async def _tick_process(self, source, subject, state) -> None:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", "--quiet", f"{subject}.service",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        verdict = state.evaluate(proc.returncode == 0)  # type: ignore[attr-defined]
        await self._emit(source, subject, verdict, state)

    async def _tick_http(self, source, subject, state, cfg) -> None:
        url = cfg["url"]
        expect = int(cfg.get("expect", 200))
        status, err = None, ""
        try:
            resp = await self.client.get(url)
            status = resp.status_code
            ok = status == expect
        except Exception as exc:  # noqa: BLE001
            ok = False
            err = type(exc).__name__
        verdict = state.evaluate(ok, status, err)
        await self._emit(source, subject, verdict, state)

    async def _tick_log(self, source, subject, state, cfg) -> None:
        path = cfg["path"]
        new_errors: list[str] = []
        try:
            offset = self._log_offsets.get(path, 0)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size < offset:  # 日志轮转/截断 → 从头读
                offset = 0
            if size > offset:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read()
                self._log_offsets[path] = offset + len(chunk.encode("utf-8", "replace"))
                new_errors = [ln for ln in chunk.splitlines()
                              if state.pattern.search(ln)]  # type: ignore[attr-defined]
        except OSError:
            logger.warning("日志文件暂不可读: %s", path)
        verdict = state.evaluate(new_errors)
        await self._emit(source, subject, verdict, state)

    async def _tick_traffic(self, source, subject, state, cfg) -> None:
        qps = 0.0
        try:
            resp = await self.client.get(cfg["metrics_url"])
            qps = float(resp.json().get("qps", 0.0))
        except Exception:  # noqa: BLE001 —— 指标不可达按 0 处理
            pass
        verdict = state.evaluate(qps)
        await self._emit(source, subject, verdict, state)

    async def _emit(self, source, subject, verdict, state) -> None:
        if verdict.verdict == "none":
            return
        if verdict.verdict == "alert":
            await self.report_alert(
                source=source, severity=_severity(source), subject=subject,
                error_class=verdict.error_class, message=verdict.message,
                payload={"evidence": verdict.evidence or [], "value": verdict.value})
        else:  # recover
            fp = make_fingerprint(source, subject, verdict.error_class)
            await self.report_recover(fp, verdict.message)

    async def run(self) -> None:
        for cfg in self._cfg:
            self._tasks.append(asyncio.create_task(self._probe_loop(cfg)))
        await asyncio.gather(*self._tasks)


def _severity(source: str) -> str:
    return "critical" if source in ("probe:process", "probe:http") else "warning"


async def main_async() -> None:
    setup_logging()
    router_url = (os.environ.get("AIOPS_ROUTER_URL")
                  or DEFAULT_ROUTER_URL)
    probes_cfg = load_probes()
    logger.info("monitor 启动 router=%s probes=%d", router_url, len(probes_cfg))
    await Monitor(probes_cfg, router_url).run()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
