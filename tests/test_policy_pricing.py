from __future__ import annotations

import inspect
import unittest
from decimal import Decimal

from returnguard.policy import (
    NormalizedReturnReason,
    PolicyAction,
    ProductMitigationEffect,
    PolicyV1Service,
    ReturnReasonGroup,
    ReturnReasonInput,
    normalize_return_reason,
    return_reason_group,
)
from returnguard.risk import CoverageLevel, RiskBand
from returnguard.risk.models import RiskReason
from tests.test_risk_policy import assess, economics, inspection


BEHAVIOR_CODES = (
    "ELEVATED_LIFETIME_RETURN_RATE",
    "RECENT_RETURN_CONCENTRATION",
    "HIGH_RETURNED_VALUE_RATIO",
)


def fee_economics(**changes):
    values = dict(
        current_item_value=200.0,
        reverse_logistics_cost=10.0,
        inspection_cost=5.0,
        recovery_value=100.0,
        total_operational_cost=15.0,
    )
    values.update(changes)
    return economics(**values)


def fee_risk(*, behavior_signal_count=0, **changes):
    risk = assess()
    reasons = [
        RiskReason(
            code=code,
            group="CUSTOMER_BEHAVIOR",
            contribution=3,
            canonical_field="return_behavior.test",
            explanation="Deterministic customer behavior signal.",
        )
        for code in BEHAVIOR_CODES[:behavior_signal_count]
    ]
    values = dict(
        score=6 if reasons else 0,
        band=RiskBand.LOW,
        coverage=CoverageLevel.SUBSTANTIAL,
        reasons=reasons,
    )
    values.update(changes)
    return risk.model_copy(update=values)


def evaluate_fee(
    *,
    reason=NormalizedReturnReason.CHANGED_MIND,
    total_items=20,
    behavior_signal_count=3,
    risk=None,
    costs=None,
    inspected=None,
):
    return PolicyV1Service().evaluate(
        risk or fee_risk(behavior_signal_count=behavior_signal_count),
        costs or fee_economics(),
        inspected,
        [],
        normalized_reason=reason,
        customer_total_items=total_items,
    )


class ReturnReasonNormalizationTests(unittest.TestCase):
    def test_exact_controlled_reason_mapping(self):
        expected = {
            "changed_mind": NormalizedReturnReason.CHANGED_MIND,
            "damaged_on_arrival": NormalizedReturnReason.DAMAGED_ON_ARRIVAL,
            "defective": NormalizedReturnReason.DEFECTIVE_OR_NOT_WORKING,
            "missing_accessories": NormalizedReturnReason.MISSING_PARTS_OR_ACCESSORIES,
            "not_as_described": NormalizedReturnReason.NOT_AS_DESCRIBED,
            "wrong_item_shipped": NormalizedReturnReason.WRONG_ITEM_RECEIVED,
            "wrong_size": NormalizedReturnReason.WRONG_SIZE_OR_FIT,
            "damaged": NormalizedReturnReason.OTHER,
            "missing_item": NormalizedReturnReason.OTHER,
        }
        for source, normalized in expected.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_return_reason(source), normalized)
        self.assertEqual(normalize_return_reason(None), NormalizedReturnReason.UNKNOWN)

    def test_reason_groups_are_explicit(self):
        self.assertEqual(
            return_reason_group(NormalizedReturnReason.DEFECTIVE_OR_NOT_WORKING),
            ReturnReasonGroup.PRODUCT_OR_FULFILLMENT_ISSUE,
        )
        self.assertEqual(
            return_reason_group(NormalizedReturnReason.CHANGED_MIND),
            ReturnReasonGroup.CUSTOMER_PREFERENCE_OR_DISCRETIONARY,
        )
        self.assertEqual(
            return_reason_group(NormalizedReturnReason.OTHER),
            ReturnReasonGroup.NEUTRAL_OR_UNKNOWN,
        )

    def test_future_return_contract_does_not_normalize_free_text(self):
        request = ReturnReasonInput(
            reason_code=NormalizedReturnReason.UNKNOWN,
            reason_details="changed mind because a better price was found",
        )
        result = evaluate_fee(reason=request.reason_code)
        self.assertNotEqual(result.matched_rule, "P30_RETURN_FEE")
        self.assertIsNone(result.return_fee)


class DynamicReturnFeeTests(unittest.TestCase):
    def test_discretionary_reason_with_sufficient_history_is_eligible(self):
        result = evaluate_fee()
        self.assertEqual(result.action, PolicyAction.APPROVE_WITH_RETURN_FEE)
        self.assertEqual(result.matched_rule, "P30_RETURN_FEE")
        self.assertEqual(result.return_fee, Decimal("13.00"))
        self.assertEqual(result.normalized_reason, NormalizedReturnReason.CHANGED_MIND)
        self.assertIsNotNone(result.fee_reason)
        self.assertIsNotNone(result.pricing)
        self.assertEqual(result.pricing.pricing_base_fee, Decimal("10.00"))
        self.assertEqual(result.pricing.pricing_raw_multiplier, Decimal("1.30"))
        self.assertEqual(result.pricing.pricing_sample_size_cap, Decimal("1.30"))
        self.assertEqual(result.pricing.pricing_capped_multiplier, Decimal("1.30"))
        self.assertEqual(result.pricing.pricing_behavior_signal_count, 3)
        self.assertEqual(result.pricing.pricing_item_value_cap, Decimal("20.00"))
        self.assertEqual(result.pricing.product_mitigation, 0)
        self.assertEqual(
            result.pricing.product_mitigation_effect,
            ProductMitigationEffect.NONE,
        )
        self.assertTrue(result.pricing.pricing_rationale)

    def test_product_or_fulfillment_reasons_never_charge_fee(self):
        reasons = (
            NormalizedReturnReason.DEFECTIVE_OR_NOT_WORKING,
            NormalizedReturnReason.DAMAGED_ON_ARRIVAL,
            NormalizedReturnReason.WRONG_ITEM_RECEIVED,
            NormalizedReturnReason.MISSING_PARTS_OR_ACCESSORIES,
            NormalizedReturnReason.NOT_AS_DESCRIBED,
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                result = evaluate_fee(reason=reason)
                self.assertNotEqual(result.matched_rule, "P30_RETURN_FEE")
                self.assertIsNone(result.return_fee)

    def test_unknown_and_other_never_charge_fee(self):
        for reason in (NormalizedReturnReason.UNKNOWN, NormalizedReturnReason.OTHER):
            with self.subTest(reason=reason):
                self.assertIsNone(evaluate_fee(reason=reason).return_fee)

    def test_sample_size_caps_behavior_multiplier(self):
        expected = ((4, "10.00"), (5, "11.00"), (10, "12.00"), (20, "13.00"))
        for total_items, fee in expected:
            with self.subTest(total_items=total_items):
                result = evaluate_fee(total_items=total_items)
                self.assertEqual(result.return_fee, Decimal(fee))

    def test_no_behavior_signal_means_no_escalation(self):
        result = evaluate_fee(total_items=20, behavior_signal_count=0)
        self.assertEqual(result.return_fee, Decimal("10.00"))

    def test_strong_product_mitigation_waives_fee(self):
        result = evaluate_fee(
            risk=fee_risk(behavior_signal_count=3, product_mitigation=-6)
        )
        self.assertNotEqual(result.matched_rule, "P30_RETURN_FEE")
        self.assertIsNone(result.return_fee)
        self.assertIsNone(result.pricing)

    def test_moderate_product_mitigation_prevents_escalation(self):
        result = evaluate_fee(
            risk=fee_risk(behavior_signal_count=3, product_mitigation=-3)
        )
        self.assertEqual(result.matched_rule, "P30_RETURN_FEE")
        self.assertEqual(result.return_fee, Decimal("10.00"))
        self.assertEqual(result.pricing.pricing_raw_multiplier, Decimal("1.30"))
        self.assertEqual(result.pricing.pricing_capped_multiplier, Decimal("1.00"))
        self.assertEqual(result.pricing.product_mitigation, -3)
        self.assertEqual(
            result.pricing.product_mitigation_effect,
            ProductMitigationEffect.MULTIPLIER_CAPPED_AT_1,
        )

    def test_refund_without_return_outranks_fee(self):
        result = evaluate_fee(
            costs=fee_economics(recovery_value=10.0, total_operational_cost=15.0)
        )
        self.assertEqual(result.matched_rule, "P40_REFUND_WITHOUT_RETURN")
        self.assertIsNone(result.return_fee)
        self.assertIsNone(result.pricing)

    def test_serial_mismatch_outranks_fee(self):
        inspected = inspection(serial_mismatch=True)
        result = evaluate_fee(inspected=inspected)
        self.assertEqual(result.matched_rule, "P100_CONFIRMED_SERIAL_MISMATCH")
        self.assertIsNone(result.return_fee)

    def test_missing_item_is_not_fee_eligible(self):
        result = evaluate_fee(inspected=inspection(item_present=False))
        self.assertNotEqual(result.matched_rule, "P30_RETURN_FEE")
        self.assertIsNone(result.return_fee)

    def test_non_low_and_undetermined_risk_never_use_fee_rule(self):
        for score, band in (
            (30, RiskBand.MEDIUM),
            (60, RiskBand.HIGH),
            (80, RiskBand.CRITICAL),
            (None, RiskBand.UNDETERMINED),
        ):
            with self.subTest(band=band):
                risk = fee_risk(score=score, band=band)
                result = evaluate_fee(risk=risk)
                self.assertNotEqual(result.matched_rule, "P30_RETURN_FEE")
                self.assertIsNone(result.return_fee)

    def test_partial_coverage_and_incomplete_economics_never_charge_fee(self):
        partial = evaluate_fee(
            risk=fee_risk(coverage=CoverageLevel.PARTIAL)
        )
        incomplete = evaluate_fee(
            costs=fee_economics(economics_data_confidence="limited")
        )
        self.assertIsNone(partial.return_fee)
        self.assertIsNone(incomplete.return_fee)

    def test_fee_is_rounded_and_never_exceeds_ten_percent_or_item_value(self):
        result = evaluate_fee(
            costs=fee_economics(
                current_item_value=Decimal("50.05"),
                reverse_logistics_cost=Decimal("100.00"),
            )
        )
        self.assertEqual(result.return_fee, Decimal("5.01"))
        self.assertLessEqual(result.return_fee, Decimal("50.05") * Decimal("0.10") + Decimal("0.01"))
        self.assertLessEqual(result.return_fee, Decimal("50.05"))

    def test_policy_source_has_no_label_or_scenario_branch(self):
        source = inspect.getsource(__import__("returnguard.policy.service", fromlist=["*"]))
        self.assertNotIn("fraud_labels", source)
        self.assertNotIn("scenario_id", source)

    def test_fee_decision_has_readable_safe_log(self):
        with self.assertLogs("returnguard.policy.service", level="INFO") as captured:
            evaluate_fee()
        rendered = "\n".join(captured.output)
        self.assertIn("action: APPROVE_WITH_RETURN_FEE", rendered)
        self.assertIn("matched rule: P30_RETURN_FEE", rendered)
        self.assertIn("normalized reason: CHANGED_MIND", rendered)
        self.assertIn("return fee: 13.00", rendered)
        self.assertIn("base fee: 10.00", rendered)
        self.assertIn("raw multiplier: 1.30", rendered)
        self.assertIn("product mitigation effect: NONE", rendered)
        self.assertIn("fee rationale:", rendered)
        self.assertNotIn("reason_details", rendered)


if __name__ == "__main__":
    unittest.main()
