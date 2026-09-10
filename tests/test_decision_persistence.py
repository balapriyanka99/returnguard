from __future__ import annotations

import json
import unittest
from datetime import timedelta
from decimal import Decimal

from returnguard.agents import (
    BigQueryDecisionAssessmentRepository,
    DecisionAssessment,
    DecisionSynthesisResult,
    decision_event_id,
)
from tests.test_decision_synthesis import decision_input


class FakeJob:
    def result(self):
        return []


class FakeClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, job_config):
        self.queries.append((sql, job_config))
        return FakeJob()


def parameter(config, name):
    return next(item for item in config.query_parameters if item.name == name)


class DecisionPersistenceTests(unittest.TestCase):
    def setUp(self):
        value = decision_input()
        self.result = DecisionSynthesisResult.model_validate({
            "return_id": value.return_id,
            "assessment_id": value.assessment_id,
            "assessment_at": value.assessment_at,
            "recommended_action": value.policy.action,
            "matched_policy_rule": value.policy.matched_rule,
            "risk_score": value.risk.score,
            "risk_band": value.risk.band,
            "risk_coverage": value.risk.coverage,
            "decision_summary": "A safe synthesized explanation.",
            "strongest_evidence": ["Deterministic inspection evidence."],
            "mitigating_context": ["Product context is mitigating."],
            "network_context": {
                "contribution": 4,
                "patterns": ["POSSIBLE_SHARED_RETURN_PATTERN"],
                "summary": "Controlled network context only.",
                "merchant_explanation": "Related return activity is contextual.",
                "limitations": [],
            },
            "economics_summary": value.economics,
            "policy_reasoning": value.policy.rationale,
            "limitations": [],
            "specialists_used": ["inspection_agent"],
            "data_origin": ["deterministic_intelligence", "controlled_demo"],
        })

    def test_event_id_is_stable_and_assessment_specific(self):
        first = decision_event_id(self.result)
        self.assertEqual(first, decision_event_id(self.result))
        changed = self.result.model_copy(update={
            "assessment_id": "later-assessment",
            "assessment_at": self.result.assessment_at + timedelta(hours=1),
        })
        self.assertNotEqual(first, decision_event_id(changed))

    def test_serialization_preserves_safe_nested_summaries_and_nullable_pricing(self):
        snapshot = DecisionAssessment.from_result(self.result)
        self.assertEqual(snapshot.decision_event_id, decision_event_id(self.result))
        self.assertEqual(snapshot.economics_summary_json["current_item_value"], 349.95)
        self.assertEqual(snapshot.network_context_json["contribution"], 4)
        self.assertIsNone(snapshot.pricing_json)
        encoded = json.dumps(snapshot.model_dump(mode="json"))
        self.assertNotIn("linked_user_id", encoded)
        self.assertNotIn("expected_serial", encoded)

    def test_pricing_json_serializes_when_present(self):
        pricing = {
            "normalized_reason": "CHANGED_MIND",
            "return_fee": Decimal("12.50"),
            "fee_reason": "Discretionary return.",
            "pricing_rationale": ["Sample-capped behavior adjustment."],
        }
        result = self.result.model_copy(update={
            "pricing": pricing,
        })
        snapshot = DecisionAssessment.from_result(result)
        self.assertEqual(snapshot.pricing_json["return_fee"], "12.50")
        self.assertEqual(snapshot.pricing_json["normalized_reason"], "CHANGED_MIND")

    def test_merge_is_insert_only_and_uses_safe_parameter_types(self):
        client = FakeClient()
        repository = BigQueryDecisionAssessmentRepository(client=client)
        snapshot = DecisionAssessment.from_result(self.result)
        repository.append(snapshot)
        repository.append(snapshot)
        self.assertEqual(len(client.queries), 2)
        sql = client.queries[0][0].upper()
        self.assertIn("MERGE", sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT", sql)
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertNotIn("UPDATE SET", sql)
        config = client.queries[0][1]
        self.assertEqual(
            parameter(config, "decision_event_id").value,
            snapshot.decision_event_id,
        )
        self.assertEqual(
            json.loads(parameter(config, "economics_summary_json").value)[
                "current_item_value"
            ],
            349.95,
        )
        self.assertEqual(parameter(config, "strongest_evidence").values,
                         ["Deterministic inspection evidence."])

    def test_repository_never_persists_raw_sensitive_fields(self):
        value = self.result.model_copy(update={
            "strongest_evidence": ["serial_mismatch detected"],
            "mitigating_context": ["image_uri=gs://private/object"],
        })
        snapshot = DecisionAssessment.from_result(value)
        encoded = json.dumps(snapshot.model_dump(mode="json"))
        self.assertNotIn("gs://", encoded)
        self.assertNotIn("image_uri", encoded)
        self.assertNotIn("fraud_labels", encoded)
        self.assertIn("serial_mismatch detected", encoded)


if __name__ == "__main__":
    unittest.main()
