"""探针状态机（纯逻辑，无 IO，design §3.1/§3.2）。

每个探针维护 active 标志与防抖计数：
- 防抖告警：连续 N 次异常才上报（HTTP 默认 3，进程/日志/流量 1 次即报）
- 恢复判定：连续 2 次正常才发恢复（避免抖动反复）
evaluate() 返回 verdict：alert / recover / none 三态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Verdict = Literal["alert", "recover", "none"]

# 常见 Python 异常类 → error_class（日志探针归类用）
_EXCEPTION_RE = re.compile(
    r"\b(ValueError|TypeError|KeyError|IndexError|ZeroDivisionError|"
    r"AttributeError|NameError|RuntimeError|ConnectionError|TimeoutError)\b"
)


@dataclass
class ProbeResult:
    verdict: Verdict
    error_class: str | None = None
    message: str = ""
    evidence: list[str] | None = None   # 日志探针：命中的错误行（含触发样本）
    value: float | None = None          # 流量探针：当前 QPS


class ProcessProbeState:
    """systemd is-active 判定（只读查询，无需 sudo）。"""

    def __init__(self, subject: str) -> None:
        self.subject = subject
        self._active = False

    def evaluate(self, is_active: bool) -> ProbeResult:
        if not is_active and not self._active:
            self._active = True
            return ProbeResult("alert", error_class="process_dead",
                               message=f"{self.subject} 进程退出/未运行")
        if is_active and self._active:
            self._active = False
            return ProbeResult("recover", error_class="process_dead")
        return ProbeResult("none", error_class="process_dead")


class HttpProbeState:
    """HTTP 探针：连续 N 次失败才告警（防抖），连续 2 次成功才恢复。"""

    def __init__(self, subject: str, consecutive_failures: int = 3) -> None:
        self.subject = subject
        self.consecutive_failures = consecutive_failures
        self._fails = 0
        self._oks = 0
        self._active = False

    def evaluate(self, ok: bool, status: int | None = None, err: str = "") -> ProbeResult:
        if ok:
            self._fails = 0
            self._oks += 1
            if self._active and self._oks >= 2:
                self._active = False
                return ProbeResult("recover", error_class="http_5xx",
                                   message=f"{self.subject} HTTP 恢复")
            return ProbeResult("none", error_class="http_5xx")
        self._oks = 0
        self._fails += 1
        error_class = "http_5xx" if status and status >= 500 else "http_unreachable"
        if not self._active and self._fails >= self.consecutive_failures:
            self._active = True
            return ProbeResult("alert", error_class=error_class,
                               message=f"{self.subject} HTTP 连续 {self._fails} 次失败"
                                       f"（status={status} {err}）")
        return ProbeResult("none", error_class=error_class)


class LogProbeState:
    """日志探针：错误行速率超阈值告警；error_class 从命中行提取异常类。"""

    def __init__(self, subject: str, error_pattern: str, rate_per_min: float,
                 interval_seconds: int) -> None:
        self.subject = subject
        self.pattern = re.compile(error_pattern)
        self.rate_per_min = rate_per_min
        self.interval = max(interval_seconds, 1)
        self._active = False

    def evaluate(self, new_error_lines: list[str]) -> ProbeResult:
        rate = len(new_error_lines) * (60.0 / self.interval)
        error_class = None
        for line in new_error_lines:
            m = _EXCEPTION_RE.search(line)
            if m:
                error_class = m.group(1)
                break
        if rate >= self.rate_per_min and not self._active:
            self._active = True
            return ProbeResult(
                "alert", error_class=error_class or "log_error_rate",
                message=f"{self.subject} 日志错误率 {rate:.0f}/min ≥ 阈值 {self.rate_per_min}/min",
                evidence=new_error_lines[-5:], value=rate)
        if rate < self.rate_per_min and self._active:
            self._active = False
            return ProbeResult("recover", error_class=error_class or "log_error_rate",
                               message=f"{self.subject} 日志错误率回落 {rate:.0f}/min")
        return ProbeResult("none", error_class=error_class)


class TrafficProbeState:
    """流量探针：QPS 超阈值告警（warning），回落恢复。"""

    def __init__(self, subject: str, qps_threshold: float) -> None:
        self.subject = subject
        self.threshold = qps_threshold
        self._active = False

    def evaluate(self, qps: float) -> ProbeResult:
        if qps > self.threshold and not self._active:
            self._active = True
            return ProbeResult("alert", error_class="qps_over",
                               message=f"{self.subject} QPS {qps:.0f} > {self.threshold:.0f}"
                                       "，疑似异常流量",
                               value=qps)
        if qps <= self.threshold and self._active:
            self._active = False
            return ProbeResult("recover", error_class="qps_over",
                               message=f"{self.subject} QPS 回落 {qps:.0f}", value=qps)
        return ProbeResult("none", error_class="qps_over", value=qps)
