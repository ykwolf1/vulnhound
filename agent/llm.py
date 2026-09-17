"""DeepSeek LLM 客户端（OpenAI 兼容 /chat/completions，原生 tool calling）。"""

import asyncio
import logging

logger = logging.getLogger("svh.llm")
import json
from dataclasses import dataclass, field

import httpx

__all__ = ["DeepSeekLLM", "LLMError", "LLMResp"]

_RETRY_DELAYS = (1, 2)  # 重试 2 次


class LLMError(Exception):
    """LLM 调用最终失败（重试耗尽）。"""


@dataclass
class LLMResp:
    content: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    reasoning_content: str | None = None  # DeepSeek 思考模式：回传 API 必需


def _parse_tool_calls(raw: list[dict]) -> list[dict]:
    calls = []
    for c in raw or []:
        fn = c.get("function", {})
        args = fn.get("arguments")
        try:
            arguments = json.loads(args) if isinstance(args, str) else (args or {})
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        calls.append({"id": c.get("id", ""), "name": fn.get("name", ""), "arguments": arguments})
    return calls


class DeepSeekLLM:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResp:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"
        last_exc: Exception | None = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            if attempt:
                await asyncio.sleep(_RETRY_DELAYS[attempt - 1])
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    return LLMResp(
                        content=msg.get("content"),
                        tool_calls=_parse_tool_calls(msg.get("tool_calls")),
                        reasoning_content=msg.get("reasoning_content"),
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = LLMError(f"http {resp.status_code}: {resp.text[:200]}")
                    continue
                logger.error("LLM http %s: %s", resp.status_code, resp.text[:200])
                raise LLMError(f"http {resp.status_code}: {resp.text[:200]}")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = LLMError(f"transport: {exc}")
                continue
        logger.error("LLM call failed after retries: %s", last_exc)
        raise LLMError(f"llm call failed after retries: {last_exc}")
