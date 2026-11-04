"""日志初始化与脱敏（PRD「约束与安全边界」：token/secret 掩码后才能出日志）。"""

from __future__ import annotations

import logging
import re

# 命中「键名: 值 / 键名=值 / Authorization: Bearer xxx」形态的敏感字段，值整体掩码。
# 注意（2026-11-04 单测发现的真 bug）：value 必须吃掉可选的 "Bearer " 前缀，
# 否则只会脱掉 "Bearer" 一词、把真实 token 留在日志里。
SECRET_PATTERN = re.compile(
    r"(?i)\b(token|secret|password|authorization|api[_-]?key)\b\s*[:=]?\s*"
    r"(?:bearer\s+)?\S+"
)
_MASK = r"\1=***"


class RedactFilter(logging.Filter):
    """对所有经过的日志记录做敏感值掩码；失败不阻断日志输出。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            if SECRET_PATTERN.search(message):
                record.msg = SECRET_PATTERN.sub(_MASK, message)
                record.args = None
        except Exception:  # noqa: BLE001 —— 脱敏失败时保底输出原始日志
            pass
        return True


def setup_logging(level: str = "INFO") -> None:
    """幂等的根日志初始化（Router/Monitor/Worker 共用）。"""
    root = logging.getLogger()
    if getattr(root, "_sentinel_configured", False):
        return
    handler = logging.StreamHandler()
    handler.addFilter(RedactFilter())
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler],
    )
    root._sentinel_configured = True  # type: ignore[attr-defined]
