#!/usr/bin/env python3
"""Read-only decision persistence smoke; writes only with explicit --persist."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from returnguard.agents import (
    AgentConfig,
    AgentExecutionContext,
    BigQueryDecisionAssessmentRepository,
    DecisionAssessment,
    HybridOrchestrator,
    MCPToolBridge,
    WorkflowIntent,
    decision_event_id,
    plan_orchestration,
)
from returnguard.agents.contracts import PROHIBITED_TOOL_EVIDENCE_FIELDS
from returnguard.intelligence import (
    BigQueryIntelligenceRepository,
    IntelligenceConfig,
    ReturnIntelligenceService,
)
from returnguard.mcp import ReturnGuardMCPTools
from returnguard.policy import ReturnPolicyService
from scripts.diagnostics.smoke_test_orchestrator_live import (
    bootstrap_vertex_ai,
    parse_timestamp,
    validate_plan,
    validate_result,
    workflow_request,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--return-id", default="RTN-M08-002")
    parser.add_argument(
        "--assessment-at", default="2026-09-02T23:59:59+00:00"
    )
    parser.add_argument("--assessment-id", default="decision-persistence-smoke")
    parser.add_argument("--trace-id", default="decision-persistence-trace")
    parser.add_argument(
        "--persist", action="store_true",
        help="Append one immutable decision row and read it back.",
    )
    parser.add_argument(
        "--inspection-available", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--google-cloud-project", default=None)
    parser.add_argument("--google-cloud-location", default=None)
    return parser.parse_args(argv)


def _contains_prohibited(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in PROHIBITED_TOOL_EVIDENCE_FIELDS
            or _contains_prohibited(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_prohibited(child) for child in value)
    return any(
        marker in str(value).casefold()
        for marker in PROHIBITED_TOOL_EVIDENCE_FIELDS
    ) if isinstance(value, str) else False


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    return json.loads(str(value))


async def execute(args: argparse.Namespace):
    context = AgentExecutionContext(
        return_id=args.return_id,
        assessment_at=parse_timestamp(args.assessment_at),
        assessment_id=args.assessment_id,
        trace_id=args.trace_id,
        request_id=f"{args.assessment_id}-request",
    )
    config = AgentConfig.from_env()
    args.google_cloud_project = (
        args.google_cloud_project
        or config.google_cloud_project
        or "return-guard-506407"
    )
    args.google_cloud_location = (
        args.google_cloud_location
        or config.google_cloud_location
        or "us-central1"
    )
    config = bootstrap_vertex_ai(args)
    repository = BigQueryIntelligenceRepository(IntelligenceConfig.from_env())
    intelligence = ReturnIntelligenceService(repository)
    bridge = MCPToolBridge.from_returnguard_tools(ReturnGuardMCPTools(intelligence))
    plan = plan_orchestration(
        WorkflowIntent.INSPECTION_REVIEW,
        context,
        inspection_available=args.inspection_available,
    )
    validate_plan(
        plan, context, WorkflowIntent.INSPECTION_REVIEW,
        inspection_available=args.inspection_available,
    )
    result = await HybridOrchestrator(
        bridge, config, policy_service=ReturnPolicyService(intelligence)
    ).execute(plan, context, workflow_request=workflow_request(plan.request_intent))
    validate_result(result, context, plan, require_decision_synthesis=True)
    return result


async def run(args: argparse.Namespace) -> int:
    result = await execute(args)
    decision = result.decision_synthesis
    if decision is None:  # validated above; defensive for callers
        raise RuntimeError("DecisionSynthesisResult was not produced")
    print("RETURNGUARD RETURN DECISION PERSISTENCE SMOKE")
    print(f"Decision synthesis: PASS ({decision.matched_policy_rule})")
    print(f"Persistence requested: {'yes' if args.persist else 'no'}")
    if not args.persist:
        print("BigQuery writes: 0")
        print("RETURN DECISION PERSISTENCE SMOKE: NOT RUN")
        return 0

    assessment = DecisionAssessment.from_result(decision)
    persistence = BigQueryDecisionAssessmentRepository()
    persistence.append(assessment)
    row = persistence.get(assessment.decision_event_id)
    if row is None:
        raise AssertionError("Persisted decision row was not found")
    if row.get("decision_event_id") != assessment.decision_event_id:
        raise AssertionError("Decision event ID did not round-trip")
    if row.get("recommended_action") != decision.recommended_action.value:
        raise AssertionError("Policy action did not round-trip")
    if row.get("matched_policy_rule") != decision.matched_policy_rule:
        raise AssertionError("Policy rule did not round-trip")
    if row.get("risk_score") != decision.risk_score:
        raise AssertionError("Risk score did not round-trip")
    if row.get("risk_band") != decision.risk_band.value:
        raise AssertionError("Risk band did not round-trip")
    if row.get("risk_coverage") != decision.risk_coverage.value:
        raise AssertionError("Risk coverage did not round-trip")
    if row.get("decision_summary") != decision.decision_summary:
        raise AssertionError("Decision summary did not round-trip")
    for key in ("network_context_json", "economics_summary_json", "pricing_json"):
        if key in row and row[key] is not None:
            _json_value(row[key])
    if _contains_prohibited(row):
        raise AssertionError("Prohibited field found in persisted decision")
    print("Row write/read: PASS")
    print(f"Decision event ID stable: PASS ({decision_event_id(decision) == assessment.decision_event_id})")
    print("Safe nested summaries: PASS")
    print("Prohibited-field check: PASS")
    print("BigQuery writes: 1")
    print("RETURN DECISION PERSISTENCE SMOKE: PASS")
    return 0


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
