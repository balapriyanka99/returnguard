import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException

from returnguard.api.app import ApiDependencies, create_app
from returnguard.audit import AuditEvent, audit_event_id
from returnguard.intelligence import ReturnNotFoundError
from returnguard.observability import sanitize_merchant_text


class _Model:
    def __init__(self, **values):
        self.values = values

    def model_dump(self, **kwargs):
        return dict(self.values)


class FakeRepository:
    def __init__(self):
        self.rows = [{
            "return_id": "RTN-1", "order_id": 10, "status": "requested",
            "reason": "changed_mind", "requested_at": "2026-09-02T00:00:00+00:00",
            "anchor_sale_price": 25.0,
        }]

    def list_returns(self, limit=50):
        return self.rows[:limit]

    def get_return(self, return_id):
        return self.rows[0] if return_id == "RTN-1" else None


class FakeIntelligence:
    def __init__(self):
        self.repository = FakeRepository()

    def resolve_assessment_at(self, return_id, assessment_at=None):
        if self.repository.get_return(return_id) is None:
            raise ReturnNotFoundError(return_id)
        return assessment_at or datetime(2026, 9, 2, tzinfo=timezone.utc)

    def get_return_intelligence(self, return_id):
        if self.repository.get_return(return_id) is None:
            raise ReturnNotFoundError(return_id)
        return SimpleNamespace(
            return_metadata=SimpleNamespace(
                return_id=return_id, source_type="controlled", order_id=10,
                order_item_id=11, user_id=12, product_id=20,
                reason="changed_mind", status="requested",
                requested_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                assessment_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            ),
            customer=SimpleNamespace(
                customer_history_data_origin="frozen", customer_tenure_days=120,
                total_orders=4, total_spend=100.0, lifetime_historical_returns=1,
                lifetime_return_rate=0.25, returns_30d=1,
            ),
            product=SimpleNamespace(
                category="Accessories", data_origin="frozen",
                product_return_rate=0.1, product_items_observed=10,
                category_return_rate=0.08, product_return_rate_elevated=False,
            ),
            return_behavior=_Model(history_confidence="limited"),
            inspection=None, evidence=[], economics=SimpleNamespace(current_item_value=25.0),
            network=SimpleNamespace(
                contextual_evidence_only=True, linked_return_count=0,
                relationship_types=[], network_history_confidence="unavailable",
            ),
        )


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event):
        if all(item.audit_event_id != event.audit_event_id for item in self.events):
            self.events.append(event)

    def list_for_return(self, return_id):
        return [event.model_dump(mode="json") for event in sorted(
            self.events, key=lambda item: item.event_at
        ) if event.return_id == return_id]


class FakeReads:
    def create_return(self, **kwargs):
        return {"return_id": kwargs["return_id"], "source_type": "source_backed",
                "status": "REQUESTED", "requested_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
                "assessment_at": None}

    def latest_for_return(self, return_id):
        return {
            "risk": {"score": 23, "band": "LOW", "coverage": "SUBSTANTIAL",
                     "group_scores_json": {"INSPECTION": 25}, "product_mitigation": -2,
                     "reasons_json": [{"code": "SERIAL_MISMATCH", "contribution": 25,
                                       "explanation": "Serial mismatch detected.", "group": "INSPECTION"}],
                     "patterns": ["POSSIBLE_SERIAL_SUBSTITUTION"], "limitations": []},
            "economics": {"current_item_value": 349.95, "reverse_logistics_cost": 11,
                          "inspection_cost": 5, "recovery_value": 250,
                          "estimated_net_return_cost": 115, "assessment_at": "2026-09-02T00:00:00Z"},
            "policy": {"action": "ESCALATE_TO_SPECIALIST", "matched_rule": "P100_CONFIRMED_SERIAL_MISMATCH",
                       "policy_version": "policy-v1", "rationale": ["Inspection safeguard."]},
            "decision": {"decision_summary": "Escalate due to a deterministic inspection discrepancy.",
                         "strongest_evidence": ["Serial mismatch detected."],
                         "mitigating_context": ["Product context reduced suspiciousness."], "limitations": []},
        }

    def assessment_history(self, return_id):
        return [{"assessment_id": "A1", "assessment_at": "2026-09-02T00:00:00Z",
                 "score": 23, "band": "LOW"}]


class ApiAuditTests(unittest.TestCase):
    def setUp(self):
        intelligence = FakeIntelligence()
        audit = FakeAudit()
        self.audit = audit
        self.app = create_app(ApiDependencies(
            intelligence=intelligence, orchestrator=object(),
            policy_service=object(), audit=audit, reads=FakeReads(),
        ))

    def _endpoint(self, path: str, method: str | None = None):
        return next(route.endpoint for route in self.app.routes
                    if getattr(route, "path", None) == path
                    and (method is None or method in getattr(route, "methods", set())))

    def test_health(self):
        self.assertEqual(self._endpoint("/health")(), {"status": "ok", "service": "returnguard"})

    def test_merchant_text_keeps_derived_findings_visible(self):
        text = sanitize_merchant_text(
            "A serial mismatch and shared return pattern were detected; expected_serial=RG-SECRET."
        )
        self.assertIn("serial mismatch", text)
        self.assertIn("shared return pattern", text)
        self.assertNotIn("RG-SECRET", text)

    def test_queue_sql_contract_uses_requested_at(self):
        from returnguard.intelligence.repository import BigQueryIntelligenceRepository

        class Job:
            def result(self):
                return []

        class Client:
            def __init__(self):
                self.sql = None
            def query(self, sql, **kwargs):
                self.sql = sql
                return Job()

        client = Client()
        BigQueryIntelligenceRepository(client=client).list_returns(5)
        self.assertIn("requested_at", client.sql)
        self.assertIn("ORDER BY requested_at DESC", client.sql)
        self.assertNotIn("created_at", client.sql)

    def test_queue_and_detail(self):
        queue = self._endpoint("/api/returns")(
            limit=5, offset=0, q="RTN-1", status=None, source_type=None, risk_band=None
        )
        self.assertEqual(queue["items"][0]["return_id"], "RTN-1")
        detail = self._endpoint("/api/returns/{return_id}")(return_id="RTN-1")
        self.assertEqual(detail["customer"]["metrics"]["order_count"], 4)
        self.assertEqual(detail["risk"]["score"], 23)
        self.assertEqual(detail["decision"]["recommended_action"], "ESCALATE_TO_SPECIALIST")

    def test_unknown_return_is_404(self):
        with self.assertRaises(HTTPException) as raised:
            self._endpoint("/api/returns/{return_id}")(return_id="UNKNOWN")
        self.assertEqual(raised.exception.status_code, 404)

    def test_create_return_appends_requested_event(self):
        endpoint = self._endpoint("/api/returns", "POST")
        result = endpoint(type("Request", (), {"order_item_id": 11, "customer_id": 12,
                                                "order_id": 10, "reason": "Changed my mind",
                                                "comment": None})())
        self.assertTrue(result["return_id"].startswith("RTN-USER-"))
        self.assertEqual(result["status"], "REQUESTED")
        self.assertEqual(self.audit.events[0].event_type, "RETURN_REQUESTED")

    def test_invalid_unavailable_intent_is_422(self):
        endpoint = self._endpoint("/api/returns/{return_id}/investigate")
        with self.assertRaises(HTTPException) as raised:
            import asyncio
            asyncio.run(endpoint("RTN-1", type("Request", (), {"intent": "vision_review"})()))
        self.assertEqual(raised.exception.status_code, 422)

    def test_audit_ids_are_stable_and_timeline_is_ordered(self):
        when = datetime(2026, 9, 2, tzinfo=timezone.utc)
        first = AuditEvent.create(
            return_id="RTN-1", assessment_id="A1", event_type="ASSESSMENT_STARTED",
            event_at=when, actor_type="api", summary="started",
        )
        retry = AuditEvent.create(
            return_id="RTN-1", assessment_id="A1", event_type="ASSESSMENT_STARTED",
            event_at=when, actor_type="api", summary="started",
        )
        self.assertEqual(first.audit_event_id, retry.audit_event_id)
        self.assertEqual(audit_event_id("RTN-1", "A1", "ASSESSMENT_STARTED", when), first.audit_event_id)
        self.audit.append(first)
        self.audit.append(retry)
        timeline = self._endpoint("/api/returns/{return_id}/timeline")(return_id="RTN-1")
        self.assertEqual(len(timeline), 1)


if __name__ == "__main__":
    unittest.main()
