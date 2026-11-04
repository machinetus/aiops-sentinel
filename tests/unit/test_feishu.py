"""飞书发送器与卡片单测：加签算法 / 时间戳一致性 / 降级逻辑 / 卡片结构。"""

from __future__ import annotations

import asyncio

from src.feishu.cards import alert_card, pr_confirm_card, timeline_card
from src.feishu.sender import FeishuSender, get_sender, sign


class _FakeResp:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, url: str, json: dict) -> _FakeResp:  # noqa: A002
        self.calls.append(json)
        return _FakeResp({"code": 0})


def test_send_uses_same_timestamp_for_sign(monkeypatch):
    """回归（2026-11-04 真机踩坑）：timestamp 与 sign 必须同源 ts，
    两次独立 time.time() 跨秒会造成签名不匹配（飞书 code=19021）。"""
    import time as time_mod

    monkeypatch.setattr(time_mod, "time", lambda: 1700000000.9)
    sender = FeishuSender("http://example.invalid/hook", "my-secret")
    fake = _FakeClient()
    sender._client = fake  # type: ignore[assignment]
    assert asyncio.run(sender.send({"elements": []})) is True
    payload = fake.calls[0]
    assert payload["timestamp"] == "1700000000"
    assert payload["sign"] == sign("my-secret", 1700000000)


def test_sign_is_stable_base64():
    s1 = sign("my-secret", 1700000000)
    s2 = sign("my-secret", 1700000000)
    assert s1 == s2
    assert len(s1) >= 20  # base64(HMAC-SHA256) ≈ 28 字符
    assert sign("other", 1700000000) != s1


def test_get_sender_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("FEISHU_WEBHOOK_SECRET", "s")
    assert get_sender(enabled=False) is None


def test_get_sender_enabled_but_missing_env_warns_and_none(monkeypatch):
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    assert get_sender(enabled=True) is None


def test_alert_card_structure():
    card = alert_card(subject="demo-app", severity="critical",
                      error_class="process_dead", message="进程退出",
                      count=3, alert_id=7, evidence=["line1", "line2"])
    assert card["header"]["template"] == "red"
    text = str(card["elements"])
    assert "demo-app" in text and "告警计数" in text and "alert_id=7" in text


def test_pr_confirm_card_has_buttons():
    card = pr_confirm_card(incident_id=12, patch_id=13, root_cause="类型未校验",
                           trigger_sample="POST /api/orders", pr_number=13,
                           pr_url="https://github.com/x/pull/13",
                           diff_stat="+14 −2", test_summary="8 passed")
    actions = card["elements"][-2]["actions"]
    values = [a["value"]["act"] for a in actions]
    assert values == ["pr_confirm", "pr_reject"]


def test_timeline_card_rows():
    card = timeline_card(incident_id=1, title="t", state="resolved",
                         rows=["15:00 告警", "15:01 重启"])
    assert "15:01 重启" in str(card["elements"])
