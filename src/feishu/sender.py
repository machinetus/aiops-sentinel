"""飞书群自定义机器人出站发送器（design §6.1）。

- webhook + 加签（HMAC-SHA256，算法按飞书官方：key=timestamp+"\n"+secret，msg 空）
- 凭据从环境变量读取（FEISHU_WEBHOOK_URL / FEISHU_WEBHOOK_SECRET），不落配置文件
- 未配置 / config.feishu.enabled=false → 静默降级为日志（演示不中断）
- 10s 超时、失败重试 1 次；仍失败仅记审计日志，不阻断主流程
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time

import httpx

logger = logging.getLogger("sentinel.feishu")

WEBHOOK_ENV = "FEISHU_WEBHOOK_URL"
SECRET_ENV = "FEISHU_WEBHOOK_SECRET"


def sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人加签算法（官方口径）。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuSender:
    def __init__(self, webhook_url: str, secret: str) -> None:
        self.webhook_url = webhook_url
        self.secret = secret
        self._client = httpx.AsyncClient(timeout=10)

    @classmethod
    def from_env(cls) -> "FeishuSender | None":
        """按 config.feishu.enabled + 环境变量装配；未就绪返回 None（调用方跳过发送）。"""
        url = os.environ.get(WEBHOOK_ENV, "").strip()
        secret = os.environ.get(SECRET_ENV, "").strip()
        if not url or not secret:
            return None
        return cls(url, secret)

    async def send(self, card: dict, *, retries: int = 1) -> bool:
        """发送交互卡片；payload 按「加签」模式组装。返回是否成功。

        注意（2026-11-04 真机踩坑）：timestamp 与 sign 必须用**同一个** ts 计算——
        两次独立 time.time() 在跨秒瞬间会产生不匹配签名，飞书报
        "sign match fail or timestamp is not within one hour"。
        """
        for attempt in range(retries + 1):
            ts = int(time.time())
            payload = {
                "timestamp": str(ts),
                "sign": sign(self.secret, ts),
                "msg_type": "interactive",
                "card": card,
            }
            try:
                resp = await self._client.post(self.webhook_url, json=payload)
                data = resp.json()
                # 飞书返回 {"code":0,...} 视为成功（旧版为 {"StatusCode":0}）
                ok = data.get("code", data.get("StatusCode", -1)) == 0
                if ok:
                    return True
                logger.warning("飞书卡片被拒: %s", data)
            except Exception:  # noqa: BLE001 —— 网络抖动重试一次
                logger.exception("飞书发送异常（attempt=%d）", attempt + 1)
            if attempt < retries:
                time.sleep(1)
        return False


_sender: FeishuSender | None = None


def get_sender(enabled: bool) -> FeishuSender | None:
    """模块级单例；enabled=False 或凭据缺失 → None（降级为不发送）。"""
    global _sender
    if not enabled:
        return None
    if _sender is None:
        _sender = FeishuSender.from_env()
        if _sender is None:
            logger.warning("feishu.enabled=true 但 %s/%s 未配置，出站降级为仅日志",
                           WEBHOOK_ENV, SECRET_ENV)
    return _sender
