from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from returnguard.agents.contracts import (
    AgentConfidence,
    AgentStatus,
    Finding,
    MissingCapability,
    OrchestrationResult,
    SpecialistResult,
    ToolEvidence,
    WorkflowIntent,
)
from returnguard.observability import (
    log_orchestration_summary,
    log_policy_summary,
    log_risk_summary,
    log_specialist_summary,
    logged_operation,
)
from returnguard.policy import PolicyAction, PolicyEconomicsSummary, PolicyEvaluation
from returnguard.risk import CoverageLevel, RiskAssessment, RiskBand, RiskPattern, RiskReason


AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)


class _LogCapture:
    def __init__(self, name: str) -> None:
        self.logger = logging.getLogger(name)
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = self.records.append

    def __enter__(self):
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *_):
        self.logger.removeHandler(self.handler)

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)


def specialist(name: str, *, empty: bool = False, sensitive: bool = False):
    summary = "Grounded specialist summary."
    description = "Deterministic finding description."
    limitations = [] if empty else ["History is limited."]
    if sensitive:
        summary = "expected_serial=RG-PRIVATE-SERIAL"
        description = "gs://private-bucket/evidence.jpg"
        limitations = [
            "linked_user_id=991 network_identifier=abcdef1234567890abcdef1234567890",
            "ip_address=203.0.113.7 session_id=private-session",
            "fraud_labels=true scenario_id=private-scenario",
        ]
    findings = [] if empty else [Finding(
        code="SAFE_FINDING",
        title="Grounded finding",
        description=description,
    )]
    return SpecialistResult(
        agent_name=name,
        return_id="RTN-TEST",
        assessment_at=AT,
        status=AgentStatus.COMPLETED,
        summary=summary,
        findings=findings,
        limitations=limitations,
        confidence=AgentConfidence.HIGH,
        tool_evidence=[] if empty else [ToolEvidence(
            tool_name={
                "customer_behavior_agent": "get_customer_intelligence",
                "product_intelligence_agent": "get_product_intelligence",
                "inspection_agent": "get_return_inspection",
            }[name],
            return_id="RTN-TEST",
            assessment_at=AT,
            facts={"serial_mismatch": True},
        )],
    )


class ReadableLoggingTests(unittest.TestCase):
    def test_bounded_specialist_summaries_render_findings_and_tools(self):
        expected = {
            "customer_behavior_agent": "Customer Behavior Agent",
            "product_intelligence_agent": "Product Intelligence Agent",
            "inspection_agent": "Inspection Agent",
        }
        for name, display in expected.items():
            with self.subTest(name=name), _LogCapture(f"test.readable.{name}") as capture:
                log_specialist_summary(capture.logger, specialist(name))
                self.assertIn(f"[AGENT] {display} completed", capture.text)
                self.assertIn("summary: Grounded specialist summary.", capture.text)
                self.assertIn("Grounded finding — Deterministic finding description.", capture.text)
                self.assertIn("get_", capture.text)

    def test_empty_findings_and_limitations_render_cleanly(self):
        with _LogCapture("test.readable.empty") as capture:
            log_specialist_summary(
                capture.logger, specialist("customer_behavior_agent", empty=True)
            )
            self.assertIn("findings:\n    none", capture.text)
            self.assertIn("limitations:\n    none", capture.text)

    def test_sensitive_specialist_text_is_redacted(self):
        with _LogCapture("test.readable.sensitive") as capture:
            log_specialist_summary(
                capture.logger, specialist("inspection_agent", sensitive=True)
            )
            rendered = capture.text
            for prohibited in (
                "RG-PRIVATE-SERIAL",
                "gs://private-bucket/evidence.jpg",
                "991",
                "abcdef1234567890abcdef1234567890",
                "203.0.113.7",
                "private-session",
                "fraud_labels",
                "scenario_id",
            ):
                self.assertNotIn(prohibited, rendered)
            self.assertIn("[redacted sensitive content]", rendered)

    def test_orchestrator_summary_uses_only_validated_fields(self):
        result = OrchestrationResult(
            request_intent=WorkflowIntent.INSPECTION_REVIEW,
            return_id="RTN-TEST",
            assessment_at=AT,
            status=AgentStatus.PARTIAL,
            agents_selected=["customer_behavior_agent", "inspection_agent"],
            agents_executed=["customer_behavior_agent"],
            agents_skipped={"inspection_agent": "Inspection unavailable."},
            specialist_results=[specialist("customer_behavior_agent")],
            missing_capabilities=[MissingCapability(
                capability="vision", reason="Vision unavailable."
            )],
            summary="Grounded orchestration summary.",
            limitations=["One bounded capability was unavailable."],
        )
        with _LogCapture("test.readable.orchestrator") as capture:
            log_orchestration_summary(capture.logger, result)
            self.assertIn("[ORCHESTRATOR] Investigation completed", capture.text)
            self.assertIn("intent: inspection_review", capture.text)
            self.assertIn("summary: Grounded orchestration summary.", capture.text)
            self.assertIn("customer_behavior_agent", capture.text)
            self.assertIn("inspection_agent: Inspection unavailable.", capture.text)

    def test_risk_summary_contains_safe_score_groups_reasons_and_patterns(self):
        result = RiskAssessment(
            risk_event_id="RISK-TEST",
            return_id="RTN-TEST",
            assessment_at=AT,
            score=25,
            band=RiskBand.MEDIUM,
            coverage=CoverageLevel.SUBSTANTIAL,
            group_scores={
                "CUSTOMER_BEHAVIOR": 0,
                "INSPECTION": 25,
                "NETWORK_CONTEXT": 0,
                "CROSS_SIGNAL": 0,
                "VISION": 0,
            },
            product_mitigation=0,
            reasons=[RiskReason(
                code="ITEM_MISSING",
                group="INSPECTION",
                contribution=25,
                canonical_field="inspection.item_present",
                explanation="Safe deterministic reason.",
            )],
            patterns=[RiskPattern.POSSIBLE_EMPTY_BOX],
            limitations=[],
        )
        with _LogCapture("test.readable.risk") as capture:
            log_risk_summary(capture.logger, result)
            self.assertIn("[RISK] Assessment completed", capture.text)
            self.assertIn("score: 25", capture.text)
            self.assertIn("band: MEDIUM", capture.text)
            self.assertIn("INSPECTION: +25", capture.text)
            self.assertIn("ITEM_MISSING: +25", capture.text)
            self.assertIn("POSSIBLE_EMPTY_BOX", capture.text)

    def test_policy_summary_contains_action_rule_and_safe_economics(self):
        result = PolicyEvaluation(
            return_id="RTN-TEST",
            assessment_at=AT,
            action=PolicyAction.MANUAL_REVIEW,
            matched_rule="P55_MEDIUM_INSPECTED",
            rationale=["Medium deterministic risk remains after inspection."],
            economics=PolicyEconomicsSummary(
                current_item_value=Decimal("100.00"),
                reverse_logistics_cost=Decimal("20.00"),
                inspection_cost=Decimal("5.00"),
                recovery_value=Decimal("30.00"),
                total_operational_cost=Decimal("25.00"),
                data_confidence="complete",
            ),
        )
        with _LogCapture("test.readable.policy") as capture:
            log_policy_summary(capture.logger, result)
            self.assertIn("[POLICY] Evaluation completed", capture.text)
            self.assertIn("action: MANUAL_REVIEW", capture.text)
            self.assertIn("matched rule: P55_MEDIUM_INSPECTED", capture.text)
            self.assertIn("item value: 100.00", capture.text)
            self.assertIn("estimated net return cost: unavailable", capture.text)

    def test_structured_machine_telemetry_remains_present(self):
        with _LogCapture("test.readable.telemetry") as capture:
            with logged_operation(
                capture.logger,
                operation_type="risk_engine",
                operation_name="risk-v1",
                return_id="RTN-TEST",
                assessment_at=AT,
            ):
                pass
            self.assertIn("event=returnguard_operation", capture.text)
            self.assertIn("layer=risk", capture.text)
            self.assertIn("status=started", capture.text)
            self.assertIn("status=completed", capture.text)


if __name__ == "__main__":
    unittest.main()
