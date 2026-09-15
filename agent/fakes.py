"""测试替身：按脚本顺序响应的 FakeLLM。"""

from agent.llm import LLMResp

__all__ = ["FakeLLM"]


class FakeLLM:
    def __init__(self, script: list[LLMResp | Exception]):
        self.script = list(script)

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResp:
        if not self.script:
            raise AssertionError("FakeLLM script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
