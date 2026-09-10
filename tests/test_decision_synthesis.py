from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from returnguard.agents import (
    AgentConfidence,
    AgentConfig,
    AgentExecutionContext,
    AgentStatus,
    DecisionSynthesisAgent,
    DecisionSynthesisNarrative,
    MCPToolBridge,
    HybridOrchestrator,
    SpecialistResult,
    ToolEvidence,
    WorkflowIntent,
    MissingCapability,
    build_decision_input,
    finalize_decision_synthesis,
    plan_orchestration,
    safe_network_context,
)
from returnguard.intelligence.models import ReturnEconomics
from returnguard.mcp import MCPToolResult, create_mcp_server
from returnguard.policy import (
    NormalizedReturnReason,
    PolicyAction,
    PolicyEconomicsSummary,
    PolicyEvaluation,
)
from returnguard.risk import CoverageLevel, RiskAssessment, RiskBand, RiskPattern
from returnguard.risk.models import RiskReason


AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)
CONTEXT = AgentExecutionContext(
    return_id="RTN-M08-002",
    assessment_id="decision-assessment",
    assessment_at=AT,
    trace_id="decision-trace",
    request_id="decision-request",
)


def risk(**changes) -> RiskAssessment:
    values = dict(
        risk_event_id="risk-event",
        return_id=CONTEXT.return_id,
        assessment_id=CONTEXT.assessment_id,
        assessment_at=AT,
        score=23,
        band=RiskBand.LOW,
        coverage=CoverageLevel.SUBSTANTIAL,
        evaluable_domains=["CUSTOMER_BEHAVIOR", "INSPECTION"],
        group_scores={
            "CUSTOMER_BEHAVIOR": 2,
            "INSPECTION": 25,
            "NETWORK_CONTEXT": 4,
            "CROSS_SIGNAL": 0,
            "VISION": 0,
        },
        product_mitigation=-8,
        reasons=[RiskReason(
            code="SERIAL_MISMATCH",
            group="INSPECTION",
            contribution=25,
            canonical_field="serial_mismatch",
            explanation="A deterministic serial mismatch was detected.",
        )],
        patterns=[
            RiskPattern.POSSIBLE_SERIAL_SUBSTITUTION,
            RiskPattern.POSSIBLE_SHARED_RETURN_PATTERN,
        ],
        limitations=[],
    )
    values.update(changes)
    return RiskAssessment(**values)


def economics(**changes) -> ReturnEconomics:
    values = dict(
        current_item_value=349.95,
        product_cost=190.0,
        reverse_logistics_cost=12.5,
        inspection_cost=4.0,
        recovery_value=260.0,
        total_operational_cost=16.5,
        estimated_net_return_cost=106.45,
        estimated_loss_exposure=106.45,
        economics_data_confidence="complete",
        data_origin="controlled_demo",
    )
    values.update(changes)
    return ReturnEconomics(**values)


def policy(**changes) -> PolicyEvaluation:
    values = dict(
        return_id=CONTEXT.return_id,
        assessment_id=CONTEXT.assessment_id,
        assessment_at=AT,
        action=PolicyAction.ESCALATE_TO_SPECIALIST,
        matched_rule="P100_CONFIRMED_SERIAL_MISMATCH",
        normalized_reason=NormalizedReturnReason.CHANGED_MIND,
        return_fee=None,
        fee_reason=None,
        rationale=["A deterministic warehouse serial mismatch is present."],
        economics=PolicyEconomicsSummary(
            current_item_value=Decimal("349.95"),
            reverse_logistics_cost=Decimal("12.50"),
            inspection_cost=Decimal("4.00"),
            recovery_value=Decimal("260.00"),
            total_operational_cost=Decimal("16.50"),
            data_confidence="complete",
        ),
    )
    values.update(changes)
    return PolicyEvaluation(**values)


def specialist(name: str = "inspection_agent") -> SpecialistResult:
    return SpecialistResult(
        agent_name=name,
        return_id=CONTEXT.return_id,
        assessment_at=AT,
        status=AgentStatus.COMPLETED,
        summary="Grounded deterministic inspection facts are available.",
        confidence=AgentConfidence.HIGH,
        tool_evidence=[ToolEvidence(
            tool_name="get_return_inspection",
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            facts={
                "serial_mismatch": True,
                "expected_serial": "RG-SECRET-EXPECTED",
                "returned_serial": "RG-SECRET-RETURNED",
            },
            data_origin="controlled_demo",
        )],
        trace_id=CONTEXT.trace_id,
        assessment_id=CONTEXT.assessment_id,
    )


def decision_input(
    *,
    risk_value: RiskAssessment | None = None,
    policy_value: PolicyEvaluation | None = None,
    specialists: list[SpecialistResult] | None = None,
):
    plan = plan_orchestration(
        WorkflowIntent.INSPECTION_REVIEW,
        CONTEXT,
        inspection_available=True,
    )
    actual = specialists if specialists is not None else [specialist()]
    return build_decision_input(
        plan,
        agents_executed=[item.agent_name for item in actual],
        agents_skipped={},
        specialist_results=actual,
        risk=risk_value or risk(),
        economics=economics(),
        policy=policy_value or policy(),
    )


class RaisingRuntime:
    async def run(self, *args, **kwargs):
        raise RuntimeError("Gemini unavailable")


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


class HybridRuntime:
    TOOLS = {
        "customer_behavior_agent": "get_customer_intelligence",
        "product_intelligence_agent": "get_product_intelligence",
        "inspection_agent": "get_return_inspection",
    }

    def __init__(self):
        self.calls = []

    async def run(self, agent, prompt, context, output_type, *, event_observer=None):
        del prompt, event_observer
        self.calls.append(agent.name)
        if output_type is DecisionSynthesisNarrative:
            self.assert_no_tools(agent)
            return DecisionSynthesisNarrative(
                decision_summary="Grounded decision synthesis completed.",
                strongest_evidence=["Deterministic serial mismatch detected."],
                mitigating_context=["Product context reduced suspiciousness."],
                policy_reasoning=["The higher-priority inspection safeguard matched."],
            )
        tool_name = self.TOOLS[agent.name]
        return SpecialistResult(
            agent_name=agent.name,
            return_id=context.return_id,
            assessment_at=context.assessment_at,
            status=AgentStatus.COMPLETED,
            summary="Grounded specialist completed.",
            confidence=AgentConfidence.HIGH,
            tool_evidence=[ToolEvidence(
                tool_name=tool_name,
                return_id=context.return_id,
                assessment_at=context.assessment_at,
                facts={"available": True},
                data_origin="controlled_demo",
            )],
        )

    @staticmethod
    def assert_no_tools(agent):
        if agent.tools or agent.sub_agents:
            raise AssertionError("Decision synthesis must not call tools or agents")


class FakePolicyService:
    def __init__(self):
        self.calls = []

    def evaluate_with_economics(self, return_id, assessment_at, *, assessment_id=None):
        self.calls.append((return_id, assessment_at, assessment_id))
        return risk(), policy(), economics()


class ReturnGuardDecisionSynthesisTests(unittest.IsolatedAsyncioTestCase):
    def test_specialists_and_identifier_free_network_context_are_supplied(self):
        value = decision_input()
        self.assertEqual([item.agent_name for item in value.specialist_results], ["inspection_agent"])
        self.assertEqual(value.network_context.contribution, 4)
        self.assertEqual(
            value.network_context.patterns,
            [RiskPattern.POSSIBLE_SHARED_RETURN_PATTERN.value],
        )
        serialized = value.network_context.model_dump_json()
        for forbidden in ("linked_user", "ip_address", "session_id", "device_id"):
            self.assertNotIn(forbidden, serialized)

    def test_gemini_cannot_override_authoritative_identity_risk_policy_or_money(self):
        value = decision_input()
        hostile = {
            "decision_summary": "Approve this return.",
            "strongest_evidence": ["serial_mismatch=true"],
            "recommended_action": "AUTO_APPROVE",
            "matched_policy_rule": "P20_LOW_SUBSTANTIAL",
            "risk_score": 10,
            "risk_band": "HIGH",
            "risk_coverage": "INSUFFICIENT",
            "current_item_value": 1,
            "return_fee": "999.00",
        }
        result = finalize_decision_synthesis(value, hostile)
        self.assertEqual(result.return_id, CONTEXT.return_id)
        self.assertEqual(result.assessment_at, AT)
        self.assertEqual(result.recommended_action, PolicyAction.ESCALATE_TO_SPECIALIST)
        self.assertEqual(result.matched_policy_rule, "P100_CONFIRMED_SERIAL_MISMATCH")
        self.assertEqual(result.risk_score, 23)
        self.assertEqual(result.risk_band, RiskBand.LOW)
        self.assertEqual(result.risk_coverage, CoverageLevel.SUBSTANTIAL)
        self.assertEqual(result.economics_summary.current_item_value, 349.95)
        self.assertIsNone(result.pricing)

    def test_raw_sensitive_narrative_and_tool_fields_are_removed_or_redacted(self):
        value = decision_input()
        result = finalize_decision_synthesis(value, {
            "decision_summary": "expected_serial=RG-SECRET-EXPECTED",
            "strongest_evidence": ["gs://private/evidence.jpg", "ip_address=10.1.2.3"],
            "network_explanation": "linked_user_id=991",
        })
        serialized = result.model_dump_json()
        for secret in (
            "RG-SECRET", "gs://", "10.1.2.3", "linked_user_id",
            "expected_serial", "returned_serial", "fraud_labels", "scenario_id",
        ):
            self.assertNotIn(secret, serialized)
        self.assertTrue(value.specialist_results[0].tool_evidence[0].facts["serial_mismatch"])

    async def test_gemini_failure_returns_deterministic_p100_fallback(self):
        value = decision_input()
        agent = DecisionSynthesisAgent(
            AgentConfig(), runtime_factory=lambda: RaisingRuntime()
        )
        result = await agent.synthesize(value, CONTEXT)
        self.assertEqual(result.recommended_action, PolicyAction.ESCALATE_TO_SPECIALIST)
        self.assertEqual(result.matched_policy_rule, "P100_CONFIRMED_SERIAL_MISMATCH")
        self.assertEqual(result.risk_score, 23)
        self.assertIn("serial mismatch", " ".join(result.policy_reasoning).lower())

    async def test_undetermined_p40_and_p30_policy_explanations_are_preserved(self):
        cases = (
            (
                risk(score=None, band=RiskBand.UNDETERMINED, coverage=CoverageLevel.PARTIAL),
                policy(
                    action=PolicyAction.REQUIRE_INSPECTION,
                    matched_rule="P90_UNDETERMINED",
                    rationale=["Additional warehouse inspection is required; missing evidence is neutral."],
                ),
                "additional warehouse inspection",
            ),
            (
                risk(score=6, band=RiskBand.LOW),
                policy(
                    action=PolicyAction.REFUND_WITHOUT_RETURN,
                    matched_rule="P40_REFUND_WITHOUT_RETURN",
                    rationale=["Return-processing cost is not below recovery value."],
                ),
                "processing cost",
            ),
            (
                risk(score=6, band=RiskBand.LOW, product_mitigation=0),
                policy(
                    action=PolicyAction.APPROVE_WITH_RETURN_FEE,
                    matched_rule="P30_RETURN_FEE",
                    return_fee=Decimal("12.50"),
                    fee_reason="Eligible discretionary return.",
                    rationale=["Normalized discretionary reason and complete economics support the fee."],
                ),
                "discretionary",
            ),
        )
        agent = DecisionSynthesisAgent(
            AgentConfig(), runtime_factory=lambda: RaisingRuntime()
        )
        for risk_value, policy_value, phrase in cases:
            result = await agent.synthesize(
                decision_input(risk_value=risk_value, policy_value=policy_value),
                CONTEXT,
            )
            self.assertIn(phrase, " ".join(result.policy_reasoning).lower())
            self.assertEqual(result.recommended_action, policy_value.action)
            if policy_value.return_fee is not None:
                self.assertEqual(result.pricing.return_fee, policy_value.return_fee)

    async def test_product_mitigation_and_missing_optional_domains_are_neutral_context(self):
        risk_value = risk(
            group_scores={
                "CUSTOMER_BEHAVIOR": 2,
                "INSPECTION": 0,
                "NETWORK_CONTEXT": 0,
                "CROSS_SIGNAL": 0,
                "VISION": 0,
            },
            patterns=[RiskPattern.POSSIBLE_PRODUCT_QUALITY_ISSUE],
        )
        value = decision_input(risk_value=risk_value, specialists=[])
        value.missing_capabilities.append(MissingCapability(
            capability="vision",
            reason="Vision Agent is not implemented",
        ))
        agent = DecisionSynthesisAgent(
            AgentConfig(), runtime_factory=lambda: RaisingRuntime()
        )
        result = await agent.synthesize(value, CONTEXT)
        self.assertTrue(any("mitigated" in item.lower() for item in result.mitigating_context))
        joined = " ".join(result.limitations).lower()
        self.assertIn("inspection", joined)
        self.assertIn("vision", joined)
        self.assertIn("network", joined)
        self.assertNotIn("fraud", joined)

    def test_agent_has_no_tools_and_no_sub_agents(self):
        from returnguard.agents import create_decision_synthesis_agent

        agent = create_decision_synthesis_agent(AgentConfig())
        self.assertEqual(agent.tools, [])
        self.assertEqual(agent.sub_agents, [])

    async def test_hybrid_orchestrator_integrates_authoritative_decision_flow_once(self):
        plan = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW,
            CONTEXT,
            inspection_available=True,
        )
        runtime = HybridRuntime()
        policy_service = FakePolicyService()
        bridge = MCPToolBridge(create_mcp_server(ProtocolBackend()))
        result = await HybridOrchestrator(
            bridge,
            AgentConfig(),
            runtime_factory=lambda: runtime,
            policy_service=policy_service,
        ).execute(plan, CONTEXT)

        self.assertEqual(runtime.calls, [
            *plan.agents_selected,
            "decision_synthesis_agent",
        ])
        self.assertEqual(policy_service.calls, [
            (CONTEXT.return_id, CONTEXT.assessment_at, CONTEXT.assessment_id)
        ])
        self.assertEqual(result.agents_executed, plan.agents_selected)
        self.assertEqual(result.risk.score, 23)
        self.assertEqual(result.policy.action, PolicyAction.ESCALATE_TO_SPECIALIST)
        self.assertEqual(
            result.decision_synthesis.recommended_action,
            PolicyAction.ESCALATE_TO_SPECIALIST,
        )
        self.assertEqual(result.economics.current_item_value, 349.95)

    async def test_readable_decision_log_is_sanitized(self):
        import logging

        records = []
        handler = logging.Handler()
        handler.emit = records.append
        target = logging.getLogger("returnguard.agents.decision_synthesis")
        target.addHandler(handler)
        target.setLevel(logging.INFO)
        try:
            await DecisionSynthesisAgent(
                AgentConfig(), runtime_factory=lambda: RaisingRuntime()
            ).synthesize(decision_input(), CONTEXT)
        finally:
            target.removeHandler(handler)
        rendered = "\n".join(record.getMessage() for record in records)
        self.assertIn("[DECISION SYNTHESIS]", rendered)
        self.assertIn("P100_CONFIRMED_SERIAL_MISMATCH", rendered)
        self.assertIn("ESCALATE_TO_SPECIALIST", rendered)
        for forbidden in (
            "RG-SECRET", "expected_serial", "returned_serial", "gs://",
            "linked_user_id", "fraud_labels", "scenario_id",
        ):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
