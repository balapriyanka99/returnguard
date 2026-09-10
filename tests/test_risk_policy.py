from __future__ import annotations

import unittest
from datetime import datetime, timezone

from returnguard.intelligence.models import (
    CustomerIntelligence,
    InspectionRecord,
    NetworkIntelligence,
    ProductIntelligence,
    RecentPurchaseActivity,
    RelationshipValueProxy,
    ReturnBehaviorIntelligence,
    ReturnEconomics,
)
from returnguard.policy import PolicyAction, PolicyV1Service
from returnguard.risk import CoverageLevel, RiskBand, RiskPattern, RiskV1Engine
from returnguard.risk.engine import _band
from returnguard.risk.repository import BigQueryRiskEventRepository


AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)


def customer(**changes):
    values = dict(
        user_id=1, assessment_at=AT, transaction_history_available=True,
        history_duration_days=365, history_confidence="limited",
        first_observed_purchase_at=AT, customer_tenure_days=365,
        total_orders=10, total_items=10, total_spend=1000.0,
        average_order_value=100.0, average_item_value=100.0,
        purchase_frequency_per_month=1.0,
        recent_purchase_activity=RecentPurchaseActivity(items_30d=10, items_90d=10),
        days_since_last_purchase=1, distinct_products_purchased=5,
        distinct_categories_purchased=2, top_purchase_category=None,
        top_purchase_category_item_count=None, top_purchase_category_share=None,
        lifetime_historical_returns=0, lifetime_return_rate=0.0,
        returns_30d=0, returns_90d=0, days_since_last_return=None,
        return_frequency_relative_to_tenure=0.0, returned_goods_value=0.0,
        average_returned_item_value=None, returned_value_to_total_spend_ratio=0.0,
        distinct_returned_products=0, distinct_returned_categories=0,
        repeat_same_product_return_count=0, repeat_same_product_return=False,
        repeat_same_category_return_count=0, repeat_same_category_return=False,
        top_returned_category=None, top_returned_category_count=None,
        top_returned_category_share=None, purchased_value=1000.0,
        returned_value=0.0, high_value_return_frequency=None,
        purchase_to_return_timing_days=None, customer_history_data_origin="live_source",
        relationship_value_proxy=RelationshipValueProxy(
            tenure_days=365, total_orders=10, total_items=10, total_spend=1000,
            recent_activity=RecentPurchaseActivity(), returned_value=0,
            history_confidence="limited",
        ),
    )
    values.update(changes)
    return CustomerIntelligence(**values)


def behavior(**changes):
    values = dict(
        historical_return_count=0, lifetime_return_rate=0.0,
        returns_30d=0, returns_90d=0, days_since_last_return=None,
        recent_return_clustering={"returns_30d": 0, "returns_90d": 0,
                                  "lifetime_historical_returns": 0,
                                  "recent_cluster_present": None},
        returned_value=0.0, average_returned_value=None,
        returned_value_to_purchase_value_ratio=0.0,
        return_frequency_relative_to_purchase_volume=0.0,
        return_frequency_relative_to_tenure=0.0,
        repeated_returned_products={"current_product_count": 0,
                                    "current_product_repeated": False,
                                    "distinct_returned_products": 0},
        repeated_returned_categories={"current_category_count": 0,
                                      "current_category_repeated": False,
                                      "distinct_returned_categories": 0},
        purchase_to_return_timing_days=None, history_confidence="limited",
        data_origin="live_source",
    )
    values.update(changes)
    return ReturnBehaviorIntelligence(**values)


def product(**changes):
    values = dict(
        product_id=1, assessment_at=AT, product_items_observed=10,
        product_returns_observed=1, product_return_rate=.1,
        product_returned_value=100, product_average_sale_price=100,
        product_last_return_at=AT, product_history_confidence="moderate",
        category="Widgets", category_items_observed=100,
        category_returns_observed=10, category_return_rate=.1,
        category_returned_value=1000, category_average_sale_price=100,
        category_history_confidence="high",
        product_vs_category_return_rate_delta=0.0,
        product_return_rate_elevated=False, data_origin="live_source",
    )
    values.update(changes)
    return ProductIntelligence(**values)


def inspection(**changes):
    values = dict(
        return_id="RTN-1", product_id=1, actual_weight_kg=1.0,
        expected_serial=None, returned_serial=None, serial_mismatch=None,
        item_present=True, condition="ok", accessories_present=["cable"],
        expected_accessories=["cable"], inspection_location="warehouse",
        inspected_at=AT, expected_weight_kg=1.0, source_type="synthetic_demo",
        product_attributes_source_type="synthetic_demo",
        product_attributes_generator_version="v", scenario_id="ignored",
        generator_version="v",
    )
    values.update(changes)
    return InspectionRecord(**values)


def network(**changes):
    values = dict(
        user_id=1, assessment_at=AT, linked_user_count=0, linked_return_count=0,
        network_identifiers=[], relationship_types=[], linked_user_ids=[],
        first_observed_at=None, last_observed_at=None,
        recent_network_activity={"links_30d": 0, "links_90d": 0},
        relationship_strength=None, coordination_indicators=None,
        network_history_confidence="none", data_origin="insufficient_history",
        contextual_evidence_only=True,
    )
    values.update(changes)
    return NetworkIntelligence(**values)


def economics(**changes):
    values = dict(
        current_item_value=100.0, product_cost=50.0,
        reverse_logistics_cost=20.0, inspection_cost=5.0,
        recovery_value=30.0, total_operational_cost=25.0,
        estimated_net_return_cost=95.0, estimated_loss_exposure=None,
        economics_data_confidence="complete", data_origin="controlled_demo",
    )
    values.update(changes)
    return ReturnEconomics(**values)


def assess(**changes):
    values = dict(
        return_id="RTN-1", assessment_at=AT, assessment_id="A-1",
        customer=customer(), behavior=behavior(), product=product(),
        inspection=None, evidence=[], network=network(),
    )
    values.update(changes)
    return RiskV1Engine().assess(**values)


class RiskV1Tests(unittest.TestCase):
    def test_exact_band_boundaries(self):
        for score, expected in (
            (0, RiskBand.LOW), (24, RiskBand.LOW),
            (25, RiskBand.MEDIUM), (49, RiskBand.MEDIUM),
            (50, RiskBand.HIGH), (74, RiskBand.HIGH),
            (75, RiskBand.CRITICAL), (100, RiskBand.CRITICAL),
        ):
            self.assertEqual(_band(score), expected)

    def test_lifetime_rate_bands_and_minimum_sample(self):
        expected = ((.10, 0), (.20, 3), (.35, 6), (.36, 9))
        for rate, points in expected:
            result = assess(
                customer=customer(total_items=20),
                behavior=behavior(lifetime_return_rate=rate),
            )
            self.assertEqual(result.group_scores["CUSTOMER_BEHAVIOR"], points)
        result = assess(
            customer=customer(total_items=4),
            behavior=behavior(lifetime_return_rate=.9, historical_return_count=None),
        )
        self.assertEqual(result.group_scores["CUSTOMER_BEHAVIOR"], 0)
        self.assertIn(
            "Lifetime return rate unavailable for scoring: historical sample below 5 items.",
            result.limitations,
        )

    def test_lifetime_rate_sample_size_caps(self):
        for total_items, expected in ((4, 0), (5, 3), (10, 6), (20, 9)):
            result = assess(
                customer=customer(total_items=total_items),
                behavior=behavior(
                    lifetime_return_rate=.5,
                    returned_value_to_purchase_value_ratio=None,
                    repeated_returned_products={"current_product_count": None},
                ),
            )
            self.assertEqual(result.group_scores["CUSTOMER_BEHAVIOR"], expected)
            reasons = [
                reason for reason in result.reasons
                if reason.code == "ELEVATED_LIFETIME_RETURN_RATE"
            ]
            if total_items in (5, 10):
                self.assertIn("capped", reasons[0].explanation)

    def test_returned_value_ratio_sample_size_caps(self):
        for total_items, expected in ((1, 0), (5, 2), (10, 4), (20, 6)):
            result = assess(
                customer=customer(total_items=total_items),
                behavior=behavior(
                    lifetime_return_rate=None,
                    historical_return_count=None,
                    returned_value_to_purchase_value_ratio=.5,
                    repeated_returned_products={"current_product_count": None},
                ),
            )
            self.assertEqual(result.group_scores["CUSTOMER_BEHAVIOR"], expected)
            reasons = [
                reason for reason in result.reasons
                if reason.code == "HIGH_RETURNED_VALUE_RATIO"
            ]
            if total_items in (5, 10):
                self.assertIn("capped", reasons[0].explanation)
        self.assertIn(
            "Returned-value ratio unavailable for scoring: historical sample below 5 items.",
            assess(
                customer=customer(total_items=1),
                behavior=behavior(
                    lifetime_return_rate=None,
                    historical_return_count=None,
                    returned_value_to_purchase_value_ratio=1.0,
                    repeated_returned_products={"current_product_count": None},
                ),
            ).limitations,
        )

    def test_missing_customer_ratios_remain_neutral(self):
        result = assess(
            customer=customer(total_items=20),
            behavior=behavior(
                lifetime_return_rate=None,
                historical_return_count=None,
                returned_value_to_purchase_value_ratio=None,
                repeated_returned_products={"current_product_count": None},
            ),
        )
        self.assertEqual(result.group_scores["CUSTOMER_BEHAVIOR"], 0)
        self.assertFalse(result.reasons)

    def test_count_fallback_is_exclusive_and_controlled_safe(self):
        result = assess(behavior=behavior(
            lifetime_return_rate=None, historical_return_count=4,
            data_origin="controlled_demo",
        ))
        self.assertEqual(result.group_scores["CUSTOMER_BEHAVIOR"], 4)
        self.assertIn("COUNT_BASED_HISTORY_FALLBACK", [r.code for r in result.reasons])

    def test_recent_value_repeat_thresholds_and_customer_cap(self):
        result = assess(
            customer=customer(total_items=20),
            behavior=behavior(
                lifetime_return_rate=.5, returns_30d=6,
                returned_value_to_purchase_value_ratio=.5,
                repeated_returned_products={"current_product_count": 3},
            ),
        )
        self.assertEqual(result.group_scores["CUSTOMER_BEHAVIOR"], 25)

    def test_recent_and_returned_value_boundaries(self):
        recent = assess(behavior=behavior(returns_30d=5))
        self.assertEqual(
            next(r.contribution for r in recent.reasons
                 if r.code == "RECENT_RETURN_CONCENTRATION"),
            4,
        )
        value = assess(behavior=behavior(returned_value_to_purchase_value_ratio=.4))
        self.assertEqual(
            next(r.contribution for r in value.reasons
                 if r.code == "HIGH_RETURNED_VALUE_RATIO"),
            4,
        )

    def test_product_mitigation_and_limited_cap(self):
        full = assess(product=product(
            product_return_rate=.4, category_return_rate=.1,
            product_vs_category_return_rate_delta=.3,
            product_return_rate_elevated=True,
        ))
        self.assertEqual(full.product_mitigation, -6)
        limited = assess(product=product(
            product_return_rate=.4, category_return_rate=.1,
            product_vs_category_return_rate_delta=.3,
            product_return_rate_elevated=True,
            product_history_confidence="limited",
        ))
        self.assertEqual(limited.product_mitigation, -2)

    def test_inspection_rules_cap_and_avoid_missing_item_double_count(self):
        result = assess(inspection=inspection(
            expected_serial="private-a", returned_serial="private-b",
            serial_mismatch=True, item_present=False,
            actual_weight_kg=.1, expected_weight_kg=1,
            expected_accessories=["a", "b"], accessories_present=[],
        ))
        self.assertEqual(result.group_scores["INSPECTION"], 40)
        reason_points = {item.code: item.contribution for item in result.reasons}
        self.assertNotIn("WEIGHT_DEVIATION", reason_points)
        self.assertNotIn("ACCESSORY_MISMATCH", reason_points)
        rendered = result.model_dump_json()
        self.assertNotIn("private-a", rendered)
        self.assertNotIn("private-b", rendered)

    def test_weight_and_accessory_thresholds(self):
        weight = assess(inspection=inspection(actual_weight_kg=.7, expected_weight_kg=1))
        self.assertEqual(
            next(r.contribution for r in weight.reasons if r.code == "WEIGHT_DEVIATION"),
            12,
        )
        accessory = assess(inspection=inspection(
            expected_accessories=["a", "b"], accessories_present=["a"]
        ))
        self.assertEqual(
            next(r.contribution for r in accessory.reasons if r.code == "ACCESSORY_MISMATCH"),
            6,
        )
        one_of_three = assess(inspection=inspection(
            expected_accessories=["a", "b", "c"], accessories_present=["a", "b"]
        ))
        self.assertEqual(
            next(r.contribution for r in one_of_three.reasons
                 if r.code == "ACCESSORY_MISMATCH"),
            3,
        )

    def test_network_is_gated_and_capped(self):
        rich_network = network(
            linked_return_count=8,
            recent_network_activity={"links_30d": 5, "links_90d": 5},
            coordination_indicators={"shared_identifier_count": 4},
        )
        gated_off = assess(network=rich_network)
        self.assertEqual(gated_off.group_scores["NETWORK_CONTEXT"], 0)
        gated_on = assess(
            behavior=behavior(lifetime_return_rate=.3), network=rich_network
        )
        self.assertEqual(gated_on.group_scores["NETWORK_CONTEXT"], 10)
        self.assertIn(RiskPattern.POSSIBLE_LINKED_ACCOUNT_ABUSE, gated_on.patterns)

    def test_cross_signal_rules_are_exact(self):
        result = assess(
            behavior=behavior(
                lifetime_return_rate=.4,
                repeated_returned_products={"current_product_count": 1},
            ),
            inspection=inspection(serial_mismatch=True, item_present=False),
            network=network(
                linked_return_count=5,
                recent_network_activity={"links_30d": 4, "links_90d": 4},
            ),
        )
        self.assertEqual(result.group_scores["CROSS_SIGNAL"], 10)

    def test_coverage_gate_and_bands(self):
        empty_customer = customer(
            total_items=0,
            recent_purchase_activity=RecentPurchaseActivity(),
        )
        empty_behavior = behavior(
            historical_return_count=None, lifetime_return_rate=None,
            returns_30d=None, returned_value_to_purchase_value_ratio=None,
            repeated_returned_products={"current_product_count": None},
            data_origin="insufficient_history",
        )
        empty_product = product(
            product_return_rate=None, category_return_rate=None,
            product_vs_category_return_rate_delta=None,
            product_return_rate_elevated=None,
        )
        result = assess(
            customer=empty_customer, behavior=empty_behavior,
            product=empty_product, inspection=None,
        )
        self.assertIsNone(result.score)
        self.assertEqual(result.band, RiskBand.UNDETERMINED)
        self.assertEqual(result.coverage, CoverageLevel.INSUFFICIENT)
        self.assertIn(RiskPattern.AMBIGUOUS_OR_INSUFFICIENT_EVIDENCE, result.patterns)

        partial = assess(
            customer=empty_customer, behavior=empty_behavior,
            product=empty_product, inspection=inspection(serial_mismatch=False),
        )
        self.assertEqual(partial.score, 0)
        self.assertEqual(partial.band, RiskBand.LOW)
        self.assertEqual(partial.coverage, CoverageLevel.PARTIAL)

    def test_event_identity_is_retry_safe_and_reassessment_specific(self):
        first = assess()
        retry = assess()
        later = assess(assessment_id="A-2")
        self.assertEqual(first.risk_event_id, retry.risk_event_id)
        self.assertNotEqual(first.risk_event_id, later.risk_event_id)

    def test_reason_order_is_positive_descending_then_mitigation(self):
        result = assess(
            behavior=behavior(
                lifetime_return_rate=.4,
                returned_value_to_purchase_value_ratio=.5,
            ),
            product=product(
                product_return_rate=.4, category_return_rate=.1,
                product_vs_category_return_rate_delta=.3,
                product_return_rate_elevated=True,
            ),
        )
        contributions = [reason.contribution for reason in result.reasons]
        first_nonpositive = next(
            (index for index, value in enumerate(contributions) if value <= 0),
            len(contributions),
        )
        self.assertEqual(
            contributions[:first_nonpositive],
            sorted(contributions[:first_nonpositive], reverse=True),
        )
        self.assertTrue(all(value <= 0 for value in contributions[first_nonpositive:]))


class PolicyV1Tests(unittest.TestCase):
    def test_serial_mismatch_has_highest_priority(self):
        risk = assess(inspection=inspection(serial_mismatch=True))
        result = PolicyV1Service().evaluate(
            risk, economics(), inspection(serial_mismatch=True), []
        )
        self.assertEqual(result.action, PolicyAction.ESCALATE_TO_SPECIALIST)
        self.assertEqual(result.matched_rule, "P100_CONFIRMED_SERIAL_MISMATCH")

    def test_undetermined_never_rejects_for_missing_evidence(self):
        risk = assess(
            customer=customer(total_items=0, recent_purchase_activity=RecentPurchaseActivity()),
            behavior=behavior(
                historical_return_count=None, lifetime_return_rate=None,
                returns_30d=None, returned_value_to_purchase_value_ratio=None,
                repeated_returned_products={"current_product_count": None},
                data_origin="insufficient_history",
            ),
            product=product(
                product_return_rate=None, category_return_rate=None,
                product_vs_category_return_rate_delta=None,
                product_return_rate_elevated=None,
            ),
        )
        result = PolicyV1Service().evaluate(risk, economics(), None, [])
        self.assertEqual(result.action, PolicyAction.REQUIRE_INSPECTION)

    def test_low_cost_rule_uses_decimal_and_economics_not_risk(self):
        risk = assess()
        result = PolicyV1Service().evaluate(
            risk, economics(recovery_value=20.0), None, []
        )
        self.assertEqual(result.action, PolicyAction.REFUND_WITHOUT_RETURN)
        self.assertEqual(result.matched_rule, "P40_REFUND_WITHOUT_RETURN")
        self.assertIsNone(result.return_fee)

    def test_medium_inspected_policy_priority(self):
        medium = assess(inspection=inspection(serial_mismatch=False, item_present=False))
        result = PolicyV1Service().evaluate(
            medium, economics(), inspection(serial_mismatch=False, item_present=False), []
        )
        self.assertEqual(result.matched_rule, "P55_MEDIUM_INSPECTED")
        self.assertEqual(result.action, PolicyAction.MANUAL_REVIEW)

    def test_high_policy_priority(self):
        high = assess().model_copy(update={"score": 60, "band": RiskBand.HIGH})
        result = PolicyV1Service().evaluate(high, economics(), None, [])
        self.assertEqual(result.matched_rule, "P70_HIGH_RISK")
        self.assertEqual(result.action, PolicyAction.MANUAL_REVIEW)

    def test_remaining_policy_priority_rules(self):
        base = assess()
        critical = base.model_copy(update={"score": 80, "band": RiskBand.CRITICAL})
        physical = inspection(serial_mismatch=False, item_present=False)
        self.assertEqual(
            PolicyV1Service().evaluate(critical, economics(), physical, []).matched_rule,
            "P80_CRITICAL_PHYSICAL_DISCREPANCY",
        )
        medium = base.model_copy(update={"score": 30, "band": RiskBand.MEDIUM})
        self.assertEqual(
            PolicyV1Service().evaluate(medium, economics(), None, []).matched_rule,
            "P60_MEDIUM_NEEDS_INSPECTION",
        )
        low_substantial = base.model_copy(update={
            "score": 5, "band": RiskBand.LOW,
            "coverage": CoverageLevel.SUBSTANTIAL,
        })
        limited = economics(
            current_item_value=None, reverse_logistics_cost=None,
            inspection_cost=None, recovery_value=None,
            total_operational_cost=None, economics_data_confidence="none",
        )
        self.assertEqual(
            PolicyV1Service().evaluate(low_substantial, limited, None, []).matched_rule,
            "P20_LOW_SUBSTANTIAL",
        )
        low_partial = low_substantial.model_copy(update={"coverage": CoverageLevel.PARTIAL})
        self.assertEqual(
            PolicyV1Service().evaluate(low_partial, limited, None, []).matched_rule,
            "P10_LOW_DEFAULT",
        )

    def test_policy_v1_never_selects_reject_or_return_fee(self):
        for risk, costs, inspected in (
            (assess(), economics(), None),
            (assess(inspection=inspection(serial_mismatch=True)), economics(),
             inspection(serial_mismatch=True)),
        ):
            result = PolicyV1Service().evaluate(risk, costs, inspected, [])
            self.assertNotEqual(result.action, PolicyAction.REJECT_RETURN)
            self.assertNotEqual(result.action, PolicyAction.APPROVE_WITH_RETURN_FEE)
            self.assertIsNone(result.return_fee)


class FakeJob:
    def result(self):
        return []


class FakeClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, job_config):
        self.queries.append((sql, job_config))
        return FakeJob()


class RiskEventPersistenceTests(unittest.TestCase):
    def test_insert_only_merge_is_retry_safe_and_never_updates(self):
        client = FakeClient()
        repository = BigQueryRiskEventRepository(client=client)
        assessment = assess()
        repository.append(assessment)
        repository.append(assessment)
        self.assertEqual(len(client.queries), 2)
        sql = client.queries[0][0].upper()
        self.assertIn("MERGE", sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT", sql)
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertNotIn("UPDATE SET", sql)
        risk_event_parameter = next(
            parameter
            for parameter in client.queries[0][1].query_parameters
            if parameter.name == "risk_event_id"
        )
        self.assertEqual(risk_event_parameter.value, assessment.risk_event_id)


if __name__ == "__main__":
    unittest.main()
