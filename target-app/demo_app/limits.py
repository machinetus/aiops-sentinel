"""限流 / 封禁注册表（demo-app 侧，供 OpsWorker 经 admin API 控制，design §5.1/§10.3）。

语义：
- block_ip：拒绝该 IP 一切业务请求（403），到期自动解除
- rate_limit：全局限流 qps（超出返回 429），到期自动解除
- 到期即失效：check_* 时惰性清除过期条目（demo 语义，重启后状态清零）
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time

logger = logging.getLogger("demo_app.limits")

STATE_FILE = "/run/demo-app/state.json"  # systemd RuntimeDirectory=demo-app 提供


class LimitsRegistry:
    """进程内单例：demo_app.main 与 demo_app.admin 共用 limits.registry 实例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocked: dict[str, float] = {}                      # ip -> 过期时间戳
        self._rate: dict | None = None                            # {"qps": int, "until": ts}
        self._recent: collections.deque[float] = collections.deque()   # 限流 1s 滑窗
        self._qps_window: collections.deque[float] = collections.deque()  # /metrics 10s 滑窗

    # ---- 写操作（admin API 调用） ----------------------------------------

    def block_ip(self, ip: str, duration_min: float = 10) -> dict:
        with self._lock:
            self._blocked[ip] = time.time() + duration_min * 60
            self._persist()
        logger.warning("ip blocked", extra={"event": "ip_blocked", "ip": ip,
                                            "duration_min": duration_min})
        return self.snapshot()

    def unblock_ip(self, ip: str) -> dict:
        with self._lock:
            self._blocked.pop(ip, None)
            self._persist()
        logger.info("ip unblocked", extra={"event": "ip_unblocked", "ip": ip})
        return self.snapshot()

    def set_rate_limit(self, qps: int, duration_min: float = 5) -> dict:
        with self._lock:
            self._rate = {"qps": qps, "until": time.time() + duration_min * 60}
            self._persist()
        logger.warning("rate limit set", extra={"event": "rate_limit_set",
                                                "qps": qps, "duration_min": duration_min})
        return self.snapshot()

    def clear_rate_limit(self) -> dict:
        with self._lock:
            self._rate = None
            self._persist()
        logger.info("rate limit cleared", extra={"event": "rate_limit_cleared"})
        return self.snapshot()

    # ---- 判定（业务中间件每请求调用） ------------------------------------

    def check_ip(self, ip: str, now: float | None = None) -> bool:
        """True = 该 IP 被封禁。惰性清除过期条目（到期自动解除）。"""
        now = now or time.time()
        with self._lock:
            expired = [k for k, exp in self._blocked.items() if exp <= now]
            for k in expired:
                del self._blocked[k]
                logger.info("block expired", extra={"event": "ip_unblocked", "ip": k})
            if expired:
                self._persist()
            return ip in self._blocked

    def check_rate(self, now: float | None = None) -> bool:
        """True = 触发限流（应返回 429）。到期自动解除并清窗。"""
        now = now or time.time()
        with self._lock:
            if self._rate is None:
                return False
            if self._rate["until"] <= now:
                logger.info("rate limit expired", extra={"event": "rate_limit_expired"})
                self._rate = None
                self._recent.clear()
                self._persist()
                return False
            self._recent.append(now)
            while self._recent and self._recent[0] < now - 1.0:
                self._recent.popleft()
            return len(self._recent) > self._rate["qps"]

    def note_request(self, now: float | None = None) -> None:
        """QPS 指标采样（/metrics 用，与限流判定独立）。"""
        self._qps_window.append(now or time.time())

    # ---- 观测 ------------------------------------------------------------

    def qps(self) -> float:
        """最近 10 秒平均 QPS（/metrics 暴露给流量探针）。"""
        now = time.time()
        while self._qps_window and self._qps_window[0] < now - 10.0:
            self._qps_window.popleft()
        return round(len(self._qps_window) / 10.0, 2)

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "blocked_ips": {ip: round(exp - now, 1) for ip, exp in self._blocked.items()
                            if exp > now},
            "rate_limit": ({"qps": self._rate["qps"],
                            "expires_in": round(self._rate["until"] - now, 1)}
                           if self._rate and self._rate["until"] > now else None),
        }

    # ---- 状态落盘（design §1.2 /run/demo-app/state.json，尽力而为） ------

    def _persist(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f, ensure_ascii=False)
        except OSError:
            pass  # /run 不可写等场景静默跳过，不影响业务


registry = LimitsRegistry()
