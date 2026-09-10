"""Typed Risk-v1 contracts containing safe deterministic facts only."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RiskBand(StrEnum):
    UNDETERMINED = "UNDETERMINED"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class CoverageLevel(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    PARTIAL = "PARTIAL"
    SUBSTANTIAL = "SUBSTANTIAL"


class RiskPattern(StrEnum):
    POSSIBLE_WARDROBING = "POSSIBLE_WARDROBING"
    POSSIBLE_REPEATED_RETURN_ABUSE = "POSSIBLE_REPEATED_RETURN_ABUSE"
    POSSIBLE_HIGH_FREQUENCY_RETURN_ABUSE = "POSSIBLE_HIGH_FREQUENCY_RETURN_ABUSE"
    POSSIBLE_RETURN_VALUE_ABUSE = "POSSIBLE_RETURN_VALUE_ABUSE"
    POSSIBLE_ITEM_SUBSTITUTION = "POSSIBLE_ITEM_SUBSTITUTION"
    POSSIBLE_SERIAL_SUBSTITUTION = "POSSIBLE_SERIAL_SUBSTITUTION"
    POSSIBLE_EMPTY_BOX = "POSSIBLE_EMPTY_BOX"
    POSSIBLE_ACCESSORY_STRIPPING = "POSSIBLE_ACCESSORY_STRIPPING"
    POSSIBLE_CONDITION_MISREPRESENTATION = "POSSIBLE_CONDITION_MISREPRESENTATION"
    POSSIBLE_CLAIM_EVIDENCE_CONFLICT = "POSSIBLE_CLAIM_EVIDENCE_CONFLICT"
    POSSIBLE_VISUAL_MISMATCH = "POSSIBLE_VISUAL_MISMATCH"
    POSSIBLE_LINKED_ACCOUNT_ABUSE = "POSSIBLE_LINKED_ACCOUNT_ABUSE"
    POSSIBLE_SHARED_RETURN_PATTERN = "POSSIBLE_SHARED_RETURN_PATTERN"
    POSSIBLE_PRODUCT_QUALITY_ISSUE = "POSSIBLE_PRODUCT_QUALITY_ISSUE"
    POSSIBLE_LOGISTICS_DAMAGE = "POSSIBLE_LOGISTICS_DAMAGE"
    LIKELY_LEGITIMATE_RETURN = "LIKELY_LEGITIMATE_RETURN"
    AMBIGUOUS_OR_INSUFFICIENT_EVIDENCE = "AMBIGUOUS_OR_INSUFFICIENT_EVIDENCE"


class RiskReason(BaseModel):
    code: str
    group: str
    contribution: int
    canonical_field: str
    explanation: str


class RiskAssessment(BaseModel):
    risk_event_id: str
    engine_version: str = "risk-v1"
    return_id: str
    assessment_id: str | None = None
    assessment_at: datetime
    score: int | None
    band: RiskBand
    coverage: CoverageLevel
    evaluable_domains: list[str] = Field(default_factory=list)
    group_scores: dict[str, int] = Field(default_factory=dict)
    product_mitigation: int = 0
    reasons: list[RiskReason] = Field(default_factory=list)
    patterns: list[RiskPattern] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    data_origin: str = "deterministic_intelligence"

