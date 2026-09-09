from __future__ import annotations

import logging
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from returnguard.intelligence.models import EvidenceRecord, StructuredModel
from returnguard.mcp import (
    TOOL_NAMES,
    IntelligenceToolRequest,
    ReturnGuardMCPTools,
    RiskAssessmentRequest,
    RiskAssessmentResult,
    RiskEngineUnavailableError,
)
from returnguard.observability import ExecutionContext


AT = datetime(2026, 9, 2, tzinfo=timezone.utc)


@dataclass
class FakeIntelligenceResult(StructuredModel):
    value: int | None
    data_origin: str


class FakeIntelligenceService:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False

    def _result(self, name, return_id, assessment_at=None):
        self.calls.append((name, return_id, assessment_at))
        if self.fail:
            raise ValueError("delegate failed")
        return FakeIntelligenceResult(7, "controlled_demo" if name == "network" else "live_source")

    def resolve_assessment_at(self, return_id, assessment_at=None):
        return assessment_at or AT

    def get_customer_intelligence(self, return_id, assessment_at=None):
        return self._result("customer", return_id, assessment_at)

    def get_product_intelligence(self, return_id, assessment_at=None):
        return self._result("product", return_id, assessment_at)

    def get_return_behavior_intelligence(self, return_id, assessment_at=None):
        return self._result("return_behavior", return_id, assessment_at)

    def get_network_intelligence(self, return_id, assessment_at=None):
        return self._result("network", return_id, assessment_at)

    def get_return_economics(self, return_id, assessment_at=None):
        return self._result("economics", return_id, assessment_at)

    def get_evidence(self, return_id, assessment_at=None):
        self.calls.append(("evidence", return_id, assessment_at))
        return [EvidenceRecord(
            evidence_id="E-1", return_id=return_id, evidence_type="photo", stage="submitted",
            image_uri="gs://private/signed-image", reference_image_uri=None,
            submitted_at=AT, source="synthetic_demo", uploaded_by="customer",
        )]

    def get_inspection(self, return_id, assessment_at=None):
        return self._result("inspection", return_id, assessment_at)


class FakeRiskEngine:
    def __init__(self) -> None:
        self.request = None

    def assess_risk(self, request):
        self.request = request
        return RiskAssessmentResult(
            return_id=request.return_id, assessment_at=request.assessment_at,
            risk_score=0.5, risk_level="test_fixture", factors=[{"name": "fixture"}],
            data_origin="test_fake",
        )


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class MCPToolTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeIntelligenceService()
        self.risk = FakeRiskEngine()
        self.tools = ReturnGuardMCPTools(self.service, self.risk)
        self.context = ExecutionContext(trace_id="trace-1", assessment_id="assessment-1")
        self.request = IntelligenceToolRequest("RTN-M08-001", AT, self.context)

    def test_exact_eight_tools_exist(self):
        self.assertEqual(len(TOOL_NAMES), 8)
        self.assertEqual(set(TOOL_NAMES), {
            "get_customer_intelligence", "get_product_intelligence", "get_return_behavior",
            "get_network_intelligence", "calculate_return_economics", "get_return_evidence",
            "get_return_inspection", "assess_risk",
        })
        for name in TOOL_NAMES:
            self.assertTrue(callable(getattr(self.tools, name)))

    def test_first_seven_delegate_and_propagate_inputs(self):
        calls = (
            ("get_customer_intelligence", "customer", True),
            ("get_product_intelligence", "product", True),
            ("get_return_behavior", "return_behavior", True),
            ("get_network_intelligence", "network", True),
            ("calculate_return_economics", "economics", True),
            ("get_return_evidence", "evidence", True),
            ("get_return_inspection", "inspection", True),
        )
        for tool_name, delegate_name, receives_assessment in calls:
            result = getattr(self.tools, tool_name)(self.request)
            self.assertIsInstance(result.to_dict(), dict)
            self.assertEqual(result.return_id, "RTN-M08-001")
            self.assertEqual(result.assessment_at, AT)
            expected_at = AT if receives_assessment else None
            self.assertIn((delegate_name, "RTN-M08-001", expected_at), self.service.calls)

    def test_structured_missing_and_controlled_provenance_are_preserved(self):
        network = self.tools.get_network_intelligence(self.request)
        self.assertEqual(network.result["data_origin"], "controlled_demo")
        self.service._result = lambda *args, **kwargs: FakeIntelligenceResult(None, "insufficient_history")
        customer = self.tools.get_customer_intelligence(self.request)
        self.assertIsNone(customer.result["value"])
        self.assertEqual(customer.result["data_origin"], "insufficient_history")

    def test_omitted_assessment_uses_stored_timestamp_in_delegate_and_envelope(self):
        request = IntelligenceToolRequest("RTN-M08-001", None, self.context)
        result = self.tools.get_return_inspection(request)
        self.assertEqual(result.assessment_at, AT)
        self.assertIn(("inspection", "RTN-M08-001", AT), self.service.calls)

    def test_evidence_reports_uri_availability_without_visual_findings(self):
        result = self.tools.get_return_evidence(self.request)
        self.assertTrue(result.result["visual_evidence_available"])
        self.assertEqual(result.result["visual_evidence_state"], "available")
        self.assertNotIn("visual_findings", result.result)

    def test_logging_context_and_failure_propagation(self):
        handler = RecordingHandler()
        tool_logger = logging.getLogger("returnguard.mcp.tools")
        tool_logger.addHandler(handler)
        tool_logger.setLevel(logging.INFO)
        try:
            self.tools.get_product_intelligence(self.request)
            completed = next(record for record in handler.records if record.event == "completed")
            self.assertEqual(completed.trace_id, "trace-1")
            self.assertEqual(completed.assessment_id, "assessment-1")
            self.assertEqual(completed.return_id, "RTN-M08-001")
            self.assertEqual(completed.tool_name, "get_product_intelligence")
            self.service.fail = True
            with self.assertRaisesRegex(ValueError, "delegate failed"):
                self.tools.get_product_intelligence(self.request)
            self.assertTrue(any(record.event == "failed" for record in handler.records))
            self.assertNotIn("gs://private/signed-image", " ".join(r.getMessage() for r in handler.records))
        finally:
            tool_logger.removeHandler(handler)

    def test_assess_risk_only_delegates_to_injected_boundary(self):
        request = RiskAssessmentRequest(
            return_id="RTN-M08-001", assessment_at=AT,
            structured_findings={"customer": {"history": "controlled_demo"}},
            context=self.context,
        )
        result = self.tools.assess_risk(request)
        self.assertIs(self.risk.request, request)
        self.assertEqual(result.data_origin, "test_fake")
        with self.assertLogs("returnguard.mcp.tools", level="ERROR"):
            with self.assertRaises(RiskEngineUnavailableError):
                ReturnGuardMCPTools(self.service).assess_risk(request)

    def test_mcp_has_no_direct_data_or_event_dependency_or_risk_algorithm(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path("returnguard/mcp").glob("*.py")
        )
        self.assertNotIn("google.cloud", source)
        self.assertNotIn(".query(", source)
        self.assertNotIn("source_events_snapshot", source)
        self.assertNotIn("synthetic_network_links", source)
        self.assertNotIn("risk_score =", source)


if __name__ == "__main__":
    unittest.main()
