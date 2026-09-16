"""Verdict 报告 schema（pydantic）。"""

from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["Direction", "Discarded", "Finding", "Verdict"]


class Finding(BaseModel):
    title: str
    severity: Literal["high", "medium", "low"]
    rationale: str = Field(max_length=300)
    evidence: list[int]


class Direction(BaseModel):
    id: str
    name: str
    hypothesis: str
    status: Literal["exploring", "concluded", "abandoned"] = "exploring"
    steps_used: int = 0
    outcome: str | None = None


class Discarded(BaseModel):
    title: str
    reason: str


class Verdict(BaseModel):
    findings: list[Finding]
    discarded: list[Discarded]
