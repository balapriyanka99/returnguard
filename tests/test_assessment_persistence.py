from __future__ import annotations

import json
import unittest
from datetime import timedelta
from decimal import Decimal

from returnguard.economics import (
    BigQueryEconomicsAssessmentRepository,
    EconomicsAssessment,
    economics_event_id,
)
from returnguard.policy import (
    BigQueryPolicyAssessmentRepository,
    NormalizedReturnReason,
    PolicyV1Service,
    ReturnPolicyService,
    policy_event_id,
)
from tests.test_risk_policy import AT, assess, customer, economics


class FakeJob:
    def __init__(self, rows=None):
        self.rows = rows or []

    def result(self):
        return self.rows


class FakeClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, job_config):
        self.queries.append((sql, job_config))
        return FakeJob()


def parameter(job_config, name):
    return next(item for item in job_config.query_parameters if item.name == name)


def p30_policy():
    return PolicyV1Service().evaluate(
        assess(),
        economics(
            current_item_value=200.0,
            reverse_logistics_cost=10.0,
            inspection_cost=5.0,
            recovery_value=100.0,
            total_operational_cost=15.0,
        ),
        None,
        [],
        normalized_reason=NormalizedReturnReason.CHANGED_MIND,
        customer_total_items=20,
    )


class EventIdentityTests(unittest.TestCase):
    def test_economics_event_identity_is_stable_and_assessment_specific(self):
        first = economics_event_id("RTN-1", "A-1", AT)
        self.assertEqual(first, economics_event_id("RTN-1", "A-1", AT))
        self.assertNotEqual(
            first, economics_event_id("RTN-1", "A-2", AT + timedelta(hours=1))
        )

    def test_policy_event_identity_is_stable_and_assessment_specific(self):
        evaluation = p30_policy()
        first = policy_event_id(evaluation)
        self.assertEqual(first, policy_event_id(evaluation))
        later = evaluation.model_copy(update={
            "assessment_id": "A-2",
            "assessment_at": AT + timedelta(hours=1),
        })
        self.assertNotEqual(first, policy_event_id(later))


class EconomicsPersistenceTests(unittest.TestCase):
    def test_snapshot_uses_all_authoritative_economics_fields_and_decimal(self):
        result = economics(
            current_item_value=123.4567891234,
            estimated_loss_exposure=7.125,
        )
        snapshot = EconomicsAssessment.from_result(
            return_id="RTN-1",
            assessment_id="A-1",
            assessment_at=AT,
            result=result,
        )
        self.assertEqual(snapshot.current_item_value, Decimal("123.456789123"))
        self.assertEqual(snapshot.product_cost, Decimal("50.000000000"))
        self.assertEqual(snapshot.estimated_loss_exposure, Decimal("7.125000000"))
        self.assertEqual(snapshot.estimated_net_return_cost, Decimal("95.000000000"))
        self.assertEqual(snapshot.confidence, "complete")
        self.assertEqual(snapshot.data_origin, "controlled_demo")

    def test_merge_is_insert_only_and_numeric_parameters_are_decimal(self):
        client = FakeClient()
        repository = BigQueryEconomicsAssessmentRepository(client=client)
        snapshot = EconomicsAssessment.from_result(
            return_id="RTN-1",
            assessment_id="A-1",
            assessment_at=AT,
            result=economics(),
        )
        repository.append(snapshot)
        repository.append(snapshot)
        self.assertEqual(len(client.queries), 2)
        sql = client.queries[0][0].upper()
        self.assertIn("MERGE", sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT", sql)
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertNotIn("UPDATE SET", sql)
        config = client.queries[0][1]
        self.assertIsInstance(parameter(config, "current_item_value").value, Decimal)
        self.assertEqual(
            parameter(config, "economics_event_id").value,
            snapshot.economics_event_id,
        )


class PolicyPersistenceTests(unittest.TestCase):
    def test_p30_serializes_typed_economics_and_pricing(self):
        client = FakeClient()
        repository = BigQueryPolicyAssessmentRepository(client=client)
        evaluation = p30_policy()
        repository.append(evaluation)
        config = client.queries[0][1]
        economics_json = json.loads(parameter(config, "economics_json").value)
        pricing_json = json.loads(parameter(config, "pricing_json").value)
        self.assertEqual(economics_json["current_item_value"], "200.00")
        self.assertEqual(pricing_json["pricing_base_fee"], "10.00")
        self.assertEqual(pricing_json["pricing_capped_multiplier"], "1.00")
        self.assertEqual(pricing_json["pricing_behavior_signal_count"], 0)
        self.assertEqual(parameter(config, "return_fee").value, Decimal("10.00"))

    def test_non_p30_pricing_json_is_null(self):
        client = FakeClient()
        repository = BigQueryPolicyAssessmentRepository(client=client)
        evaluation = PolicyV1Service().evaluate(assess(), economics(), None, [])
        repository.append(evaluation)
        self.assertIsNone(parameter(client.queries[0][1], "pricing_json").value)
        self.assertIsNone(evaluation.pricing)

    def test_merge_is_insert_only_and_retry_safe(self):
        client = FakeClient()
        repository = BigQueryPolicyAssessmentRepository(client=client)
        evaluation = p30_policy()
        repository.append(evaluation)
        repository.append(evaluation)
        self.assertEqual(len(client.queries), 2)
        sql = client.queries[0][0].upper()
        self.assertIn("WHEN NOT MATCHED THEN INSERT", sql)
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertNotIn("UPDATE SET", sql)
        self.assertEqual(
            parameter(client.queries[0][1], "policy_event_id").value,
            policy_event_id(evaluation),
        )


class Recorder:
    def __init__(self):
        self.values = []

    def append(self, value):
        self.values.append(value)


class FakeSourceRepository:
    def get_return(self, return_id):
        return {"reason": "changed_mind"}


class FakeIntelligence:
    def __init__(self):
        self.repository = FakeSourceRepository()

    def get_return_economics(self, return_id, assessment_at):
        return economics()

    def get_inspection(self, return_id, assessment_at):
        return None

    def get_evidence(self, return_id, assessment_at):
        return []

    def get_customer_intelligence(self, return_id, assessment_at):
        return customer(total_items=20)


class FakeRiskService:
    def assess(self, return_id, assessment_at=None, *, assessment_id=None):
        return assess().model_copy(update={
            "return_id": return_id,
            "assessment_at": assessment_at or AT,
            "assessment_id": assessment_id,
        })


class OptInPersistenceTests(unittest.TestCase):
    def test_evaluate_remains_zero_write_and_recording_is_explicit(self):
        economics_repository = Recorder()
        policy_repository = Recorder()
        service = ReturnPolicyService(
            FakeIntelligence(),
            risk=FakeRiskService(),
            economics_repository=economics_repository,
            policy_repository=policy_repository,
        )
        service.evaluate("RTN-1", AT, assessment_id="A-1")
        self.assertEqual(economics_repository.values, [])
        self.assertEqual(policy_repository.values, [])

        _, policy, economics_assessment = service.evaluate_and_record(
            "RTN-1", AT, assessment_id="A-1"
        )
        self.assertEqual(economics_repository.values, [economics_assessment])
        self.assertEqual(policy_repository.values, [policy])

    def test_recording_without_both_repositories_fails_before_evaluation(self):
        service = ReturnPolicyService(FakeIntelligence(), risk=FakeRiskService())
        with self.assertRaisesRegex(RuntimeError, "required to persist"):
            service.evaluate_and_record("RTN-1", AT, assessment_id="A-1")


if __name__ == "__main__":
    unittest.main()
