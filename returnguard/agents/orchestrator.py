"""Deterministically planned, ADK-executed ReturnGuard hybrid Orchestrator."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from google.adk.agents import LlmAgent

from returnguard.observability import ExecutionContext, bind_execution_context, log_action

from .config import AgentConfig
from .contracts import (
    AgentExecutionContext,
    AgentStatus,
    MissingCapability,
    OrchestrationPlan,
    OrchestrationResult,
    OrchestrationSynthesis,
    SpecialistResult,
    WorkflowIntent,
)
from .customer_behavior import create_customer_behavior_agent
from .inspection import create_inspection_agent
from .mcp_bridge import MCPToolBridge
from .product_intelligence import create_product_intelligence_agent
from .prompts import ORCHESTRATOR_INSTRUCTION
from .runtime import ADKAgentRuntime, EventObserver


logger = logging.getLogger(__name__)
CUSTOMER_AGENT = "customer_behavior_agent"
PRODUCT_AGENT = "product_intelligence_agent"
INSPECTION_AGENT = "inspection_agent"


def create_orchestrator_agent(
    bridge: MCPToolBridge,
    context: AgentExecutionContext,
    config: AgentConfig | None = None,
    *,
    model=None,
) -> LlmAgent:
    """Create the final Gemini synthesis agent with no transfer-based sub-agents."""

    del bridge, context  # Keep the existing public factory signature compatible.
    config = config or AgentConfig.from_env()
    return LlmAgent(
        name="returnguard_orchestrator_agent",
        description="Synthesizes already-executed grounded ReturnGuard specialist results.",
        model=model or config.model,
        instruction=ORCHESTRATOR_INSTRUCTION,
        output_schema=OrchestrationSynthesis,
    )


def plan_orchestration(
    intent: WorkflowIntent,
    context: AgentExecutionContext,
    *,
    inspection_available: bool = False,
) -> OrchestrationPlan:
    """Create a deterministic plan from intent and capability availability only."""

    selected = [CUSTOMER_AGENT, PRODUCT_AGENT]
    skipped: dict[str, str] = {}
    missing: list[MissingCapability] = []
    next_action = None

    if intent == WorkflowIntent.INSPECTION_REVIEW:
        if inspection_available:
            selected.append(INSPECTION_AGENT)
        else:
            skipped[INSPECTION_AGENT] = "No inspection is currently available"
            next_action = "Wait for or request warehouse inspection"
    elif intent == WorkflowIntent.VISION_REVIEW:
        missing.append(MissingCapability(
            capability="vision",
            reason="Gemini multimodal image analysis / Vision Agent is not implemented yet",
            next_action=(
                "Implement the approved Cloud Storage + Gemini multimodal Vision Agent path"
            ),
        ))
        next_action = missing[0].next_action
    elif intent == WorkflowIntent.RISK_POLICY_DECISION:
        if inspection_available:
            selected.append(INSPECTION_AGENT)
        missing.extend([
            MissingCapability(
                capability="deterministic_risk",
                reason="The deterministic Risk Engine is not implemented",
                next_action="Implement and validate deterministic risk scoring",
            ),
            MissingCapability(
                capability="decision_policy",
                reason="The Decision & Policy Agent / policy workflow is not implemented",
                next_action=(
                    "Implement Decision & Policy only after deterministic risk exists"
                ),
            ),
        ])
        next_action = "Implement deterministic risk, then Decision & Policy"

    return OrchestrationPlan(
        request_intent=intent,
        return_id=context.return_id,
        assessment_at=context.assessment_at,
        agents_selected=selected,
        agents_skipped=skipped,
        missing_capabilities=missing,
        next_required_action=next_action,
        trace_id=context.trace_id,
        assessment_id=context.assessment_id,
    )


class HybridOrchestrator:
    """Execute a fixed plan through bounded ADK specialists, then synthesize."""

    def __init__(
        self,
        bridge: MCPToolBridge,
        config: AgentConfig | None = None,
        *,
        model: Any = None,
        runtime_factory: Callable[[], ADKAgentRuntime] | None = None,
    ) -> None:
        self.bridge = bridge
        self.config = config or AgentConfig.from_env()
        self.model = model
        self.runtime_factory = runtime_factory or (
            lambda: ADKAgentRuntime(self.config)
        )

    def _specialist(self, name: str, context: AgentExecutionContext) -> LlmAgent:
        factories = {
            CUSTOMER_AGENT: create_customer_behavior_agent,
            PRODUCT_AGENT: create_product_intelligence_agent,
            INSPECTION_AGENT: create_inspection_agent,
        }
        try:
            factory = factories[name]
        except KeyError as exc:
            raise ValueError(f"Unsupported specialist in orchestration plan: {name}") from exc
        return factory(self.bridge, context, self.config, model=self.model)

    @staticmethod
    def _specialist_prompt(name: str) -> str:
        return (
            f"Analyze this return as {name} using only your bounded MCP tools. "
            "Return a validated SpecialistResult with compact grounded tool_evidence. "
            "Missing information is neutral and must be reported as insufficient or "
            "unavailable, never as evidence of fraud. Do not use fraud_labels, invent "
            "risk scores, or expose prohibited raw values."
        )

    @staticmethod
    def _synthesis_prompt(
        plan: OrchestrationPlan,
        executed: list[str],
        skipped: dict[str, str],
        results: list[SpecialistResult],
        workflow_request: str | None,
    ) -> str:
        payload = {
            "workflow_request": workflow_request,
            "request_intent": plan.request_intent.value,
            "return_id": plan.return_id,
            "assessment_at": plan.assessment_at.isoformat(),
            "agents_selected": plan.agents_selected,
            "agents_executed": executed,
            "agents_skipped": skipped,
            "missing_capabilities": [
                item.model_dump(mode="json") for item in plan.missing_capabilities
            ],
            "specialist_results": [
                item.model_dump(mode="json") for item in results
            ],
            "next_required_action": plan.next_required_action,
        }
        return (
            "Synthesize a concise grounded workflow summary and limitations from this "
            "typed execution record. Do not restate raw private evidence and do not add "
            "execution metadata.\n" + json.dumps(payload, sort_keys=True)
        )

    async def execute(
        self,
        plan: OrchestrationPlan,
        context: AgentExecutionContext,
        *,
        workflow_request: str | None = None,
        event_observer: EventObserver | None = None,
    ) -> OrchestrationResult:
        if (
            plan.return_id != context.return_id
            or plan.assessment_at != context.assessment_at
            or plan.trace_id != context.trace_id
            or plan.assessment_id != context.assessment_id
        ):
            raise ValueError("Orchestration plan does not match execution context")

        correlation = ExecutionContext(
            context.trace_id, context.assessment_id, context.request_id
        )
        executed: list[str] = []
        results: list[SpecialistResult] = []
        skipped = dict(plan.agents_skipped)
        with bind_execution_context(correlation):
            log_action(
                logger,
                operation_type="agent",
                operation_name="hybrid_orchestrator",
                action=f"Create orchestration plan for {plan.request_intent.value}",
                status="planned",
                return_id=context.return_id,
                assessment_at=context.assessment_at,
                tools_selected=plan.agents_selected,
                tool_count=len(plan.agents_selected),
            )
            for name in plan.agents_selected:
                log_action(
                    logger,
                    operation_type="agent",
                    operation_name=name,
                    action="Execute selected bounded specialist",
                    status="started",
                    return_id=context.return_id,
                    assessment_at=context.assessment_at,
                )
                try:
                    result = await self.runtime_factory().run(
                        self._specialist(name, context),
                        self._specialist_prompt(name),
                        context,
                        SpecialistResult,
                        event_observer=event_observer,
                    )
                except Exception as exc:
                    skipped[name] = f"Specialist execution failed: {type(exc).__name__}"
                    log_action(
                        logger,
                        operation_type="agent",
                        operation_name=name,
                        action="Execute selected bounded specialist",
                        status="failed",
                        return_id=context.return_id,
                        assessment_at=context.assessment_at,
                        level=logging.ERROR,
                    )
                    continue
                result = result.model_copy(update={
                    "agent_name": name,
                    "return_id": context.return_id,
                    "assessment_at": context.assessment_at,
                    "trace_id": context.trace_id,
                    "assessment_id": context.assessment_id,
                })
                if result.status in {AgentStatus.FAILED, AgentStatus.UNAVAILABLE}:
                    skipped[name] = (
                        f"Specialist returned status: {result.status.value}"
                    )
                    log_action(
                        logger,
                        operation_type="agent",
                        operation_name=name,
                        action="Execute selected bounded specialist",
                        status="failed",
                        return_id=context.return_id,
                        assessment_at=context.assessment_at,
                        level=logging.ERROR,
                    )
                    continue
                executed.append(name)
                results.append(result)
                log_action(
                    logger,
                    operation_type="agent",
                    operation_name=name,
                    action="Execute selected bounded specialist",
                    status="completed",
                    return_id=context.return_id,
                    assessment_at=context.assessment_at,
                )

            if not results:
                final = OrchestrationResult(
                    request_intent=plan.request_intent,
                    return_id=plan.return_id,
                    assessment_at=plan.assessment_at,
                    status=AgentStatus.FAILED,
                    agents_selected=plan.agents_selected,
                    agents_executed=[],
                    agents_skipped=skipped,
                    specialist_results=[],
                    missing_capabilities=plan.missing_capabilities,
                    next_required_action=plan.next_required_action,
                    summary="No selected specialist completed successfully.",
                    limitations=[
                        "No grounded SpecialistResult was available for synthesis."
                    ],
                    trace_id=plan.trace_id,
                    assessment_id=plan.assessment_id,
                )
                log_action(
                    logger,
                    operation_type="agent",
                    operation_name="hybrid_orchestrator",
                    action="Complete hybrid orchestration workflow",
                    status=final.status.value,
                    return_id=context.return_id,
                    assessment_at=context.assessment_at,
                )
                return final

            log_action(
                logger,
                operation_type="agent",
                operation_name="hybrid_orchestrator",
                action="Synthesize grounded orchestration result",
                status="started",
                return_id=context.return_id,
                assessment_at=context.assessment_at,
                tool_count=len(results),
            )
            try:
                synthesis = await self.runtime_factory().run(
                    create_orchestrator_agent(
                        self.bridge, context, self.config, model=self.model
                    ),
                    self._synthesis_prompt(
                        plan, executed, skipped, results, workflow_request
                    ),
                    context,
                    OrchestrationSynthesis,
                    event_observer=event_observer,
                )
            except Exception as exc:
                skipped["final_synthesis"] = (
                    f"Orchestrator synthesis failed: {type(exc).__name__}"
                )
                log_action(
                    logger,
                    operation_type="agent",
                    operation_name="hybrid_orchestrator",
                    action="Synthesize grounded orchestration result",
                    status="failed",
                    return_id=context.return_id,
                    assessment_at=context.assessment_at,
                    level=logging.ERROR,
                )
                synthesis = OrchestrationSynthesis(
                    summary="Grounded specialist results are available, but final synthesis failed.",
                    limitations=["Final Gemini orchestration synthesis was unavailable."],
                )
            status = (
                AgentStatus.COMPLETED
                if len(executed) == len(plan.agents_selected)
                and not skipped
                and not plan.missing_capabilities
                else AgentStatus.PARTIAL
            )
            final = OrchestrationResult(
                request_intent=plan.request_intent,
                return_id=plan.return_id,
                assessment_at=plan.assessment_at,
                status=status,
                agents_selected=list(plan.agents_selected),
                agents_executed=executed,
                agents_skipped=skipped,
                specialist_results=results,
                missing_capabilities=plan.missing_capabilities,
                next_required_action=plan.next_required_action,
                summary=synthesis.summary,
                limitations=synthesis.limitations,
                trace_id=plan.trace_id,
                assessment_id=plan.assessment_id,
            )
            log_action(
                logger,
                operation_type="agent",
                operation_name="hybrid_orchestrator",
                action="Validate final structured orchestration result",
                status="completed",
                return_id=context.return_id,
                assessment_at=context.assessment_at,
            )
            log_action(
                logger,
                operation_type="agent",
                operation_name="hybrid_orchestrator",
                action="Complete hybrid orchestration workflow",
                status=final.status.value,
                return_id=context.return_id,
                assessment_at=context.assessment_at,
            )
            return final
