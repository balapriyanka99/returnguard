from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from returnguard.intelligence import product
from returnguard.intelligence.service import ReturnIntelligenceService


AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self) -> None:
        scenarios = {
            "RTN-S01-001": ("S01", 1, 11, 101, None),
            "RTN-S02-001": ("S02", 2, 12, 102, 4),
            "RTN-S12V2-001": ("S12V2", 3, 13, 103, None),
            "RTN-M01-001": ("M01", 4, 14, 104, 5),
            "RTN-M08-001": ("M08", 5, 15, 105, None),
            "RTN-NOHISTORY": ("S01", 6, 16, 106, None),
        }
        self.returns = {
            rid: {
                "return_id": rid, "scenario_id": values[0], "generator_version": "rg-synth-v2.0.0",
                "user_id": values[1], "order_item_id": values[2], "order_id": 1000 + values[2],
                "product_id": values[3], "assessment_at": AT,
                "requested_at": AT - timedelta(days=3), "reason": "defective",
                "responsibility": "merchant_fault", "status": "REQUESTED",
                "source_type": "synthetic_demo", "customer_historical_return_count": values[4],
            }
            for rid, values in scenarios.items()
        }
        self.calls = []

    def get_return(self, return_id):
        return self.returns.get(return_id)

    def get_customer_history(self, user_id, product_id, category, assessment_at, current_order_item_id):
        self.calls.append(("customer", assessment_at, current_order_item_id))
        if user_id == 6:
            return {"total_orders": 0, "total_items": 0, "total_spend": 0}
        return {
            "total_orders": 3, "total_items": 4, "total_spend": 400.0,
            "average_order_value": 400 / 3, "average_item_value": 100.0,
            "first_observed_purchase_at": AT - timedelta(days=120),
            "last_purchase_at": AT - timedelta(days=10),
            "distinct_products_purchased": 3, "distinct_categories_purchased": 2,
            "orders_30d": 1, "items_30d": 1, "orders_90d": 2, "items_90d": 3,
            "live_return_count": 1, "returns_30d": 1, "returns_90d": 1,
            "last_return_at": AT - timedelta(days=5), "returned_value": 120.0,
            "distinct_returned_products": 1, "distinct_returned_categories": 1,
            "same_product_return_count": 1, "same_category_return_count": 1,
            "purchase_to_return_timing_days": 8.0, "top_purchase_category": "Jeans",
            "top_purchase_category_item_count": 3, "top_returned_category": "Jeans",
            "top_returned_category_count": 1,
        }

    def get_product_history(self, product_id, assessment_at, current_order_item_id):
        self.calls.append(("product", assessment_at, current_order_item_id))
        if product_id == 106:
            return {"category": "Jeans", "product_items_observed": 0,
                    "product_returns_observed": 0, "category_items_observed": 0,
                    "category_returns_observed": 0}
        returns = 1 if product_id in (103, 105) else 0
        return {"category": "Jeans", "product_items_observed": 4,
                "product_returns_observed": returns, "product_returned_value": 90.0 if returns else 0,
                "product_average_sale_price": 80.0,
                "product_last_return_at": AT - timedelta(days=20) if returns else None,
                "category_items_observed": 25, "category_returns_observed": 3,
                "category_returned_value": 220.0, "category_average_sale_price": 70.0}

    def get_network_links(self, return_id, assessment_at):
        self.calls.append(("network", assessment_at, return_id))
        if return_id not in ("RTN-M01-001", "RTN-M08-001"):
            return []
        user = self.returns[return_id]["user_id"]
        return [{"return_id": return_id, "user_id": user, "linked_user_id": 999,
                 "network_identifier": f"ip-{return_id}", "relationship_type": "shared_ip",
                 "first_observed_at": AT - timedelta(days=20),
                 "last_observed_at": AT - timedelta(days=1), "source_type": "synthetic_demo"}]

    def get_evidence(self, return_id):
        return [{"evidence_id": f"E-{return_id}", "return_id": return_id,
                 "type": "photo", "stage": "customer_submission", "image_uri": "gs://example/image",
                 "reference_image_uri": None, "observed_at": AT, "source_type": "synthetic_demo",
                 "uploaded_by": "customer", "claim_metadata": '{"claim": "defective"}'}]

    def get_inspection(self, return_id):
        if return_id == "RTN-S01-001":
            return None
        return {"return_id": return_id, "actual_weight_kg": 1.0,
                "expected_serial": "EXPECTED", "returned_serial": "EXPECTED-SWAP",
                "item_present": True, "condition": "defective", "accessories_present": '["cable"]',
                "product_id": self.returns[return_id]["product_id"],
                "expected_accessories": '["cable", "manual"]',
                "inspection_location": "warehouse", "inspected_at": AT,
                "expected_weight_kg": 1.0, "source_type": "synthetic_demo",
                "product_attributes_source_type": "synthetic_demo",
                "product_attributes_generator_version": "v2",
                "scenario_id": self.returns[return_id]["scenario_id"], "generator_version": "v2"}

    def get_economics(self, return_id):
        return {"current_item_value": 300, "product_cost": 120,
                "logistics_reverse_cost": 45, "reverse_logistics_cost": 55,
                "inspection_cost": 10, "recovery_value": 100, "source_type": "synthetic_demo"}


class IntelligenceServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepository()
        self.service = ReturnIntelligenceService(self.repo)

    def test_normal_case_uses_live_history_and_structured_shape(self):
        result = self.service.get_return_intelligence("RTN-S01-001")
        self.assertEqual(result.customer.customer_history_data_origin, "live_source")
        self.assertEqual(result.customer.lifetime_return_rate, 0.25)
        self.assertEqual(result.economics.total_operational_cost, 65.0)
        serialized = result.to_dict()
        self.assertIsInstance(serialized["customer"], dict)
        self.assertEqual(serialized["return_metadata"]["assessment_at"], AT.isoformat())

    def test_controlled_customer_count_wins_without_fabricated_rate(self):
        result = self.service.get_customer_intelligence("RTN-S02-001")
        self.assertEqual(result.lifetime_historical_returns, 4)
        self.assertEqual(result.customer_history_data_origin, "controlled_demo")
        self.assertIsNone(result.lifetime_return_rate)

    def test_s12v2_product_has_leave_one_out_return_history(self):
        result = self.service.get_product_intelligence("RTN-S12V2-001")
        self.assertEqual(result.product_returns_observed, 1)
        self.assertEqual(result.product_return_rate, 0.25)
        self.assertIn(("product", AT, 13), self.repo.calls)

    def test_network_case_is_context_not_fraud(self):
        result = self.service.get_network_intelligence("RTN-M01-001")
        self.assertEqual(result.linked_user_ids, [999])
        self.assertEqual(result.linked_return_count, 1)
        self.assertEqual(result.data_origin, "controlled_demo")
        self.assertTrue(result.contextual_evidence_only)

    def test_multisignal_m08_aggregate(self):
        result = self.service.get_return_intelligence("RTN-M08-001")
        self.assertEqual(result.product.product_returns_observed, 1)
        self.assertEqual(result.network.linked_user_count, 1)
        self.assertEqual(result.economics.current_item_value, 300.0)
        self.assertEqual(result.inspection.condition, "defective")
        self.assertEqual(result.inspection.expected_serial, "EXPECTED")
        self.assertEqual(result.inspection.returned_serial, "EXPECTED-SWAP")
        self.assertTrue(result.inspection.serial_mismatch)
        self.assertEqual(result.inspection.accessories_present, ["cable"])
        self.assertEqual(result.inspection.expected_accessories, ["cable", "manual"])
        self.assertEqual(result.inspection.product_attributes_source_type, "synthetic_demo")
        self.assertEqual(result.evidence[0].metadata["claim"], "defective")

    def test_missing_history_is_nullable_not_false_zero_rate(self):
        result = self.service.get_return_intelligence("RTN-NOHISTORY")
        self.assertFalse(result.customer.transaction_history_available)
        self.assertIsNone(result.customer.lifetime_historical_returns)
        self.assertIsNone(result.customer.lifetime_return_rate)
        self.assertEqual(result.customer.customer_history_data_origin, "insufficient_history")
        self.assertIsNone(result.product.product_return_rate)
        self.assertEqual(result.product.product_history_confidence, "none")

    def test_explicit_reassessment_timestamp_reaches_history_queries(self):
        later = AT + timedelta(days=30)
        result = self.service.get_return_intelligence("RTN-S01-001", assessment_at=later)
        self.assertEqual(result.return_metadata.assessment_at, later)
        self.assertIn(("product", later, 11), self.repo.calls)
        self.assertIn(("customer", later, 11), self.repo.calls)

    def test_facade_delegates_product_capability(self):
        with patch(
            "returnguard.intelligence.service.product.build_product_intelligence",
            wraps=product.build_product_intelligence,
        ) as capability:
            self.service.get_product_intelligence("RTN-S12V2-001")
        capability.assert_called_once()


if __name__ == "__main__":
    unittest.main()
