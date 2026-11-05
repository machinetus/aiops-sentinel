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

    if bug_mode() == "type_bug":
        # 故意缺陷：直接类型转换，无校验（修复 PR 将在此处加校验 + 回归测试）
        total = int(amount) * price
    else:
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise HTTPException(status_code=400, detail="amount must be an integer")
        total = amount * price

    order = {"order_id": uuid.uuid4().hex[:12], "item": item, "amount": amount,
             "price": price, "total": total}
    logger.info("order created", extra={"event": "order_created", "order": order})
    return order
