"""Typed contracts shared by ReturnGuard agents."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from returnguard.intelligence.models import ReturnEconomics
from returnguard.policy.models import (
    NormalizedReturnReason,
    PolicyAction,
    PolicyEvaluation,
)
from returnguard.risk.models import CoverageLevel, RiskAssessment, RiskBand


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
    "device_id",
    "device_identifier",
    "fraud_labels",
    "scenario_id",
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


class SafeNetworkContext(BaseModel):
    """Identifier-free network context already gated by deterministic Risk-v1."""

    contribution: int = 0
    patterns: list[str] = Field(default_factory=list)
    summary: str | None = None
    merchant_explanation: str | None = None
    limitations: list[str] = Field(default_factory=list)
    contextual_evidence_only: bool = True


class DecisionEconomicsSummary(BaseModel):
    """Authoritative economics copied without LLM calculation."""

    current_item_value: float | None = None
    product_cost: float | None = None
    reverse_logistics_cost: float | None = None
    inspection_cost: float | None = None
    recovery_value: float | None = None
    total_operational_cost: float | None = None
    estimated_net_return_cost: float | None = None
    estimated_loss_exposure: float | None = None
    economics_data_confidence: str
    data_origin: str

    @classmethod
    def from_economics(cls, value: ReturnEconomics) -> "DecisionEconomicsSummary":
        return cls(**value.to_dict())


class DecisionPricingSummary(BaseModel):
    """Safe authoritative pricing facts from Policy-v1, when applicable."""

    normalized_reason: NormalizedReturnReason
    return_fee: Decimal | None = None
    fee_reason: str | None = None
    pricing_rationale: list[str] = Field(default_factory=list)


class DecisionSynthesisNarrative(BaseModel):
    """The only fields Gemini is allowed to author for decision synthesis."""

    decision_summary: str
    strongest_evidence: list[str] = Field(default_factory=list)
    mitigating_context: list[str] = Field(default_factory=list)
    network_explanation: str | None = None
    policy_reasoning: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class DecisionSynthesisInput(BaseModel):
    """Grounded, structured state supplied to the synthesis-only agent."""

    request_intent: WorkflowIntent
    return_id: str
    assessment_id: str | None = None
    assessment_at: datetime
    agents_selected: list[str] = Field(default_factory=list)
    agents_executed: list[str] = Field(default_factory=list)
    agents_skipped: dict[str, str] = Field(default_factory=dict)
    specialist_results: list[SpecialistResult] = Field(default_factory=list)
    network_context: SafeNetworkContext | None = None
    risk: RiskAssessment
    economics: DecisionEconomicsSummary
    policy: PolicyEvaluation
    vision_result: SpecialistResult | None = None
    missing_capabilities: list[MissingCapability] = Field(default_factory=list)

    @model_validator(mode="after")
    def deterministic_identity_must_match(self):
        identities = (
            (self.risk.return_id, self.risk.assessment_id, self.risk.assessment_at),
            (self.policy.return_id, self.policy.assessment_id, self.policy.assessment_at),
        )
        expected = (self.return_id, self.assessment_id, self.assessment_at)
        if any(identity != expected for identity in identities):
            raise ValueError("Decision synthesis deterministic identities do not match")
        return self


class DecisionSynthesisResult(BaseModel):
    """Merchant explanation with deterministic authority fields protected."""

    return_id: str
    assessment_id: str | None = None
    assessment_at: datetime
    recommended_action: PolicyAction
    matched_policy_rule: str
    risk_score: int | None
    risk_band: RiskBand
    risk_coverage: CoverageLevel
    decision_summary: str
    strongest_evidence: list[str] = Field(default_factory=list)
    mitigating_context: list[str] = Field(default_factory=list)
    network_context: SafeNetworkContext | None = None
    economics_summary: DecisionEconomicsSummary
    pricing: DecisionPricingSummary | None = None
    policy_reasoning: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    specialists_used: list[str] = Field(default_factory=list)
    data_origin: list[str] = Field(default_factory=list)


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
    network_context: SafeNetworkContext | None = None
    risk: RiskAssessment | None = None
    economics: DecisionEconomicsSummary | None = None
    policy: PolicyEvaluation | None = None
    decision_synthesis: DecisionSynthesisResult | None = None
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
