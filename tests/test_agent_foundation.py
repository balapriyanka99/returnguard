from __future__ import annotations

import unittest
import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

from mcp import Client
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from pydantic import PrivateAttr, ValidationError

from returnguard.agents import (
    AgentConfidence,
    AgentConfig,
    AgentExecutionContext,
    AgentStatus,
    ADKAgentRuntime,
    CopilotAnswer,
    MCPToolBridge,
    OrchestrationPlan,
    OrchestrationSynthesis,
    SpecialistResult,
    ToolEvidence,
    WorkflowIntent,
    create_customer_behavior_agent,
    create_inspection_agent,
    create_investigation_copilot,
    create_orchestrator_agent,
    create_product_intelligence_agent,
    plan_orchestration,
    unavailable_decision_policy_result,
    unavailable_vision_result,
)
from returnguard.mcp import MCPToolResult, create_mcp_server


AT = datetime(2026, 9, 2, tzinfo=timezone.utc)
CONTEXT = AgentExecutionContext(
    return_id="RTN-M08-001", assessment_at=AT,
    trace_id="trace-agent", assessment_id="assessment-agent",
    request_id="request-agent",
)


class FakeProtocolBackend:
    def __init__(self):
        self.calls = []

    def _call(self, name, request):
        self.calls.append((name, request))
        return MCPToolResult(
            tool_name=name, return_id=request.return_id,
            assessment_at=request.assessment_at, trace_id=request.context.trace_id,
            assessment_id=request.context.assessment_id,
            result={"data_origin": "controlled_demo", "lifetime_return_rate": None},
        )

    def __getattr__(self, name):
        if name.startswith("get_") or name == "calculate_return_economics":
            return lambda request: self._call(name, request)
        raise AttributeError(name)


class FakeSelectingLlm(BaseLlm):
    """Deterministic ADK model double that selects exactly one tool per question."""

    selected: str
    _responded: bool = PrivateAttr(default=False)

    async def generate_content_async(self, llm_request, stream=False):
        has_tool_response = any(
            part.function_response is not None
            for content in llm_request.contents
            for part in (content.parts or [])
        )
        if not has_tool_response:
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(function_call=types.FunctionCall(
                    id="fake-call", name=self.selected, args={},
                ))],
            ))
            return
        payload = {
            "return_id": CONTEXT.return_id,
            "assessment_at": AT.isoformat(),
            "status": "completed",
            "answer": "Grounded answer from the selected tool.",
            "tools_used": [self.selected],
            "key_evidence": [{
                "tool_name": self.selected, "return_id": CONTEXT.return_id,
                "assessment_at": AT.isoformat(),
                "facts": {"data_origin": "controlled_demo"},
                "data_origin": "controlled_demo",
            }],
            "limitations": [],
            "unavailable_capabilities": [],
            "trace_id": CONTEXT.trace_id,
            "assessment_id": CONTEXT.assessment_id,
        }
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=json.dumps(payload))],
        ))


class FakeSpecialistLlm(BaseLlm):
    selected: str
    payload: dict

    async def generate_content_async(self, llm_request, stream=False):
        has_tool_response = any(
            part.function_response is not None
            for content in llm_request.contents
            for part in (content.parts or [])
        )
        if not has_tool_response:
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(function_call=types.FunctionCall(
                    id="specialist-call", name=self.selected, args={},
                ))],
            ))
            return
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=json.dumps(self.payload))],
        ))


def tool_names(agent):
    return {tool.name for tool in agent.tools}


class AgentFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_config_defaults_and_vertex_validation(self):
        previous = {key: os.environ.get(key) for key in (
            "RETURNGUARD_GEMINI_MODEL", "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
        )}
        try:
            for key in previous:
                os.environ.pop(key, None)
            self.assertEqual(AgentConfig.from_env().model, "gemini-2.5-flash")
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            with self.assertRaisesRegex(ValueError, "GOOGLE_CLOUD_PROJECT"):
                AgentConfig.from_env().validate_for_live_model()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def test_bridge_uses_real_mcp_client_protocol(self):
        backend = FakeProtocolBackend()
        server = create_mcp_server(backend)
        bridge = MCPToolBridge(server, client_factory=Client)
        result = await bridge.call("get_customer_intelligence", CONTEXT)
        self.assertEqual(result["return_id"], CONTEXT.return_id)
        self.assertEqual(result["assessment_at"], AT.isoformat())
        self.assertEqual(len(backend.calls), 1)
        _, request = backend.calls[0]
        self.assertEqual(request.context.trace_id, "trace-agent")
        self.assertEqual(request.context.assessment_id, "assessment-agent")

    async def test_bounded_specialists_use_only_allowed_mcp_tools(self):
        bridge = MCPToolBridge(create_mcp_server(FakeProtocolBackend()))
        config = AgentConfig()
        customer = create_customer_behavior_agent(bridge, CONTEXT, config)
        product = create_product_intelligence_agent(bridge, CONTEXT, config)
        inspection = create_inspection_agent(bridge, CONTEXT, config)
        self.assertEqual(tool_names(customer), {
            "get_customer_intelligence", "get_return_behavior",
        })
        self.assertEqual(tool_names(product), {"get_product_intelligence"})
        self.assertEqual(tool_names(inspection), {"get_return_inspection"})

    async def test_structured_contract_preserves_missing_and_controlled_null(self):
        result = SpecialistResult(
            agent_name="customer_behavior_agent", return_id=CONTEXT.return_id,
            assessment_at=AT, status=AgentStatus.INSUFFICIENT_EVIDENCE,
            summary="History is insufficient.",
            findings=[], limitations=["The controlled count has no deterministic rate."],
            confidence=AgentConfidence.LOW,
            tool_evidence=[ToolEvidence(
                tool_name="get_customer_intelligence", return_id=CONTEXT.return_id,
                assessment_at=AT,
                facts={"lifetime_historical_returns": 4, "lifetime_return_rate": None},
                data_origin="controlled_demo",
            )],
        )
        self.assertIsNone(result.tool_evidence[0].facts["lifetime_return_rate"])
        self.assertNotIn("risk_score", result.model_dump())
        sanitized = ToolEvidence(
            tool_name="get_return_inspection", return_id=CONTEXT.return_id,
            facts={"expected_serial": "private", "serial_mismatch": True},
        )
        self.assertEqual(sanitized.facts, {"serial_mismatch": True})

    async def test_tool_evidence_strips_raw_serials_and_preserves_comparison(self):
        evidence = ToolEvidence(
            tool_name="get_return_inspection",
            return_id=CONTEXT.return_id,
            facts={
                "condition": "salable",
                "expected_serial": "EXPECTED-PRIVATE",
                "returned_serial": "RETURNED-PRIVATE",
                "serial_mismatch": True,
                "serial_comparison_performed": True,
            },
        )
        self.assertEqual(evidence.facts, {
            "condition": "salable",
            "serial_mismatch": True,
            "serial_comparison_performed": True,
        })

    async def test_tool_evidence_strips_uri_and_preserves_availability(self):
        evidence = ToolEvidence(
            tool_name="get_return_evidence",
            return_id=CONTEXT.return_id,
            facts={
                "visual_evidence_available": True,
                "image_uri": "gs://private/evidence",
                "stage": "WAREHOUSE",
            },
        )
        self.assertEqual(evidence.facts, {
            "visual_evidence_available": True,
            "stage": "WAREHOUSE",
        })

    async def test_tool_evidence_deeply_strips_prohibited_network_and_label_fields(self):
        evidence = ToolEvidence(
            tool_name="get_network_intelligence",
            return_id=CONTEXT.return_id,
            facts={
                "relationship_count": 2,
                "nested": [{
                    "ip_address": "192.0.2.1",
                    "session_id": "private-session",
                    "network_identifiers": ["private-hash"],
                    "linked_user_ids": [123],
                    "fraud_labels": ["evaluation-only"],
                    "data_origin": "controlled_demo",
                }],
            },
        )
        self.assertEqual(evidence.facts, {
            "relationship_count": 2,
            "nested": [{"data_origin": "controlled_demo"}],
        })

    async def test_sanitized_evidence_validates_in_specialist_and_copilot_contracts(self):
        raw = ToolEvidence(
            tool_name="get_return_inspection",
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            facts={"expected_serial": "private", "serial_mismatch": True},
        )
        specialist = SpecialistResult(
            agent_name="inspection_agent",
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            status=AgentStatus.COMPLETED,
            summary="A deterministic serial mismatch was reported.",
            findings=[],
            confidence=AgentConfidence.HIGH,
            tool_evidence=[raw],
        )
        copilot = CopilotAnswer(
            return_id=CONTEXT.return_id,
            assessment_at=AT,
            status=AgentStatus.COMPLETED,
            answer="Review the deterministic inspection mismatch.",
            tools_used=["get_return_inspection"],
            key_evidence=[raw],
        )
        self.assertEqual(specialist.tool_evidence[0].facts, {"serial_mismatch": True})
        self.assertEqual(copilot.key_evidence[0].facts, {"serial_mismatch": True})

    async def test_orchestrator_routes_supported_and_unavailable_intents(self):
        general = plan_orchestration(WorkflowIntent.GENERAL_INVESTIGATION, CONTEXT)
        self.assertIsInstance(general, OrchestrationPlan)
        self.assertEqual(general.agents_selected, [
            "customer_behavior_agent", "product_intelligence_agent",
        ])
        inspection = plan_orchestration(
            WorkflowIntent.INSPECTION_REVIEW, CONTEXT, inspection_available=True
        )
        self.assertIn("inspection_agent", inspection.agents_selected)
        vision = plan_orchestration(WorkflowIntent.VISION_REVIEW, CONTEXT)
        self.assertEqual(vision.missing_capabilities[0].capability, "vision")
        risk = plan_orchestration(WorkflowIntent.RISK_POLICY_DECISION, CONTEXT)
        self.assertEqual(
            {item.capability for item in risk.missing_capabilities},
            {"deterministic_risk", "decision_policy"},
        )

    async def test_real_adk_orchestrator_and_dynamic_copilot_are_constructed(self):
        backend = FakeProtocolBackend()
        bridge = MCPToolBridge(create_mcp_server(backend))
        orchestrator = create_orchestrator_agent(bridge, CONTEXT)
        self.assertEqual(orchestrator.sub_agents, [])
        self.assertIs(orchestrator.output_schema, OrchestrationSynthesis)
        copilot = create_investigation_copilot(bridge, CONTEXT)
        self.assertEqual(len(tool_names(copilot)), 7)
        self.assertIn("smallest relevant subset", copilot.instruction)
        self.assertEqual(backend.calls, [])

    async def test_copilot_model_selects_relevant_subset_through_mcp(self):
        cases = (
            ("Is this customer's behavior unusual?", "get_customer_intelligence"),
            ("Are there connected accounts?", "get_network_intelligence"),
            ("Is processing the return economical?", "calculate_return_economics"),
            ("What did warehouse inspection find?", "get_return_inspection"),
        )
        for index, (question, selected) in enumerate(cases):
            backend = FakeProtocolBackend()
            bridge = MCPToolBridge(create_mcp_server(backend))
            context = CONTEXT.model_copy(update={"request_id": f"copilot-{index}"})
            fake_model = FakeSelectingLlm(model="fake-selector", selected=selected)
            copilot = create_investigation_copilot(bridge, context, model=fake_model)
            result = await ADKAgentRuntime(AgentConfig()).run(
                copilot, question, context, CopilotAnswer
            )
            self.assertEqual(result.tools_used, [selected])
            self.assertEqual([name for name, _ in backend.calls], [selected])

    async def test_specialists_validate_grounded_missing_and_serial_results(self):
        base = {
            "return_id": CONTEXT.return_id, "assessment_at": AT.isoformat(),
            "findings": [], "supporting_evidence": [], "counter_evidence": [],
            "limitations": [], "tool_evidence": [], "trace_id": CONTEXT.trace_id,
            "assessment_id": CONTEXT.assessment_id,
        }
        cases = (
            (
                create_customer_behavior_agent, "get_customer_intelligence",
                {**base, "agent_name": "customer_behavior_agent",
                 "status": "insufficient_evidence", "summary": "Rate unavailable.",
                 "confidence": "low", "limitations": ["Controlled count has no rate."]},
            ),
            (
                create_product_intelligence_agent, "get_product_intelligence",
                {**base, "agent_name": "product_intelligence_agent",
                 "status": "insufficient_evidence", "summary": "Product rate unavailable.",
                 "confidence": "low", "limitations": ["No eligible denominator."]},
            ),
            (
                create_inspection_agent, "get_return_inspection",
                {**base, "agent_name": "inspection_agent", "status": "completed",
                 "summary": "The deterministic serial mismatch flag is true.",
                 "confidence": "high", "supporting_evidence": ["serial_mismatch=true"],
                 "tool_evidence": [{
                     "tool_name": "get_return_inspection", "return_id": CONTEXT.return_id,
                     "facts": {"serial_mismatch": True}, "data_origin": "controlled_demo",
                 }]},
            ),
        )
        for index, (factory, tool, payload) in enumerate(cases):
            backend = FakeProtocolBackend()
            bridge = MCPToolBridge(create_mcp_server(backend))
            context = CONTEXT.model_copy(update={"request_id": f"specialist-{index}"})
            agent = factory(
                bridge, context,
                model=FakeSpecialistLlm(model="fake-specialist", selected=tool, payload=payload),
            )
            result = await ADKAgentRuntime(AgentConfig()).run(
                agent, "Analyze only grounded tool facts.", context, SpecialistResult
            )
            self.assertEqual(result.agent_name, payload["agent_name"])
            self.assertEqual([name for name, _ in backend.calls], [tool])
            self.assertNotIn("risk_score", result.model_dump())

    async def test_agent_logging_excludes_prompt_and_sensitive_values(self):
        sensitive = "RG-SECRET-SERIAL"
        payload = {
            "agent_name": "inspection_agent", "return_id": CONTEXT.return_id,
            "assessment_at": AT.isoformat(), "status": "completed",
            "summary": f"Mismatch confirmed for {sensitive}.", "findings": [],
            "supporting_evidence": [], "counter_evidence": [], "limitations": [],
            "confidence": "high", "tool_evidence": [],
        }
        backend = FakeProtocolBackend()
        bridge = MCPToolBridge(create_mcp_server(backend))
        agent = create_inspection_agent(
            bridge, CONTEXT,
            model=FakeSpecialistLlm(
                model="fake-specialist", selected="get_return_inspection", payload=payload,
            ),
        )
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        agent_logger = logging.getLogger("returnguard.agents.runtime")
        agent_logger.addHandler(handler)
        agent_logger.setLevel(logging.INFO)
        try:
            result = await ADKAgentRuntime(AgentConfig()).run(
                agent, "private prompt content", CONTEXT, SpecialistResult
            )
            self.assertIn(sensitive, result.summary)
        finally:
            agent_logger.removeHandler(handler)
        rendered = " ".join(record.getMessage() for record in records)
        self.assertNotIn(sensitive, rendered)
        self.assertNotIn("private prompt content", rendered)
        self.assertIn("agent=inspection_agent", rendered)
        self.assertIn('action="Interpret deterministic inspection facts"', rendered)
        self.assertIn("return_id=RTN-M08-001", rendered)
        self.assertIn("trace_id=trace-agent", rendered)

    async def test_agent_runtime_logs_safe_gemini_tool_selection(self):
        backend = FakeProtocolBackend()
        bridge = MCPToolBridge(create_mcp_server(backend))
        model = FakeSelectingLlm(
            model="fake-selector", selected="get_return_inspection"
        )
        agent = create_investigation_copilot(bridge, CONTEXT, model=model)
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        agent_logger = logging.getLogger("returnguard.agents.runtime")
        agent_logger.addHandler(handler)
        agent_logger.setLevel(logging.INFO)
        try:
            await ADKAgentRuntime(AgentConfig()).run(
                agent, "Review the inspection.", CONTEXT, CopilotAnswer
            )
        finally:
            agent_logger.removeHandler(handler)
        rendered = " ".join(record.getMessage() for record in records)
        self.assertIn('action="Request grounded tool selection from Gemini"', rendered)
        self.assertIn('action="Gemini selected grounded tools"', rendered)
        self.assertIn("tools_selected=[get_return_inspection]", rendered)
        self.assertIn('action="Synthesize grounded agent response"', rendered)
        self.assertIn("request_id=request-agent", rendered)
        for sensitive in (
            "expected_serial", "returned_serial", "image_uri", "ip_address",
            "session_id", "fraud_labels",
        ):
            self.assertNotIn(sensitive, rendered)

    async def test_agent_runtime_event_observer_exposes_names_and_counts_only(self):
        backend = FakeProtocolBackend()
        bridge = MCPToolBridge(create_mcp_server(backend))
        agent = create_investigation_copilot(
            bridge,
            CONTEXT,
            model=FakeSelectingLlm(
                model="fake-selector", selected="get_return_inspection"
            ),
        )
        events = []
        await ADKAgentRuntime(AgentConfig()).run(
            agent,
            "Review inspection facts.",
            CONTEXT,
            CopilotAnswer,
            event_observer=events.append,
        )
        rendered = repr(events)
        self.assertIn("get_return_inspection", rendered)
        self.assertIn("investigation_copilot", rendered)
        self.assertNotIn("controlled_demo", rendered)
        self.assertNotIn("lifetime_return_rate", rendered)
        self.assertNotIn("Grounded answer", rendered)

    async def test_agent_runtime_logs_safe_structured_validation_failure(self):
        backend = FakeProtocolBackend()
        bridge = MCPToolBridge(create_mcp_server(backend))
        invalid_payload = {
            "return_id": CONTEXT.return_id,
            "assessment_at": AT.isoformat(),
            "status": "not-a-valid-status",
        }
        agent = create_investigation_copilot(
            bridge,
            CONTEXT,
            model=FakeSpecialistLlm(
                model="fake-invalid",
                selected="get_return_inspection",
                payload=invalid_payload,
            ),
        )
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        agent_logger = logging.getLogger("returnguard.agents.runtime")
        agent_logger.addHandler(handler)
        agent_logger.setLevel(logging.INFO)
        try:
            with self.assertRaises(ValidationError):
                await ADKAgentRuntime(AgentConfig()).run(
                    agent, "Private prompt must not be logged.", CONTEXT, CopilotAnswer
                )
        finally:
            agent_logger.removeHandler(handler)
        rendered = " ".join(record.getMessage() for record in records)
        self.assertIn('action="Validate structured agent response"', rendered)
        self.assertIn("status=failed", rendered)
        self.assertIn("validation_error=", rendered)
        self.assertIn("CopilotAnswer", rendered)
        self.assertNotIn("not-a-valid-status", rendered)
        self.assertNotIn("Private prompt must not be logged", rendered)
        self.assertTrue(all(record.exc_info is None for record in records))

    async def test_unavailable_agents_do_not_fabricate_results(self):
        vision = unavailable_vision_result(CONTEXT)
        decision = unavailable_decision_policy_result(CONTEXT)
        self.assertEqual(vision.status, AgentStatus.UNAVAILABLE)
        self.assertEqual(decision.status, AgentStatus.UNAVAILABLE)
        self.assertEqual(vision.findings, [])
        self.assertEqual(decision.findings, [])

    async def test_agent_modules_have_no_direct_data_access_or_scoring(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path("returnguard/agents").glob("*.py")
        )
        self.assertNotIn("BigQueryIntelligenceRepository", source)
        self.assertNotIn("google.cloud.bigquery", source)
        self.assertNotIn(".query(", source)
        self.assertNotIn("risk_score=", source)
        self.assertNotIn("assess_risk", source)


if __name__ == "__main__":
    unittest.main()
