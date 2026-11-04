"""探针状态机单测：防抖 / 恢复 / 错误类提取 / QPS 阈值（design §3.1/§3.2）。"""

from __future__ import annotations

from src.monitor.probes import (HttpProbeState, LogProbeState, ProcessProbeState,
                                TrafficProbeState)


def test_http_probe_requires_consecutive_failures():
    state = HttpProbeState("demo-app", consecutive_failures=3)
    assert state.evaluate(False, 503).verdict == "none"      # 第 1 次失败：防抖
    assert state.evaluate(False, 503).verdict == "none"      # 第 2 次
    r = state.evaluate(False, 503)                           # 第 3 次 → 告警
    assert r.verdict == "alert" and r.error_class == "http_5xx"
    assert state.evaluate(True, 200).verdict == "none"       # 恢复 1 次：再观察
    assert state.evaluate(True, 200).verdict == "recover"    # 连续 2 次 → 恢复


def test_http_probe_unreachable_error_class():
    state = HttpProbeState("demo-app")
    for _ in range(3):
        r = state.evaluate(False, None, "ConnectError")
    assert r.verdict == "alert" and r.error_class == "http_unreachable"


def test_process_probe_alert_then_recover():
    state = ProcessProbeState("demo-app")
    assert state.evaluate(False).verdict == "alert"
    assert state.evaluate(False).verdict == "none"           # 已 active 不重复告警
    assert state.evaluate(True).verdict == "recover"
    assert state.evaluate(True).verdict == "none"


def test_log_probe_rate_and_error_class():
    state = LogProbeState("demo-app", "ERROR|Traceback", rate_per_min=5,
                          interval_seconds=30)
    lines = [f'{{"level":"ERROR","msg":"ValueError: invalid literal for int()"}}'] * 3
    r = state.evaluate(lines)   # 3 条/30s = 6/min ≥ 5 → 告警
    assert r.verdict == "alert"
    assert r.error_class == "ValueError"
    assert r.evidence == lines[-5:]
    assert state.evaluate([]).verdict == "recover"


def test_log_probe_below_threshold_no_alert():
    state = LogProbeState("demo-app", "ERROR|Traceback", rate_per_min=5,
                          interval_seconds=30)
    assert state.evaluate(["ERROR x"]).verdict == "none"     # 2/min < 5


def test_traffic_probe_threshold():
    state = TrafficProbeState("demo-app", qps_threshold=50)
    assert state.evaluate(30).verdict == "none"
    r = state.evaluate(80)
    assert r.verdict == "alert" and r.value == 80
    assert state.evaluate(80).verdict == "none"              # 不重复
    assert state.evaluate(10).verdict == "recover"
