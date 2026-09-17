"""Verdict 报告 schema（pydantic）。"""

from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["Direction", "Discarded", "Finding", "Verdict"]


class Evidence(BaseModel):
    request_id: int
    why: str = Field(default="", max_length=200)  # P2：该请求证明了什么（强制模型自查相关性）


class Finding(BaseModel):
    title: str
    severity: Literal["high", "medium", "low"]
    rationale: str = Field(max_length=300)
    evidence: list[int | Evidence]

    @property
    def evidence_ids(self) -> list[int]:
        return [e if isinstance(e, int) else e.request_id for e in self.evidence]


class Direction(BaseModel):
    id: str
    name: str
    hypothesis: str
    status: Literal["exploring", "concluded", "abandoned"] = "exploring"
    steps_used: int = 0
    outcome: str | None = None
    budget_reminded: bool = False


class Discarded(BaseModel):
    title: str
    reason: str


class Verdict(BaseModel):
    findings: list[Finding]
    discarded: list[Discarded]
