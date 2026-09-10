"""Pure deterministic descending-priority Policy-v1 rules."""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

from returnguard.economics.models import EconomicsAssessment
from returnguard.economics.repository import EconomicsAssessmentRepository
from returnguard.intelligence import ReturnIntelligenceService
from returnguard.intelligence.models import EvidenceRecord, InspectionRecord, ReturnEconomics
from returnguard.observability import logged_operation, log_policy_summary
from returnguard.risk.models import CoverageLevel, RiskAssessment, RiskBand
from returnguard.risk.service import ReturnRiskService

from .models import (
    NormalizedReturnReason,
    PolicyAction,
    PolicyEconomicsSummary,
    PolicyEvaluation,
    PolicyPricingSummary,
    ProductMitigationEffect,
    ReturnReasonGroup,
    ReturnReasonInput,
)
from .repository import PolicyAssessmentRepository


logger = logging.getLogger(__name__)


_CONTROLLED_REASON_MAP = {
    "changed_mind": NormalizedReturnReason.CHANGED_MIND,
    "damaged_on_arrival": NormalizedReturnReason.DAMAGED_ON_ARRIVAL,
    "defective": NormalizedReturnReason.DEFECTIVE_OR_NOT_WORKING,
    "missing_accessories": NormalizedReturnReason.MISSING_PARTS_OR_ACCESSORIES,
    "not_as_described": NormalizedReturnReason.NOT_AS_DESCRIBED,
    "wrong_item_shipped": NormalizedReturnReason.WRONG_ITEM_RECEIVED,
    "wrong_size": NormalizedReturnReason.WRONG_SIZE_OR_FIT,
}

_PRODUCT_OR_FULFILLMENT_REASONS = {
    NormalizedReturnReason.DEFECTIVE_OR_NOT_WORKING,
    NormalizedReturnReason.DAMAGED_ON_ARRIVAL,
    NormalizedReturnReason.WRONG_ITEM_RECEIVED,
    NormalizedReturnReason.MISSING_PARTS_OR_ACCESSORIES,
    NormalizedReturnReason.NOT_AS_DESCRIBED,
}

_DISCRETIONARY_REASONS = {
    NormalizedReturnReason.WRONG_SIZE_OR_FIT,
    NormalizedReturnReason.NO_LONGER_NEEDED,
    NormalizedReturnReason.CHANGED_MIND,
    NormalizedReturnReason.ORDERED_BY_MISTAKE,
    NormalizedReturnReason.BETTER_PRICE_FOUND,
}

_BEHAVIOR_REASON_CODES = {
    "ELEVATED_LIFETIME_RETURN_RATE",
    "RECENT_RETURN_CONCENTRATION",
    "HIGH_RETURNED_VALUE_RATIO",
    "REPEAT_SAME_PRODUCT_RETURNS",
    "COUNT_BASED_HISTORY_FALLBACK",
}


def normalize_return_reason(
    value: str | NormalizedReturnReason | None,
) -> NormalizedReturnReason:
    """Normalize only explicit codes and unambiguous controlled reason values."""

    if isinstance(value, NormalizedReturnReason):
        return value
    if value is None or not value.strip():
        return NormalizedReturnReason.UNKNOWN
    normalized = value.strip()
    try:
        return NormalizedReturnReason(normalized.upper())
    except ValueError:
        return _CONTROLLED_REASON_MAP.get(
            normalized.casefold(), NormalizedReturnReason.OTHER
        )


def return_reason_group(reason: NormalizedReturnReason) -> ReturnReasonGroup:
    if reason in _PRODUCT_OR_FULFILLMENT_REASONS:
        return ReturnReasonGroup.PRODUCT_OR_FULFILLMENT_ISSUE
    if reason in _DISCRETIONARY_REASONS:
        return ReturnReasonGroup.CUSTOMER_PREFERENCE_OR_DISCRETIONARY
    return ReturnReasonGroup.NEUTRAL_OR_UNKNOWN


def _money(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PolicyV1Service:
    policy_version = "policy-v1"

    def evaluate(
        self,
        risk: RiskAssessment,
        economics: ReturnEconomics,
        inspection: InspectionRecord | None,
        evidence: list[EvidenceRecord],
        *,
        inspection_is_meaningful_next_step: bool = True,
        normalized_reason: NormalizedReturnReason = NormalizedReturnReason.UNKNOWN,
        customer_total_items: int = 0,
    ) -> PolicyEvaluation:
        del evidence  # Metadata contributes no suspiciousness and no direct policy rule.
        with logged_operation(
            logger,
            operation_type="policy_engine",
            operation_name="policy-v1",
            return_id=risk.return_id,
            assessment_at=risk.assessment_at,
        ):
            result = self._evaluate(
                risk,
                economics,
                inspection,
                inspection_is_meaningful_next_step=inspection_is_meaningful_next_step,
                normalized_reason=normalized_reason,
                customer_total_items=customer_total_items,
            )
            log_policy_summary(logger, result)
            return result

    def _evaluate(
        self,
        risk: RiskAssessment,
        economics: ReturnEconomics,
        inspection: InspectionRecord | None,
        *,
        inspection_is_meaningful_next_step: bool,
        normalized_reason: NormalizedReturnReason,
        customer_total_items: int,
    ) -> PolicyEvaluation:
        summary = PolicyEconomicsSummary(
            current_item_value=_money(economics.current_item_value),
            reverse_logistics_cost=_money(economics.reverse_logistics_cost),
            inspection_cost=_money(economics.inspection_cost),
            recovery_value=_money(economics.recovery_value),
            total_operational_cost=_money(economics.total_operational_cost),
            data_confidence=economics.economics_data_confidence,
        )

        return_fee = None
        fee_reason = None
        pricing = None
        if inspection is not None and inspection.serial_mismatch is True:
            rule = "P100_CONFIRMED_SERIAL_MISMATCH"
            action = PolicyAction.ESCALATE_TO_SPECIALIST
            rationale = ["A deterministic warehouse serial mismatch is present."]
        elif risk.band == RiskBand.UNDETERMINED:
            rule = "P90_UNDETERMINED"
            if inspection is None and inspection_is_meaningful_next_step:
                action = PolicyAction.REQUIRE_INSPECTION
                rationale = ["Risk is undetermined and warehouse inspection is a meaningful next step."]
            else:
                action = PolicyAction.REQUEST_EVIDENCE
                rationale = ["Risk is undetermined; request additional evidence without inferring abuse."]
        elif risk.band == RiskBand.CRITICAL and self._strong_inspection(inspection):
            rule = "P80_CRITICAL_PHYSICAL_DISCREPANCY"
            action = PolicyAction.ESCALATE_TO_SPECIALIST
            rationale = ["Critical risk includes a strong deterministic physical discrepancy."]
        elif risk.band in {RiskBand.HIGH, RiskBand.CRITICAL}:
            rule = "P70_HIGH_RISK"
            action = PolicyAction.MANUAL_REVIEW
            rationale = ["High or critical deterministic risk requires human review."]
        elif risk.band == RiskBand.MEDIUM and inspection is None:
            rule = "P60_MEDIUM_NEEDS_INSPECTION"
            action = PolicyAction.REQUIRE_INSPECTION
            rationale = ["Medium risk has no completed warehouse inspection."]
        elif risk.band == RiskBand.MEDIUM:
            rule = "P55_MEDIUM_INSPECTED"
            action = PolicyAction.MANUAL_REVIEW
            rationale = ["Medium risk remains after warehouse inspection."]
        elif self._refund_without_return(risk, summary):
            rule = "P40_REFUND_WITHOUT_RETURN"
            action = PolicyAction.REFUND_WITHOUT_RETURN
            rationale = ["Low risk and deterministic return-processing cost is not below recovery value."]
        elif fee := self._return_fee(
            risk,
            summary,
            inspection,
            normalized_reason,
            customer_total_items,
        ):
            return_fee, pricing = fee
            rule = "P30_RETURN_FEE"
            action = PolicyAction.APPROVE_WITH_RETURN_FEE
            fee_reason = (
                "Eligible discretionary return priced from reverse-logistics cost "
                "with a sample-capped historical-behavior adjustment."
            )
            rationale = list(pricing.pricing_rationale)
        elif risk.band == RiskBand.LOW and risk.coverage == CoverageLevel.SUBSTANTIAL:
            rule = "P20_LOW_SUBSTANTIAL"
            action = PolicyAction.AUTO_APPROVE
            rationale = ["Low risk is supported by substantial deterministic coverage."]
        else:
            rule = "P10_LOW_DEFAULT"
            action = PolicyAction.STANDARD_RETURN
            rationale = ["Low risk follows the standard return workflow."]

        return PolicyEvaluation(
            return_id=risk.return_id,
            assessment_id=risk.assessment_id,
            assessment_at=risk.assessment_at,
            action=action,
            matched_rule=rule,
            normalized_reason=normalized_reason,
            return_fee=return_fee,
            fee_reason=fee_reason,
            pricing=pricing,
            rationale=rationale,
            economics=summary,
        )

    @staticmethod
    def _strong_inspection(inspection: InspectionRecord | None) -> bool:
        if inspection is None:
            return False
        return inspection.serial_mismatch is True or inspection.item_present is False

    @staticmethod
    def _refund_without_return(
        risk: RiskAssessment, economics: PolicyEconomicsSummary
    ) -> bool:
        return bool(
            risk.band == RiskBand.LOW
            and economics.data_confidence == "complete"
            and economics.current_item_value is not None
            and economics.current_item_value <= Decimal("1500.00")
            and economics.total_operational_cost is not None
            and economics.recovery_value is not None
            and economics.total_operational_cost >= economics.recovery_value
        )

    @staticmethod
    def _return_fee(
        risk: RiskAssessment,
        economics: PolicyEconomicsSummary,
        inspection: InspectionRecord | None,
        normalized_reason: NormalizedReturnReason,
        customer_total_items: int,
    ) -> tuple[Decimal, PolicyPricingSummary] | None:
        if (
            risk.band != RiskBand.LOW
            or risk.score is None
            or risk.coverage != CoverageLevel.SUBSTANTIAL
            or return_reason_group(normalized_reason)
            != ReturnReasonGroup.CUSTOMER_PREFERENCE_OR_DISCRETIONARY
            or PolicyV1Service._strong_inspection(inspection)
            or economics.data_confidence != "complete"
            or economics.current_item_value is None
            or economics.reverse_logistics_cost is None
            or economics.current_item_value <= 0
            or economics.reverse_logistics_cost <= 0
            or risk.product_mitigation <= -6
        ):
            return None

        signal_count = len({
            reason.code
            for reason in risk.reasons
            if reason.group == "CUSTOMER_BEHAVIOR"
            and reason.contribution > 0
            and reason.code in _BEHAVIOR_REASON_CODES
        })
        sample_cap = (
            Decimal("1.00") if customer_total_items < 5
            else Decimal("1.10") if customer_total_items < 10
            else Decimal("1.20") if customer_total_items < 20
            else Decimal("1.30")
        )
        raw_multiplier = Decimal("1.00") + Decimal("0.10") * signal_count
        multiplier = min(
            raw_multiplier,
            sample_cap,
        )
        mitigation_effect = ProductMitigationEffect.NONE
        if risk.product_mitigation <= -3:
            multiplier = Decimal("1.00")
            mitigation_effect = ProductMitigationEffect.MULTIPLIER_CAPPED_AT_1

        ten_percent_cap = (
            economics.current_item_value * Decimal("0.10")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        base_fee = min(economics.reverse_logistics_cost, ten_percent_cap)
        fee = min(
            base_fee * multiplier,
            ten_percent_cap,
            economics.current_item_value,
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if fee <= 0:
            return None
        pricing_rationale = [
            f"Normalized reason {normalized_reason.value} is discretionary.",
            (
                f"Historical behavior produced raw multiplier {raw_multiplier} from "
                f"{signal_count} distinct canonical behavior signal(s)."
            ),
            f"Historical sample-size multiplier cap was {sample_cap}.",
            (
                "Product-quality mitigation limited the fee to its base amount."
                if mitigation_effect == ProductMitigationEffect.MULTIPLIER_CAPPED_AT_1
                else "No material product-quality fee waiver applied."
            ),
        ]
        return fee, PolicyPricingSummary(
            pricing_base_fee=base_fee,
            pricing_raw_multiplier=raw_multiplier,
            pricing_capped_multiplier=multiplier,
            pricing_sample_size_cap=sample_cap,
            pricing_behavior_signal_count=signal_count,
            pricing_item_value_cap=ten_percent_cap,
            product_mitigation=risk.product_mitigation,
            product_mitigation_effect=mitigation_effect,
            pricing_rationale=pricing_rationale,
        )


class ReturnPolicyService:
    """Resolve one assessment through Risk-v1 and Policy-v1 without an LLM."""

    def __init__(
        self,
        intelligence: ReturnIntelligenceService,
        risk: ReturnRiskService | None = None,
        policy: PolicyV1Service | None = None,
        economics_repository: EconomicsAssessmentRepository | None = None,
        policy_repository: PolicyAssessmentRepository | None = None,
    ) -> None:
        self.intelligence = intelligence
        self.risk = risk or ReturnRiskService(intelligence)
        self.policy = policy or PolicyV1Service()
        self.economics_repository = economics_repository
        self.policy_repository = policy_repository

    def evaluate(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
        *,
        assessment_id: str | None = None,
        inspection_is_meaningful_next_step: bool = True,
        return_reason: ReturnReasonInput | None = None,
    ) -> tuple[RiskAssessment, PolicyEvaluation]:
        risk, policy, _ = self._evaluate_once(
            return_id,
            assessment_at,
            assessment_id=assessment_id,
            inspection_is_meaningful_next_step=inspection_is_meaningful_next_step,
            return_reason=return_reason,
        )
        return risk, policy

    def evaluate_and_record(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
        *,
        assessment_id: str | None = None,
        inspection_is_meaningful_next_step: bool = True,
        return_reason: ReturnReasonInput | None = None,
    ) -> tuple[RiskAssessment, PolicyEvaluation, EconomicsAssessment]:
        """Evaluate once and append immutable economics and policy snapshots."""

        if self.economics_repository is None or self.policy_repository is None:
            raise RuntimeError(
                "EconomicsAssessmentRepository and PolicyAssessmentRepository "
                "are required to persist policy assessments"
            )
        risk, policy, economics = self._evaluate_once(
            return_id,
            assessment_at,
            assessment_id=assessment_id,
            inspection_is_meaningful_next_step=inspection_is_meaningful_next_step,
            return_reason=return_reason,
        )
        economics_assessment = EconomicsAssessment.from_result(
            return_id=return_id,
            assessment_id=risk.assessment_id,
            assessment_at=risk.assessment_at,
            result=economics,
        )
        self.economics_repository.append(economics_assessment)
        self.policy_repository.append(policy)
        return risk, policy, economics_assessment

    def _evaluate_once(
        self,
        return_id: str,
        assessment_at: datetime | None,
        *,
        assessment_id: str | None,
        inspection_is_meaningful_next_step: bool,
        return_reason: ReturnReasonInput | None,
    ) -> tuple[RiskAssessment, PolicyEvaluation, ReturnEconomics]:
        risk = self.risk.assess(
            return_id, assessment_at, assessment_id=assessment_id
        )
        effective = risk.assessment_at
        economics = self.intelligence.get_return_economics(return_id, effective)
        inspection = self.intelligence.get_inspection(return_id, effective)
        evidence = self.intelligence.get_evidence(return_id, effective)
        current = self.intelligence.repository.get_return(return_id)
        stored_reason = None if current is None else current.get("reason")
        normalized_reason = (
            return_reason.reason_code
            if return_reason is not None
            else normalize_return_reason(stored_reason)
        )
        customer = self.intelligence.get_customer_intelligence(return_id, effective)
        policy = self.policy.evaluate(
            risk,
            economics,
            inspection,
            evidence,
            inspection_is_meaningful_next_step=inspection_is_meaningful_next_step,
            normalized_reason=normalized_reason,
            customer_total_items=customer.total_items,
        )
        return risk, policy, economics
