"""日志脱敏单测（PRD 安全边界：token/secret 不得明文出日志）。"""

from __future__ import annotations

import logging

from src.common.log import RedactFilter


def _make_record(msg: str, args: tuple = ()) -> logging.LogRecord:
    return logging.LogRecord(name="t", level=logging.INFO, pathname="",
                             lineno=0, msg=msg, args=args, exc_info=None)


def test_redact_token_assignment():
    record = _make_record("call failed token=sk-abcdef123")
    RedactFilter().filter(record)
    assert "sk-abcdef123" not in record.getMessage()
    assert "token=***" in record.getMessage()


def test_redact_authorization_header():
    record = _make_record("Authorization: Bearer abcdef123456")
    RedactFilter().filter(record)
    assert "abcdef123456" not in record.getMessage()


def test_redact_no_secret_untouched():
    record = _make_record("normal log line without secrets")
    RedactFilter().filter(record)
    assert record.getMessage() == "normal log line without secrets"
