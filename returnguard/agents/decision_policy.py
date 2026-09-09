"""Explicitly unavailable Decision & Policy capability."""

from .contracts import AgentConfidence, AgentExecutionContext, AgentStatus, SpecialistResult


def unavailable_decision_policy_result(context: AgentExecutionContext) -> SpecialistResult:
    return SpecialistResult(
        agent_name="decision_policy_agent", return_id=context.return_id,
        assessment_at=context.assessment_at, status=AgentStatus.UNAVAILABLE,
        summary="Decision and policy reasoning is unavailable until deterministic dependencies exist.",
        limitations=[
            "The deterministic Risk Engine is not implemented.",
            "The policy service and approved policy rules are not implemented.",
        ],
        confidence=AgentConfidence.UNAVAILABLE, trace_id=context.trace_id,
        assessment_id=context.assessment_id,
    )
