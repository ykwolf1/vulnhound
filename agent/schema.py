"""Verdict 报告 schema（pydantic）。"""

from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["Discarded", "Finding", "Verdict"]


class Finding(BaseModel):
    title: str
    severity: Literal["high", "medium", "low"]
    rationale: str = Field(max_length=300)
    evidence: list[int]


class Discarded(BaseModel):
    title: str
    reason: str


class Verdict(BaseModel):
    findings: list[Finding]
    discarded: list[Discarded]
