"""Explicitly unavailable Vision capability for checkpoint-one orchestration."""

from .contracts import AgentConfidence, AgentExecutionContext, AgentStatus, SpecialistResult


def unavailable_vision_result(context: AgentExecutionContext) -> SpecialistResult:
    return SpecialistResult(
        agent_name="evidence_vision_agent", return_id=context.return_id,
        assessment_at=context.assessment_at, status=AgentStatus.UNAVAILABLE,
        summary="Vision analysis is not implemented in this checkpoint.",
        limitations=["Evidence metadata is not equivalent to multimodal image analysis."],
        confidence=AgentConfidence.UNAVAILABLE, trace_id=context.trace_id,
        assessment_id=context.assessment_id,
    )
