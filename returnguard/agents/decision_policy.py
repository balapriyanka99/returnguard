"""Explicitly unavailable Decision & Policy capability."""

from .contracts import AgentConfidence, AgentExecutionContext, AgentStatus, SpecialistResult


def unavailable_decision_policy_result(context: AgentExecutionContext) -> SpecialistResult:
    return SpecialistResult(
        agent_name="decision_policy_agent", return_id=context.return_id,
        assessment_at=context.assessment_at, status=AgentStatus.UNAVAILABLE,
        summary="A separate Decision & Policy specialist agent is not implemented.",
        limitations=[
            "Risk-v1 and Policy-v1 remain authoritative deterministic services.",
            "Decision synthesis explains their results but does not replace them.",
        ],
        confidence=AgentConfidence.UNAVAILABLE, trace_id=context.trace_id,
        assessment_id=context.assessment_id,
    )
