"""下单业务逻辑（场景二的缺陷载体）。

BUG_MODE（design §9.3，env 可开关）：
- type_bug：不校验 amount 类型直接 int() —— amount 为字符串时抛 ValueError → 500
- off     ：正常校验，非法输入返回 400（复测基线 / 修复后形态）
"""

from __future__ import annotations

import logging
import os
import uuid

from fastapi import HTTPException

logger = logging.getLogger("demo_app.orders")


def bug_mode() -> str:
    # 动态读取：便于测试切换；生产由 systemd Environment=BUG_MODE 固定
    return os.environ.get("BUG_MODE", "off")


def create_order(body: dict) -> dict:
    """处理下单请求。

    type_bug 模式下 amount 传字符串（如 "abc"）会在 int() 处抛 ValueError，
    栈帧定位到本文件 create_order —— 这正是 DiagWorker 根因卡片要指认的 orders 缺陷。
    """
    item = body.get("item", "unknown")
    amount = body.get("amount")
    price = body.get("price", 1)

    # 无论 BUG_MODE 如何，入参校验必须在业务计算之前完成：
    # 非整数（含字符串 "abc"、布尔值、None）一律 400，避免 int()/乘法抛 ValueError 变成 500。
    if isinstance(amount, bool) or not isinstance(amount, int):
        logger.warning(
            "invalid amount type",
            extra={"event": "invalid_amount", "amount_type": type(amount).__name__},
        )
        raise HTTPException(status_code=400, detail="amount must be an integer")
    try:
        total = amount * price
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid amount or price") from exc

    order = {"order_id": uuid.uuid4().hex[:12], "item": item, "amount": amount,
             "price": price, "total": total}
    logger.info("order created", extra={"event": "order_created", "order": order})
    return order
