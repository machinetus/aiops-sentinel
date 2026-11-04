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


def test_http_probe_recover_uses_alert_error_class():
    """回归（2026-11-04 真机发现）：告警 http_unreachable / 恢复 http_5xx
    → 指纹不一致，firing 告警永远无法恢复关闭。"""
    from src.common.fingerprint import make_fingerprint

    state = HttpProbeState("demo-app")
    alert = None
    for _ in range(3):
        alert = state.evaluate(False, None, "ConnectError")
    assert alert.verdict == "alert" and alert.error_class == "http_unreachable"
    recover = state.evaluate(True, 200)
    recover2 = state.evaluate(True, 200)
    assert recover2.verdict == "recover"
    # 恢复指纹必须等于告警指纹
    assert make_fingerprint("probe:http", "demo-app", recover2.error_class) == \
        make_fingerprint("probe:http", "demo-app", alert.error_class)


def test_log_probe_recover_uses_alert_error_class():
    """回归：日志告警提取到 ValueError 时，恢复也必须用 ValueError。"""
    from src.common.fingerprint import make_fingerprint

    state = LogProbeState("demo-app", "ERROR|Traceback", rate_per_min=5,
                          interval_seconds=30)
    alert = state.evaluate(['{"level":"ERROR","msg":"ValueError: bad"}'] * 3)
    assert alert.error_class == "ValueError"
    recover = state.evaluate([])
    assert recover.verdict == "recover"
    assert make_fingerprint("probe:log", "demo-app", recover.error_class) == \
        make_fingerprint("probe:log", "demo-app", alert.error_class)


def test_log_probe_extracts_exception_from_escaped_json():
    """回归（2026-11-04 真机发现）：JSON 行内栈是转义存储（字面量 \\nValueError），
    原始行上 \\b 提取失配；必须 json.loads 后在 msg+exc 上提取。"""
    import json as _json

    state = LogProbeState("demo-app", "ERROR|Traceback", rate_per_min=5,
                          interval_seconds=30)
    line = _json.dumps({
        "level": "ERROR", "msg": "order failed",
        "exc": "Traceback (most recent call last):\n"
               "  File \"orders.py\", line 36, in create_order\n"
               "    total = int(amount) * price\n"
               "ValueError: invalid literal for int() with base 10: 'abc'",
    }, ensure_ascii=False)
    assert "\\nValueError" in line          # 前置条件：确实是转义形态
    r = state.evaluate([line] * 3)
    assert r.verdict == "alert"
    assert r.error_class == "ValueError"


def test_log_probe_tails_from_end_on_first_sight(tmp_path, monkeypatch):
    """回归（2026-11-04 真机发现）：monitor 重启后 offset=0 全量重读历史日志
    → 误告警。首次见到文件必须从末尾开始 tail。"""
    import asyncio

    from src.monitor.main import Monitor

    log = tmp_path / "app.log"
    log.write_text("old ERROR lines\n" * 20, encoding="utf-8")
    cfg = {"type": "log", "subject": "demo-app", "path": str(log),
           "error_pattern": "ERROR|Traceback", "rate_per_min": 5, "interval": 30}
    mon = Monitor([cfg], "http://127.0.0.1:9")
    events: list = []

    async def fake_alert(**kw):
        events.append(("alert", kw))

    async def fake_recover(fp, msg):
        events.append(("recover", fp))

    monkeypatch.setattr(mon, "report_alert", fake_alert)
    monkeypatch.setattr(mon, "report_recover", fake_recover)
    state = mon._make_state(cfg)

    asyncio.run(mon._tick_log("probe:log", "demo-app", state, cfg))
    assert events == []                                  # 历史错误不触发

    with open(log, "a", encoding="utf-8") as f:           # 追加新错误
        f.write("ERROR ValueError: fresh\n" * 3)
    asyncio.run(mon._tick_log("probe:log", "demo-app", state, cfg))
    assert len(events) == 1 and events[0][0] == "alert"
    assert events[0][1]["error_class"] == "ValueError"
