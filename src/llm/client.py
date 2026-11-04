"""LLM 客户端（design §7）：OpenAI 兼容 chat/completions。

约定：
- 凭据 / 端点 / 模型全部来自环境变量（secrets.env 经 systemd 注入）：
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
- 代理走 httpx trust_env（systemd 单元已注入 HTTP(S)_PROXY，NO_PROXY 含回环）
- 失败语义：HTTP 错误 / 解析错误一律抛 LlmError，由调用方 fail-safe 转人工，
  不静默降级、不无限重试（单次请求最多尝试 2 次）
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger("sentinel.llm")


class LlmError(Exception):
    """LLM 调用/解析失败（调用方转人工）。"""


class LlmClient:
    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout: float = 60.0, temperature: float = 0.1) -> None:
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.temperature = temperature
        self._client = httpx.AsyncClient(timeout=timeout)

    @classmethod
    def from_env(cls, *, timeout: float = 60.0, temperature: float = 0.1) -> "LlmClient":
        client = cls(timeout=timeout, temperature=temperature)
        missing = [name for name, value in (
            ("LLM_BASE_URL", client.base_url), ("LLM_API_KEY", client.api_key),
            ("LLM_MODEL", client.model)) if not value]
        if missing:
            raise LlmError(f"LLM 配置缺失: {', '.join(missing)}")
        return client

    async def complete(self, system_prompt: str, user_content: str) -> str:
        """调用 chat/completions，返回助手文本（调用方自行 JSON 解析）。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers)
                if resp.status_code != 200:
                    raise LlmError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise LlmError("空响应")
                return content
            except (httpx.HTTPError, KeyError, IndexError, TypeError, LlmError) as exc:
                last_error = exc
                logger.warning("LLM 调用失败（attempt=%d）: %s", attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(1)
        raise LlmError(str(last_error))
