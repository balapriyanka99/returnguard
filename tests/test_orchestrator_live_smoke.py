from __future__ import annotations

import unittest
import os
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError

from returnguard.agents import (
    AgentConfidence,
    AgentExecutionContext,
    AgentStatus,
    OrchestrationPlan,
    OrchestrationResult,
    SpecialistResult,
    ToolEvidence,
    WorkflowIntent,
    plan_orchestration,
)
from scripts.diagnostics.smoke_test_orchestrator_live import (
    ExecutionDiagnostics,
    bootstrap_vertex_ai,
    parse_args,
    safe_validation_errors,
    validate_plan,
    validate_result,
    workflow_request,
)


AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)
CONTEXT = AgentExecutionContext(
    return_id="RTN-M08-002",
    assessment_at=AT,
    trace_id="trace-test",
    assessment_id="assessment-test",
    request_id="request-test",
)
PLAN = plan_orchestration(
    WorkflowIntent.INSPECTION_REVIEW, CONTEXT, inspection_available=True
)


def specialist(name: str, tool: str) -> SpecialistResult:
    return SpecialistResult(
        agent_name=name,
        return_id=CONTEXT.return_id,
        assessment_at=AT,
        status=AgentStatus.COMPLETED,
        summary="Grounded specialist result.",
        confidence=AgentConfidence.HIGH,
        tool_evidence=[ToolEvidence(
            tool_name=tool,
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            facts={"data_origin": "controlled_demo"},
            data_origin="controlled_demo",
        )],
        trace_id=CONTEXT.trace_id,
        assessment_id=CONTEXT.assessment_id,
    )


class OrchestratorLiveSmokeHelperTests(unittest.TestCase):
    def test_cli_defaults_to_recommended_inspection_case(self):
        args = parse_args([])
        self.assertEqual(args.return_id, "RTN-M08-002")
        self.assertEqual(args.intent, WorkflowIntent.INSPECTION_REVIEW)
        self.assertTrue(args.inspection_available)

    def test_live_smoke_bootstraps_known_vertex_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            args = parse_args([])
            config = bootstrap_vertex_ai(args)
            self.assertTrue(config.use_vertex_ai)
            self.assertEqual(config.google_cloud_project, "return-guard-506407")
            self.assertEqual(config.google_cloud_location, "us-central1")
            self.assertEqual(os.environ["GOOGLE_GENAI_USE_VERTEXAI"], "true")

    def test_inspection_request_requires_real_orchestrator_delegation(self):
        prompt = workflow_request(WorkflowIntent.INSPECTION_REVIEW)
        self.assertIn("customer_behavior_agent", prompt)
        self.assertIn("product_intelligence_agent", prompt)
        self.assertIn("inspection_agent", prompt)
        self.assertIn("bounded MCP-backed tools", prompt)
        self.assertIn("hybrid structured workflow", prompt)

    def test_valid_grounded_inspection_orchestration_passes(self):
        names = [
            "customer_behavior_agent",
            "product_intelligence_agent",
            "inspection_agent",
        ]
        result = OrchestrationResult(
            request_intent=WorkflowIntent.INSPECTION_REVIEW,
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            status=AgentStatus.COMPLETED,
            agents_selected=names,
            agents_executed=names,
            specialist_results=[
                specialist(names[0], "get_customer_intelligence"),
                specialist(names[1], "get_product_intelligence"),
                specialist(names[2], "get_return_inspection"),
            ],
            trace_id=CONTEXT.trace_id,
            assessment_id=CONTEXT.assessment_id,
        )
        self.assertEqual(
            validate_result(result, CONTEXT, PLAN),
            {"structured": "PASS", "grounded": "PASS", "prohibited": "PASS"},
        )

    def test_missing_required_specialist_fails(self):
        result = OrchestrationResult(
            request_intent=WorkflowIntent.INSPECTION_REVIEW,
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            status=AgentStatus.PARTIAL,
            agents_selected=["customer_behavior_agent"],
            agents_executed=["customer_behavior_agent"],
            specialist_results=[specialist(
                "customer_behavior_agent", "get_customer_intelligence"
            )],
        )
        with self.assertRaisesRegex(AssertionError, "agents_selected"):
            validate_result(result, CONTEXT, PLAN)

    def test_prohibited_output_fails(self):
        names = [
            "customer_behavior_agent",
            "product_intelligence_agent",
            "inspection_agent",
        ]
        inspection = specialist("inspection_agent", "get_return_inspection")
        inspection.summary = "Raw expected_serial must not be exposed."
        result = OrchestrationResult(
            request_intent=WorkflowIntent.INSPECTION_REVIEW,
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            status=AgentStatus.COMPLETED,
            agents_selected=names,
            agents_executed=names,
            specialist_results=[
                specialist(names[0], "get_customer_intelligence"),
                specialist(names[1], "get_product_intelligence"),
                inspection,
            ],
        )
        with self.assertRaisesRegex(AssertionError, "Prohibited field"):
            validate_result(result, CONTEXT, PLAN)

    def test_hybrid_plan_validation_uses_lifecycle_availability(self):
        self.assertIsInstance(PLAN, OrchestrationPlan)
        validate_plan(
            PLAN,
            CONTEXT,
            WorkflowIntent.INSPECTION_REVIEW,
            inspection_available=True,
        )
        unavailable = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW,
            CONTEXT,
            inspection_available=False,
        )
        validate_plan(
            unavailable,
            CONTEXT,
            WorkflowIntent.INSPECTION_REVIEW,
            inspection_available=False,
        )
        self.assertIn("inspection_agent", unavailable.agents_skipped)

    def test_diagnostics_capture_safe_transfer_specialist_and_tool_metadata(self):
        diagnostics = ExecutionDiagnostics(
            intent=WorkflowIntent.INSPECTION_REVIEW,
            return_id=CONTEXT.return_id,
            trace_id=CONTEXT.trace_id,
            assessment_id=CONTEXT.assessment_id,
            orchestrator_state="started",
        )
        diagnostics.observe({
            "author": "returnguard_orchestrator_agent",
            "function_calls": [{
                "name": "transfer_to_agent",
                "target_agent": "inspection_agent",
            }],
            "function_responses": [],
            "is_final_response": False,
        })
        diagnostics.observe({
            "author": "inspection_agent",
            "function_calls": [{"name": "get_return_inspection"}],
            "function_responses": ["get_return_inspection"],
            "is_final_response": True,
            "output_contract": "SpecialistResult",
            "tool_evidence_count": 1,
            "findings_count": 2,
            "status": "completed",
            "confidence": "high",
        })
        diagnostics.observe({
            "author": "returnguard_orchestrator_agent",
            "function_calls": [],
            "function_responses": [],
            "is_final_response": True,
        })

        self.assertEqual(diagnostics.transfers, [
            ("returnguard_orchestrator_agent", "inspection_agent")
        ])
        self.assertEqual(diagnostics.agents_executed, ["inspection_agent"])
        self.assertEqual(diagnostics.invoked_tools, ["get_return_inspection"])
        self.assertTrue(diagnostics.control_returned)
        self.assertEqual(diagnostics.specialist_result_count, 1)
        self.assertEqual(
            diagnostics.specialists["inspection_agent"].tool_evidence_count, 1
        )

    def test_validation_diagnostics_exclude_rejected_input_values(self):
        private_value = "DO-NOT-PRINT-THIS-RAW-VALUE"
        try:
            OrchestrationResult.model_validate({
                "request_intent": "inspection_review",
                "return_id": CONTEXT.return_id,
                "assessment_at": AT.isoformat(),
                "status": private_value,
            })
        except ValidationError as exc:
            errors = safe_validation_errors(exc)
        else:  # pragma: no cover - invalid status must remain invalid
            self.fail("Expected OrchestrationResult validation to fail")

        rendered = repr(errors)
        self.assertIn("location", rendered)
        self.assertIn("type", rendered)
        self.assertIn("message", rendered)
        self.assertNotIn(private_value, rendered)
        self.assertNotIn("input_value", rendered)


if __name__ == "__main__":
    unittest.main()
