"""Typed, transport-neutral contracts for ReturnGuard MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from returnguard.intelligence.models import StructuredModel
from returnguard.observability import ExecutionContext


@dataclass(frozen=True)
class IntelligenceToolRequest:
    return_id: str
    assessment_at: datetime | None = None
    context: ExecutionContext = field(default_factory=ExecutionContext)


@dataclass(frozen=True)
class RiskAssessmentRequest:
    """Structured findings supplied to a future deterministic Risk Engine."""

    return_id: str
    structured_findings: dict[str, Any]
    assessment_at: datetime | None = None
    context: ExecutionContext = field(default_factory=ExecutionContext)


@dataclass
class MCPToolResult(StructuredModel):
    tool_name: str
    return_id: str
    assessment_at: datetime | None
    trace_id: str | None
    assessment_id: str | None
    result: dict[str, Any] | list[dict[str, Any]] | None


@dataclass
class RiskAssessmentResult(StructuredModel):
    """Minimum result boundary; values must be supplied by the Risk Engine."""

    return_id: str
    assessment_at: datetime | None
    risk_score: float | None
    risk_level: str | None
    factors: list[dict[str, Any]]
    data_origin: str
