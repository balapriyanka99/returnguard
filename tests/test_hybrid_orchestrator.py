from __future__ import annotations

import unittest
from collections import Counter
from datetime import datetime, timezone

from returnguard.agents import (
    AgentConfidence,
    AgentConfig,
    AgentExecutionContext,
    AgentStatus,
    HybridOrchestrator,
    MCPToolBridge,
    OrchestrationSynthesis,
    SpecialistResult,
    ToolEvidence,
    WorkflowIntent,
    plan_orchestration,
)
from returnguard.mcp import MCPToolResult, create_mcp_server


AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)
CONTEXT = AgentExecutionContext(
    return_id="RTN-M08-002",
    assessment_at=AT,
    trace_id="hybrid-trace",
    assessment_id="hybrid-assessment",
    request_id="hybrid-request",
)
TOOLS_BY_AGENT = {
    "customer_behavior_agent": {
        "get_customer_intelligence", "get_return_behavior",
    },
    "product_intelligence_agent": {"get_product_intelligence"},
    "inspection_agent": {"get_return_inspection"},
}


class ProtocolBackend:
    def __getattr__(self, name):
        if name.startswith("get_") or name == "calculate_return_economics":
            return lambda request: MCPToolResult(
                tool_name=name,
                return_id=request.return_id,
                assessment_at=request.assessment_at,
                trace_id=request.context.trace_id,
                assessment_id=request.context.assessment_id,
                result={"data_origin": "controlled_demo"},
            )
        raise AttributeError(name)


class FakeRuntime:
    """ADK runtime seam: records real agent construction without external calls."""

    def __init__(
        self,
        failures: set[str] | None = None,
        failed_results: set[str] | None = None,
    ):
        self.failures = failures or set()
        self.failed_results = failed_results or set()
        self.calls: list[str] = []

    async def run(
        self, agent, prompt, context, output_type, *, event_observer=None
    ):
        del prompt, event_observer
        self.calls.append(agent.name)
        if agent.name in self.failures:
            raise RuntimeError("bounded specialist failed")
        if output_type is OrchestrationSynthesis:
            return OrchestrationSynthesis(
                summary="Grounded specialists completed.", limitations=[]
            )
        tool_names = {tool.name for tool in agent.tools}
        self_test = TOOLS_BY_AGENT[agent.name]
        if tool_names != self_test:
            raise AssertionError(f"Unexpected tool boundary for {agent.name}")
        tool_name = sorted(tool_names)[0]
        return SpecialistResult(
            agent_name=agent.name,
            return_id=context.return_id,
            assessment_at=context.assessment_at,
            status=(
                AgentStatus.FAILED
                if agent.name in self.failed_results
                else AgentStatus.COMPLETED
            ),
            summary="Grounded specialist result.",
            confidence=AgentConfidence.HIGH,
            tool_evidence=[ToolEvidence(
                tool_name=tool_name,
                return_id=context.return_id,
                assessment_at=context.assessment_at,
                facts={"data_origin": "controlled_demo"},
                data_origin="controlled_demo",
            )],
            trace_id=context.trace_id,
            assessment_id=context.assessment_id,
        )


def coordinator(runtime: FakeRuntime) -> HybridOrchestrator:
    bridge = MCPToolBridge(create_mcp_server(ProtocolBackend()))
    return HybridOrchestrator(
        bridge,
        AgentConfig(),
        runtime_factory=lambda: runtime,
    )


class HybridOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def test_planning_rules_are_deterministic_and_explicit(self):
        general = plan_orchestration(
            WorkflowIntent.GENERAL_INVESTIGATION, CONTEXT
        )
        self.assertEqual(general.agents_selected, [
            "customer_behavior_agent", "product_intelligence_agent",
        ])

        available = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW,
            CONTEXT,
            inspection_available=True,
        )
        self.assertEqual(available.agents_selected, [
            "customer_behavior_agent", "product_intelligence_agent",
            "inspection_agent",
        ])

        unavailable = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW,
            CONTEXT,
            inspection_available=False,
        )
        self.assertNotIn("inspection_agent", unavailable.agents_selected)
        self.assertEqual(
            unavailable.agents_skipped["inspection_agent"],
            "No inspection is currently available",
        )
        self.assertEqual(
            unavailable.next_required_action,
            "Wait for or request warehouse inspection",
        )

        vision = plan_orchestration(WorkflowIntent.VISION_REVIEW, CONTEXT)
        self.assertEqual(
            [item.capability for item in vision.missing_capabilities], ["vision"]
        )
        self.assertNotIn("vision_agent", vision.agents_selected)

        risk = plan_orchestration(
            WorkflowIntent.RISK_POLICY_DECISION, CONTEXT
        )
        self.assertEqual(
            {item.capability for item in risk.missing_capabilities},
            {"deterministic_risk", "decision_policy"},
        )
        self.assertNotIn("decision_policy_agent", risk.agents_selected)

    async def test_each_planned_specialist_runs_once_then_gemini_synthesis(self):
        plan = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW,
            CONTEXT,
            inspection_available=True,
        )
        runtime = FakeRuntime()
        result = await coordinator(runtime).execute(plan, CONTEXT)

        self.assertEqual(runtime.calls, [*plan.agents_selected, "returnguard_orchestrator_agent"])
        self.assertTrue(all(count == 1 for count in Counter(runtime.calls).values()))
        self.assertEqual(result.agents_selected, plan.agents_selected)
        self.assertEqual(result.agents_executed, plan.agents_selected)
        self.assertEqual(
            [item.agent_name for item in result.specialist_results],
            plan.agents_selected,
        )
        self.assertEqual(result.status, AgentStatus.COMPLETED)

    async def test_failed_specialist_result_is_not_reported_as_executed(self):
        plan = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW,
            CONTEXT,
            inspection_available=True,
        )
        runtime = FakeRuntime(failed_results={"product_intelligence_agent"})
        result = await coordinator(runtime).execute(plan, CONTEXT)

        self.assertEqual(result.status, AgentStatus.PARTIAL)
        self.assertNotIn("product_intelligence_agent", result.agents_executed)
        self.assertNotIn(
            "product_intelligence_agent",
            {item.agent_name for item in result.specialist_results},
        )
        self.assertIn("product_intelligence_agent", result.agents_skipped)
        self.assertEqual(result.request_intent, WorkflowIntent.INSPECTION_REVIEW)
        self.assertEqual(result.return_id, CONTEXT.return_id)
        self.assertEqual(result.assessment_at, CONTEXT.assessment_at)

    async def test_specialist_failure_is_partial_and_never_reported_executed(self):
        plan = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW,
            CONTEXT,
            inspection_available=True,
        )
        runtime = FakeRuntime({"product_intelligence_agent"})
        result = await coordinator(runtime).execute(plan, CONTEXT)

        self.assertEqual(result.status, AgentStatus.PARTIAL)
        self.assertNotIn("product_intelligence_agent", result.agents_executed)
        self.assertIn("product_intelligence_agent", result.agents_skipped)
        self.assertEqual(result.agents_executed, [
            "customer_behavior_agent", "inspection_agent",
        ])
        self.assertEqual(
            [item.agent_name for item in result.specialist_results],
            result.agents_executed,
        )

    async def test_all_specialists_failed_returns_failed_without_fake_synthesis(self):
        plan = plan_orchestration(
            WorkflowIntent.GENERAL_INVESTIGATION, CONTEXT
        )
        runtime = FakeRuntime(set(plan.agents_selected))
        result = await coordinator(runtime).execute(plan, CONTEXT)

        self.assertEqual(result.status, AgentStatus.FAILED)
        self.assertEqual(result.agents_executed, [])
        self.assertEqual(result.specialist_results, [])
        self.assertNotIn("returnguard_orchestrator_agent", runtime.calls)

    async def test_plan_cannot_be_executed_with_different_context(self):
        plan = plan_orchestration(
            WorkflowIntent.GENERAL_INVESTIGATION, CONTEXT
        )
        different = CONTEXT.model_copy(update={"return_id": "RTN-S01-001"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            await coordinator(FakeRuntime()).execute(plan, different)


if __name__ == "__main__":
    unittest.main()
