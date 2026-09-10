"""Pure deterministic descending-priority Policy-v1 rules."""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

from returnguard.intelligence import ReturnIntelligenceService
from returnguard.intelligence.models import EvidenceRecord, InspectionRecord, ReturnEconomics
from returnguard.observability import logged_operation, log_policy_summary
from returnguard.risk.models import CoverageLevel, RiskAssessment, RiskBand
from returnguard.risk.service import ReturnRiskService

from .models import PolicyAction, PolicyEconomicsSummary, PolicyEvaluation


logger = logging.getLogger(__name__)


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
    ) -> PolicyEvaluation:
        summary = PolicyEconomicsSummary(
            current_item_value=_money(economics.current_item_value),
            reverse_logistics_cost=_money(economics.reverse_logistics_cost),
            inspection_cost=_money(economics.inspection_cost),
            recovery_value=_money(economics.recovery_value),
            total_operational_cost=_money(economics.total_operational_cost),
            data_confidence=economics.economics_data_confidence,
        )

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
            return_fee=None,
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


class ReturnPolicyService:
    """Resolve one assessment through Risk-v1 and Policy-v1 without an LLM."""

    def __init__(
        self,
        intelligence: ReturnIntelligenceService,
        risk: ReturnRiskService | None = None,
        policy: PolicyV1Service | None = None,
    ) -> None:
        self.intelligence = intelligence
        self.risk = risk or ReturnRiskService(intelligence)
        self.policy = policy or PolicyV1Service()

    def evaluate(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
        *,
        assessment_id: str | None = None,
        inspection_is_meaningful_next_step: bool = True,
    ) -> tuple[RiskAssessment, PolicyEvaluation]:
        risk = self.risk.assess(
            return_id, assessment_at, assessment_id=assessment_id
        )
        effective = risk.assessment_at
        economics = self.intelligence.get_return_economics(return_id, effective)
        inspection = self.intelligence.get_inspection(return_id, effective)
        evidence = self.intelligence.get_evidence(return_id, effective)
        policy = self.policy.evaluate(
            risk,
            economics,
            inspection,
            evidence,
            inspection_is_meaningful_next_step=inspection_is_meaningful_next_step,
        )
        return risk, policy
