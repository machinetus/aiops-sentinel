"""告警指纹（design §3.2 降噪的第一步）。

design §3.2 原文口径为 sha256(source|probe|subject|error_class)；
实现层面 source 已内含探针类别（如 probe:http、webhook、manual），
因此 probe 维度并入 source，指纹字段与 alert 表四列一一对应。
"""

from __future__ import annotations

import hashlib


def make_fingerprint(source: str, subject: str, error_class: str | None) -> str:
    """确定性指纹：任一字段不同 → 不同指纹（分属不同告警/事件）。"""
    raw = f"{source.strip()}|{subject.strip()}|{(error_class or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
