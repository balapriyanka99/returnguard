"""Typed Policy-v1 contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class NormalizedReturnReason(StrEnum):
    """Stable reason codes supplied by controlled data or a future return form."""

    DEFECTIVE_OR_NOT_WORKING = "DEFECTIVE_OR_NOT_WORKING"
    DAMAGED_ON_ARRIVAL = "DAMAGED_ON_ARRIVAL"
    WRONG_ITEM_RECEIVED = "WRONG_ITEM_RECEIVED"
    MISSING_PARTS_OR_ACCESSORIES = "MISSING_PARTS_OR_ACCESSORIES"
    NOT_AS_DESCRIBED = "NOT_AS_DESCRIBED"
    WRONG_SIZE_OR_FIT = "WRONG_SIZE_OR_FIT"
    NO_LONGER_NEEDED = "NO_LONGER_NEEDED"
    CHANGED_MIND = "CHANGED_MIND"
    ORDERED_BY_MISTAKE = "ORDERED_BY_MISTAKE"
    BETTER_PRICE_FOUND = "BETTER_PRICE_FOUND"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ReturnReasonGroup(StrEnum):
    PRODUCT_OR_FULFILLMENT_ISSUE = "PRODUCT_OR_FULFILLMENT_ISSUE"
    CUSTOMER_PREFERENCE_OR_DISCRETIONARY = "CUSTOMER_PREFERENCE_OR_DISCRETIONARY"
    NEUTRAL_OR_UNKNOWN = "NEUTRAL_OR_UNKNOWN"


class ReturnReasonInput(BaseModel):
    """Future Raise Return boundary; free text is never parsed by Policy-v1."""

    reason_code: NormalizedReturnReason
    reason_details: str | None = None


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


class ProductMitigationEffect(StrEnum):
    NONE = "NONE"
    MULTIPLIER_CAPPED_AT_1 = "MULTIPLIER_CAPPED_AT_1"
    FEE_WAIVED = "FEE_WAIVED"


class PolicyPricingSummary(BaseModel):
    """Exact safe inputs and intermediates used by a P30 fee calculation."""

    pricing_base_fee: Decimal
    pricing_raw_multiplier: Decimal
    pricing_capped_multiplier: Decimal
    pricing_sample_size_cap: Decimal
    pricing_behavior_signal_count: int
    pricing_item_value_cap: Decimal
    product_mitigation: int
    product_mitigation_effect: ProductMitigationEffect
    pricing_rationale: list[str]


class PolicyEvaluation(BaseModel):
    policy_version: str = "policy-v1"
    return_id: str
    assessment_id: str | None = None
    assessment_at: datetime
    action: PolicyAction
    matched_rule: str
    normalized_reason: NormalizedReturnReason = NormalizedReturnReason.UNKNOWN
    return_fee: Decimal | None = None
    fee_reason: str | None = None
    pricing: PolicyPricingSummary | None = None
    rationale: list[str]
    economics: PolicyEconomicsSummary
