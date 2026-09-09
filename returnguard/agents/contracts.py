"""Typed contracts shared by ReturnGuard agents."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROHIBITED_TOOL_EVIDENCE_FIELDS = frozenset({
    "expected_serial",
    "returned_serial",
    "image_uri",
    "reference_image_uri",
    "network_identifier",
    "network_identifiers",
    "linked_user_id",
    "linked_user_ids",
    "ip_address",
    "session_id",
    "fraud_labels",
})


def sanitize_tool_evidence_facts(value: Any) -> Any:
    """Remove prohibited raw fields while preserving safe derived facts."""

    if isinstance(value, dict):
        return {
            key: sanitize_tool_evidence_facts(child)
            for key, child in value.items()
            if str(key).casefold() not in PROHIBITED_TOOL_EVIDENCE_FIELDS
        }
    if isinstance(value, list):
        return [sanitize_tool_evidence_facts(child) for child in value]
    return value


class AgentStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED = "failed"


class AgentConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNAVAILABLE = "unavailable"


class WorkflowIntent(StrEnum):
    GENERAL_INVESTIGATION = "general_investigation"
    INSPECTION_REVIEW = "inspection_review"
    VISION_REVIEW = "vision_review"
    RISK_POLICY_DECISION = "risk_policy_decision"


class AgentExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    return_id: str
    assessment_at: datetime
    trace_id: str | None = None
    assessment_id: str | None = None
    request_id: str | None = None


class Finding(BaseModel):
    code: str
    title: str
    description: str
    severity: str | None = None
    evidence: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)


class ToolEvidence(BaseModel):
    tool_name: str
    return_id: str
    assessment_at: datetime | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    data_origin: str | None = None

    @field_validator("facts")
    @classmethod
    def facts_must_be_compact_and_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        return sanitize_tool_evidence_facts(value)


class SpecialistResult(BaseModel):
    agent_name: str
    return_id: str
    assessment_at: datetime
    status: AgentStatus
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence: AgentConfidence
    tool_evidence: list[ToolEvidence] = Field(default_factory=list)
    trace_id: str | None = None
    assessment_id: str | None = None

    @model_validator(mode="after")
    def findings_require_tool_evidence(self):
        if self.findings and not self.tool_evidence:
            raise ValueError("Factual findings require compact MCP tool evidence")
        return self


class MissingCapability(BaseModel):
    capability: str
    reason: str
    next_action: str | None = None


class OrchestrationPlan(BaseModel):
    request_intent: WorkflowIntent
    return_id: str
    assessment_at: datetime
    agents_selected: list[str] = Field(default_factory=list)
    agents_skipped: dict[str, str] = Field(default_factory=dict)
    missing_capabilities: list[MissingCapability] = Field(default_factory=list)
    next_required_action: str | None = None
    trace_id: str | None = None
    assessment_id: str | None = None


class OrchestrationSynthesis(BaseModel):
    """Gemini-authored narrative only; execution facts remain deterministic."""

    summary: str
    limitations: list[str] = Field(default_factory=list)


class OrchestrationResult(BaseModel):
    request_intent: WorkflowIntent
    return_id: str
    assessment_at: datetime
    status: AgentStatus
    agents_selected: list[str] = Field(default_factory=list)
    agents_executed: list[str] = Field(default_factory=list)
    agents_skipped: dict[str, str] = Field(default_factory=dict)
    specialist_results: list[SpecialistResult] = Field(default_factory=list)
    missing_capabilities: list[MissingCapability] = Field(default_factory=list)
    next_required_action: str | None = None
    summary: str = ""
    limitations: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    assessment_id: str | None = None


class CopilotAnswer(BaseModel):
    return_id: str
    assessment_at: datetime
    status: AgentStatus
    answer: str
    tools_used: list[str] = Field(default_factory=list)
    key_evidence: list[ToolEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    unavailable_capabilities: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    assessment_id: str | None = None

    @model_validator(mode="after")
    def used_tools_require_evidence(self):
        if self.tools_used and not self.key_evidence:
            raise ValueError("Used tools require compact MCP tool evidence")
        cited = {evidence.tool_name for evidence in self.key_evidence}
        if not cited.issubset(set(self.tools_used)):
            raise ValueError("Tool evidence must reference a tool listed in tools_used")
        return self
