"""Typed Policy-v1 contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class PolicyAction(StrEnum):
    AUTO_APPROVE = "AUTO_APPROVE"
    STANDARD_RETURN = "STANDARD_RETURN"
    REFUND_WITHOUT_RETURN = "REFUND_WITHOUT_RETURN"
    APPROVE_WITH_RETURN_FEE = "APPROVE_WITH_RETURN_FEE"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    REQUIRE_INSPECTION = "REQUIRE_INSPECTION"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    ESCALATE_TO_SPECIALIST = "ESCALATE_TO_SPECIALIST"
    REJECT_RETURN = "REJECT_RETURN"


class PolicyEconomicsSummary(BaseModel):
    current_item_value: Decimal | None
    reverse_logistics_cost: Decimal | None
    inspection_cost: Decimal | None
    recovery_value: Decimal | None
    total_operational_cost: Decimal | None
    data_confidence: str


class PolicyEvaluation(BaseModel):
    policy_version: str = "policy-v1"
    return_id: str
    assessment_id: str | None = None
    assessment_at: datetime
    action: PolicyAction
    matched_rule: str
    return_fee: Decimal | None = None
    rationale: list[str]
    economics: PolicyEconomicsSummary
