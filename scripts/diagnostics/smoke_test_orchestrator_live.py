#!/usr/bin/env python3
"""Credentialed read-only live smoke test for the ReturnGuard ADK Orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from returnguard.agents import (
    AgentConfig,
    AgentExecutionContext,
    AgentStatus,
    HybridOrchestrator,
    MCPToolBridge,
    OrchestrationPlan,
    OrchestrationResult,
    WorkflowIntent,
    plan_orchestration,
)
from returnguard.agents.contracts import PROHIBITED_TOOL_EVIDENCE_FIELDS
from returnguard.agents.mcp_bridge import APPROVED_TOOLS
from returnguard.intelligence import (
    BigQueryIntelligenceRepository,
    IntelligenceConfig,
    ReturnIntelligenceService,
)
from returnguard.mcp import ReturnGuardMCPTools


DEFAULT_RETURN_ID = "RTN-M08-002"
DEFAULT_ASSESSMENT_AT = "2026-09-02T23:59:59+00:00"
SUPPORTED_LIVE_INTENTS = frozenset({
    WorkflowIntent.GENERAL_INVESTIGATION,
    WorkflowIntent.INSPECTION_REVIEW,
})
EXPECTED_AGENTS = {
    WorkflowIntent.GENERAL_INVESTIGATION: {
        "customer_behavior_agent",
        "product_intelligence_agent",
    },
    WorkflowIntent.INSPECTION_REVIEW: {
        "customer_behavior_agent",
        "product_intelligence_agent",
        "inspection_agent",
    },
}
ORCHESTRATOR_NAME = "returnguard_orchestrator_agent"
KNOWN_SPECIALISTS = frozenset().union(*EXPECTED_AGENTS.values())
ALLOWED_SPECIALIST_TOOLS = {
    "customer_behavior_agent": {
        "get_customer_intelligence", "get_return_behavior",
    },
    "product_intelligence_agent": {"get_product_intelligence"},
    "inspection_agent": {"get_return_inspection"},
}


@dataclass
class SpecialistExecution:
    state: str = "started"
    tools_invoked: list[str] = field(default_factory=list)
    tools_completed: list[str] = field(default_factory=list)
    result_produced: bool = False
    tool_evidence_count: int = 0
    findings_count: int = 0
    status: str | None = None
    confidence: str | None = None


@dataclass
class ExecutionDiagnostics:
    """Safe names/counts-only summary of an ADK orchestration execution."""

    intent: WorkflowIntent
    return_id: str
    trace_id: str | None
    assessment_id: str | None
    orchestrator_state: str = "not_started"
    transfers: list[tuple[str, str]] = field(default_factory=list)
    specialists: dict[str, SpecialistExecution] = field(default_factory=dict)
    agents_selected: list[str] = field(default_factory=list)
    agents_executed: list[str] = field(default_factory=list)
    agents_skipped: dict[str, str] = field(default_factory=dict)
    control_returned: bool = False

    def observe(self, event: dict[str, Any]) -> None:
        author = event.get("author")
        if author in KNOWN_SPECIALISTS:
            if author not in self.agents_executed:
                self.agents_executed.append(author)
            execution = self.specialists.setdefault(author, SpecialistExecution())
        else:
            execution = None

        for call in event.get("function_calls", []):
            name = call.get("name")
            if name == "transfer_to_agent" and call.get("target_agent"):
                target = call["target_agent"]
                transfer = (author or "unknown", target)
                if not self.transfers or self.transfers[-1] != transfer:
                    self.transfers.append(transfer)
                if target not in self.agents_selected:
                    self.agents_selected.append(target)
                if target in KNOWN_SPECIALISTS:
                    self.specialists.setdefault(target, SpecialistExecution())
            elif name in APPROVED_TOOLS and execution is not None:
                if name not in execution.tools_invoked:
                    execution.tools_invoked.append(name)

        for name in event.get("function_responses", []):
            if name not in APPROVED_TOOLS:
                continue
            owner = execution
            if owner is None:
                owner = next((
                    item for item in reversed(list(self.specialists.values()))
                    if name in item.tools_invoked and name not in item.tools_completed
                ), None)
            if owner is not None and name not in owner.tools_completed:
                owner.tools_completed.append(name)

        if event.get("is_final_response"):
            if author == ORCHESTRATOR_NAME:
                self.control_returned = bool(self.transfers)
            elif execution is not None:
                execution.state = "completed"
                execution.result_produced = event.get("output_contract") == "SpecialistResult"
                execution.tool_evidence_count = int(event.get("tool_evidence_count", 0))
                execution.findings_count = int(event.get("findings_count", 0))
                execution.status = event.get("status")
                execution.confidence = event.get("confidence")

    @property
    def specialist_result_count(self) -> int:
        return sum(item.result_produced for item in self.specialists.values())

    @property
    def invoked_tools(self) -> list[str]:
        return [
            tool
            for execution in self.specialists.values()
            for tool in execution.tools_invoked
        ]


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--return-id", default=DEFAULT_RETURN_ID)
    parser.add_argument("--assessment-at", default=DEFAULT_ASSESSMENT_AT)
    parser.add_argument(
        "--intent",
        type=WorkflowIntent,
        choices=list(WorkflowIntent),
        default=WorkflowIntent.INSPECTION_REVIEW,
    )
    parser.add_argument("--trace-id", default="orchestrator-smoke-trace")
    parser.add_argument("--assessment-id", default="orchestrator-smoke-assessment")
    parser.add_argument(
        "--inspection-available",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Current lifecycle availability used by deterministic planning.",
    )
    return parser.parse_args(argv)


def workflow_request(intent: WorkflowIntent) -> str:
    if intent == WorkflowIntent.INSPECTION_REVIEW:
        return (
            "Perform a grounded inspection review for this return. Coordinate the "
            "customer_behavior_agent, product_intelligence_agent, and inspection_agent "
            "through the hybrid structured workflow. Each specialist must use "
            "only its bounded MCP-backed tools and return compact tool_evidence. "
            "Synthesize grounded specialist results. Do not invent facts, "
            "do not expose prohibited raw evidence, and do not use fraud_labels."
        )
    return (
        "Perform a grounded general investigation for this return. Coordinate the "
        "customer_behavior_agent and product_intelligence_agent through the hybrid "
        "structured workflow. Each specialist must use only its bounded MCP-backed "
        "tools and return compact tool_evidence. Synthesize grounded results. Do not "
        "invent facts, expose prohibited raw evidence, or "
        "use fraud_labels."
    )


def _prohibited_paths(value: Any, path: str = "result") -> list[str]:
    """Return paths containing prohibited keys or prohibited field-name text."""

    failures: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).casefold() in PROHIBITED_TOOL_EVIDENCE_FIELDS:
                failures.append(child_path)
            failures.extend(_prohibited_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            failures.extend(_prohibited_paths(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        lowered = value.casefold()
        if any(field in lowered for field in PROHIBITED_TOOL_EVIDENCE_FIELDS):
            failures.append(path)
    return failures


def validate_plan(
    plan: OrchestrationPlan,
    context: AgentExecutionContext,
    intent: WorkflowIntent,
    *,
    inspection_available: bool,
) -> None:
    """Validate deterministic planning before any model or data operation."""

    if plan.return_id != context.return_id or plan.assessment_at != context.assessment_at:
        raise AssertionError("Orchestration plan does not preserve case context")
    if plan.request_intent != intent:
        raise AssertionError("Orchestration plan intent does not match the request")
    expected = set(EXPECTED_AGENTS[intent])
    if intent == WorkflowIntent.INSPECTION_REVIEW and not inspection_available:
        expected.remove("inspection_agent")
    if set(plan.agents_selected) != expected:
        raise AssertionError("Orchestration plan selected unexpected required agents")
    if len(plan.agents_selected) != len(set(plan.agents_selected)):
        raise AssertionError("Orchestration plan contains duplicate specialist agents")


def validate_result(
    result: OrchestrationResult,
    context: AgentExecutionContext,
    plan: OrchestrationPlan,
) -> dict[str, str]:
    """Enforce the live smoke invariants without reproducing business logic."""

    if result.return_id != context.return_id:
        raise AssertionError("Orchestration result return_id does not match the request")
    if result.assessment_at != context.assessment_at:
        raise AssertionError("Orchestration result assessment_at does not match the request")
    if result.request_intent != plan.request_intent:
        raise AssertionError("Orchestration result intent does not match the request")
    if not isinstance(result.status, AgentStatus):
        raise AssertionError("Orchestration result status is not a valid AgentStatus")
    if not result.agents_selected:
        raise AssertionError("Orchestrator selected no specialist agents")
    if not result.agents_executed:
        raise AssertionError("Orchestrator reported no executed specialist agents")
    if not result.specialist_results:
        raise AssertionError("Orchestrator returned no specialist results")

    required = set(plan.agents_selected)
    selected = set(result.agents_selected)
    executed = set(result.agents_executed)
    specialist_names = {item.agent_name for item in result.specialist_results}
    if result.agents_selected != plan.agents_selected:
        raise AssertionError("Final agents_selected does not match deterministic plan")
    if len(result.agents_executed) != len(executed):
        raise AssertionError("A selected specialist executed more than once")
    if not executed.issubset(selected):
        raise AssertionError("An unselected specialist was reported as executed")
    if specialist_names != executed:
        raise AssertionError("Specialist results do not match actual executions")
    for label, actual in (
        ("selected", selected),
        ("executed", executed),
        ("returned specialist results", specialist_names),
    ):
        missing = required - actual
        if missing:
            raise AssertionError(
                f"Required agents missing from {label}: {', '.join(sorted(missing))}"
            )

    for specialist in result.specialist_results:
        if specialist.status in {AgentStatus.FAILED, AgentStatus.UNAVAILABLE}:
            raise AssertionError(
                f"Specialist did not complete grounded analysis: {specialist.agent_name}"
            )
        if specialist.findings and not specialist.tool_evidence:
            raise AssertionError(
                f"Factual findings lack tool evidence: {specialist.agent_name}"
            )
        if specialist.agent_name in required and not specialist.tool_evidence:
            raise AssertionError(
                f"Executed specialist supplied no grounded MCP evidence: {specialist.agent_name}"
            )
        allowed = ALLOWED_SPECIALIST_TOOLS[specialist.agent_name]
        actual_tools = {item.tool_name for item in specialist.tool_evidence}
        if not actual_tools.issubset(allowed):
            raise AssertionError(
                f"Specialist used a tool outside its bounded ownership: {specialist.agent_name}"
            )

    if result.status != AgentStatus.COMPLETED:
        raise AssertionError("Required hybrid orchestration did not complete")

    serialized = result.model_dump(mode="json")
    prohibited = _prohibited_paths(serialized)
    if prohibited:
        raise AssertionError(
            "Prohibited field or raw-field reference found in orchestration output"
        )
    return {
        "structured": "PASS",
        "grounded": "PASS",
        "prohibited": "PASS",
    }


def print_unavailable_preflight(
    intent: WorkflowIntent, context: AgentExecutionContext
) -> None:
    preflight = plan_orchestration(intent, context)
    print("RETURNGUARD ORCHESTRATOR LIVE SMOKE: NOT RUN")
    print(f"Return: {context.return_id}")
    print(f"Intent: {intent.value}")
    for missing in preflight.missing_capabilities:
        print(f"Unavailable capability: {missing.capability} — {missing.reason}")
    print("No Gemini or BigQuery operation was started.")


async def run(
    args: argparse.Namespace,
    diagnostics: ExecutionDiagnostics | None = None,
) -> OrchestrationResult | None:
    context = AgentExecutionContext(
        return_id=args.return_id,
        assessment_at=parse_timestamp(args.assessment_at),
        trace_id=args.trace_id,
        assessment_id=args.assessment_id,
        request_id=f"{args.assessment_id}-request",
    )
    if args.intent not in SUPPORTED_LIVE_INTENTS:
        print_unavailable_preflight(args.intent, context)
        return None

    config = AgentConfig.from_env()
    config.validate_for_live_model()
    repository = BigQueryIntelligenceRepository(IntelligenceConfig.from_env())
    intelligence = ReturnIntelligenceService(repository)
    bridge = MCPToolBridge.from_returnguard_tools(ReturnGuardMCPTools(intelligence))
    plan = plan_orchestration(
        args.intent,
        context,
        inspection_available=args.inspection_available,
    )
    validate_plan(
        plan,
        context,
        args.intent,
        inspection_available=args.inspection_available,
    )
    if diagnostics is not None:
        diagnostics.orchestrator_state = "started"
        diagnostics.agents_selected = list(plan.agents_selected)
        diagnostics.agents_skipped = dict(plan.agents_skipped)
    result = await HybridOrchestrator(bridge, config).execute(
        plan,
        context,
        workflow_request=workflow_request(args.intent),
        event_observer=None if diagnostics is None else diagnostics.observe,
    )
    if diagnostics is not None:
        diagnostics.orchestrator_state = "completed"
        diagnostics.agents_selected = list(result.agents_selected)
        diagnostics.agents_executed = list(result.agents_executed)
        diagnostics.agents_skipped = dict(result.agents_skipped)
    checks = validate_result(result, context, plan)

    print("RETURNGUARD ORCHESTRATOR LIVE SMOKE: PASS")
    print(f"Return: {result.return_id}")
    print(f"Intent: {result.request_intent.value}")
    print(f"Status: {result.status.value}")
    print("\nAgents selected:")
    for name in result.agents_selected:
        print(f"- {name}")
    print("\nAgents executed:")
    for name in result.agents_executed:
        print(f"- {name}")
    print("\nSpecialist results:")
    for specialist in result.specialist_results:
        print(f"- {specialist.agent_name}: {specialist.status.value}")
    print(f"\nGrounded MCP evidence: {checks['grounded']}")
    print("Structured OrchestrationPlan: PASS")
    print(f"Structured OrchestrationResult: {checks['structured']}")
    print(f"Prohibited-field check: {checks['prohibited']}")
    print("BigQuery writes: 0")
    return result


def safe_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Expose Pydantic location/type/message without rejected input values."""

    summaries = []
    for error in exc.errors(
        include_input=False, include_context=False, include_url=False
    ):
        location = ".".join(str(part) for part in error.get("loc", ())) or "result"
        message = " ".join(str(error.get("msg", "Validation failed")).split())[:240]
        summaries.append({
            "location": location,
            "type": str(error.get("type", "validation_error")),
            "message": message,
        })
    return summaries


def print_execution_diagnostics(diagnostics: ExecutionDiagnostics) -> None:
    print("\nOrchestration execution:")
    print(f"- requested intent: {diagnostics.intent.value}")
    print(f"- return_id: {diagnostics.return_id}")
    print(f"- trace_id: {diagnostics.trace_id or 'not supplied'}")
    print(f"- assessment_id: {diagnostics.assessment_id or 'not supplied'}")
    print(f"- orchestrator: {diagnostics.orchestrator_state}")
    print(
        "- agents selected: "
        + (", ".join(diagnostics.agents_selected) or "none observed")
    )
    print(
        "- agents executed: "
        + (", ".join(diagnostics.agents_executed) or "none observed")
    )
    skipped = ", ".join(
        f"{name} ({reason})" for name, reason in diagnostics.agents_skipped.items()
    )
    print(f"- agents skipped: {skipped or 'none reported'}")
    print(f"- specialist results produced: {diagnostics.specialist_result_count}")

    print("\nDelegation transfers:")
    if diagnostics.transfers:
        for index, (source, target) in enumerate(diagnostics.transfers, start=1):
            print(f"- {index}. {source} -> {target}")
    else:
        print("- none observed")
    print(
        "- control returned to Orchestrator: "
        + ("yes" if diagnostics.control_returned else "no")
    )

    print("\nSpecialist execution:")
    if not diagnostics.specialists:
        print("- none observed")
    for name, execution in diagnostics.specialists.items():
        invoked = ", ".join(execution.tools_invoked) or "none observed"
        print(
            f"- {name}: state={execution.state}; tools={invoked}; "
            f"SpecialistResult={'yes' if execution.result_produced else 'no'}; "
            f"tool_evidence={execution.tool_evidence_count}; "
            f"findings={execution.findings_count}; "
            f"status={execution.status or 'unavailable'}; "
            f"confidence={execution.confidence or 'unavailable'}"
        )

    print("\nMCP/tool execution:")
    if not diagnostics.invoked_tools:
        print("- none observed")
    for specialist, execution in diagnostics.specialists.items():
        for tool in execution.tools_invoked:
            status = "completed" if tool in execution.tools_completed else "no response observed"
            print(f"- {tool}: specialist={specialist}; status={status}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    diagnostics = ExecutionDiagnostics(
        intent=args.intent,
        return_id=args.return_id,
        trace_id=args.trace_id,
        assessment_id=args.assessment_id,
    )
    try:
        result = asyncio.run(run(args, diagnostics))
        return 0 if result is not None or args.intent not in SUPPORTED_LIVE_INTENTS else 1
    except ValidationError as exc:
        diagnostics.orchestrator_state = "failed"
        print("RETURNGUARD ORCHESTRATOR LIVE SMOKE: FAIL")
        print("Failure: structured OrchestrationResult validation failed")
        print("Validation errors:")
        for error in safe_validation_errors(exc):
            print(f"- location: {error['location']}")
            print(f"  type: {error['type']}")
            print(f"  message: {error['message']}")
        print_execution_diagnostics(diagnostics)
        print("BigQuery writes: 0")
        return 1
    except Exception as exc:
        diagnostics.orchestrator_state = "failed"
        print("RETURNGUARD ORCHESTRATOR LIVE SMOKE: FAIL")
        print(f"Failure type: {type(exc).__name__}")
        print_execution_diagnostics(diagnostics)
        print("BigQuery writes: 0")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
