"""Gemini narrative synthesis over authoritative ReturnGuard decision state."""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.agents import LlmAgent

from returnguard.intelligence.models import ReturnEconomics
from returnguard.observability import (
    log_action,
    log_decision_synthesis_summary,
    sanitize_structured_value,
)
from returnguard.policy.models import PolicyEvaluation
from returnguard.risk.models import RiskAssessment, RiskPattern

from .config import AgentConfig
from .contracts import (
    AgentExecutionContext,
    DecisionEconomicsSummary,
    DecisionPricingSummary,
    DecisionSynthesisInput,
    DecisionSynthesisNarrative,
    DecisionSynthesisResult,
    OrchestrationPlan,
    SafeNetworkContext,
    SpecialistResult,
)
from .prompts import DECISION_SYNTHESIS_INSTRUCTION
from .runtime import ADKAgentRuntime, EventObserver


logger = logging.getLogger(__name__)
NETWORK_PATTERNS = {
    RiskPattern.POSSIBLE_LINKED_ACCOUNT_ABUSE,
    RiskPattern.POSSIBLE_SHARED_RETURN_PATTERN,
}


def create_decision_synthesis_agent(
    config: AgentConfig | None = None,
    *,
    model: Any = None,
) -> LlmAgent:
    """Create a synthesis-only ADK agent with no tools or sub-agents."""

    config = config or AgentConfig.from_env()
    return LlmAgent(
        name="decision_synthesis_agent",
        description=(
            "Explains authoritative ReturnGuard Risk-v1, economics, and Policy-v1 "
            "results without changing them."
        ),
        model=model or config.model,
        instruction=DECISION_SYNTHESIS_INSTRUCTION,
        output_schema=DecisionSynthesisNarrative,
    )


def safe_network_context(risk: RiskAssessment) -> SafeNetworkContext | None:
    """Project Risk-v1 network output into an identifier-free context."""

    contribution = risk.group_scores.get("NETWORK_CONTEXT", 0)
    patterns = [pattern.value for pattern in risk.patterns if pattern in NETWORK_PATTERNS]
    network_limitations = [
        limitation for limitation in risk.limitations
        if "network" in limitation.casefold()
    ]
    if contribution == 0 and not patterns and not network_limitations:
        return None
    if contribution:
        summary = (
            f"Controlled network context contributed {contribution} capped Risk-v1 "
            "point(s); it is contextual evidence only."
        )
    else:
        summary = "Network context was unavailable or did not add Risk-v1 points."
    return SafeNetworkContext(
        contribution=contribution,
        patterns=patterns,
        summary=summary,
        limitations=network_limitations,
    )


def build_decision_input(
    plan: OrchestrationPlan,
    *,
    agents_executed: list[str],
    agents_skipped: dict[str, str],
    specialist_results: list[SpecialistResult],
    risk: RiskAssessment,
    economics: ReturnEconomics,
    policy: PolicyEvaluation,
    vision_result: SpecialistResult | None = None,
) -> DecisionSynthesisInput:
    """Build the exact typed and sanitized state presented to Gemini."""

    return DecisionSynthesisInput(
        request_intent=plan.request_intent,
        return_id=plan.return_id,
        assessment_id=plan.assessment_id,
        assessment_at=plan.assessment_at,
        agents_selected=list(plan.agents_selected),
        agents_executed=list(agents_executed),
        agents_skipped=dict(agents_skipped),
        specialist_results=specialist_results,
        network_context=safe_network_context(risk),
        risk=risk,
        economics=DecisionEconomicsSummary.from_economics(economics),
        policy=policy,
        vision_result=vision_result,
        missing_capabilities=plan.missing_capabilities,
    )


def _pricing(policy: PolicyEvaluation) -> DecisionPricingSummary | None:
    if policy.return_fee is None:
        return None
    rationale = (
        list(policy.pricing.pricing_rationale)
        if policy.pricing is not None
        else list(policy.rationale)
    )
    return DecisionPricingSummary(
        normalized_reason=policy.normalized_reason,
        return_fee=policy.return_fee,
        fee_reason=policy.fee_reason,
        pricing_rationale=rationale,
    )


def _fallback_narrative(value: DecisionSynthesisInput) -> DecisionSynthesisNarrative:
    """Build a safe explanation when Gemini is unavailable."""

    score = "undetermined" if value.risk.score is None else str(value.risk.score)
    strongest = [
        f"{reason.code}: {reason.explanation}"
        for reason in value.risk.reasons
        if reason.contribution > 0
    ]
    mitigating = [
        f"Product context mitigated the suspiciousness score by {abs(value.risk.product_mitigation)} point(s)."
    ] if value.risk.product_mitigation < 0 else []
    limitations = list(value.risk.limitations)
    if not any(item.agent_name == "inspection_agent" for item in value.specialist_results):
        limitations.append("Warehouse inspection specialist context was not available.")
    if any(item.capability == "vision" for item in value.missing_capabilities):
        limitations.append("Vision analysis is not implemented and added no risk.")
    if value.network_context is None:
        limitations.append("Network context was unavailable or immaterial; no risk was added.")
    return DecisionSynthesisNarrative(
        decision_summary=(
            f"Deterministic Risk-v1 is {score} ({value.risk.band.value}); "
            f"Policy-v1 selected {value.policy.action.value} under "
            f"{value.policy.matched_rule}."
        ),
        strongest_evidence=strongest,
        mitigating_context=mitigating,
        network_explanation=None,
        policy_reasoning=list(value.policy.rationale),
        limitations=list(dict.fromkeys(limitations)),
    )


def finalize_decision_synthesis(
    value: DecisionSynthesisInput,
    narrative: DecisionSynthesisNarrative | dict[str, Any],
) -> DecisionSynthesisResult:
    """Merge narrative only, reconstructing every authoritative field."""

    authored = (
        narrative
        if isinstance(narrative, DecisionSynthesisNarrative)
        else DecisionSynthesisNarrative.model_validate(narrative)
    )
    authored = DecisionSynthesisNarrative.model_validate(
        sanitize_structured_value(authored.model_dump())
    )
    network_context = value.network_context
    if network_context is not None and authored.network_explanation:
        network_context = network_context.model_copy(update={
            "merchant_explanation": authored.network_explanation,
        })
    origins = [value.risk.data_origin, value.economics.data_origin]
    origins.extend(
        evidence.data_origin
        for specialist in value.specialist_results
        for evidence in specialist.tool_evidence
        if evidence.data_origin
    )
    return DecisionSynthesisResult.model_validate({
        "return_id": value.return_id,
        "assessment_id": value.assessment_id,
        "assessment_at": value.assessment_at,
        "recommended_action": value.policy.action,
        "matched_policy_rule": value.policy.matched_rule,
        "risk_score": value.risk.score,
        "risk_band": value.risk.band,
        "risk_coverage": value.risk.coverage,
        "decision_summary": authored.decision_summary,
        "strongest_evidence": authored.strongest_evidence,
        "mitigating_context": authored.mitigating_context,
        "network_context": network_context,
        "economics_summary": value.economics,
        "pricing": _pricing(value.policy),
        "policy_reasoning": (
            authored.policy_reasoning or list(value.policy.rationale)
        ),
        "limitations": list(dict.fromkeys([
            *authored.limitations,
            *value.risk.limitations,
        ])),
        "specialists_used": list(value.agents_executed),
        "data_origin": list(dict.fromkeys(origins)),
    })


class DecisionSynthesisAgent:
    """Run Gemini narrative synthesis and preserve a deterministic fallback."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        model: Any = None,
        runtime_factory=None,
    ) -> None:
        self.config = config or AgentConfig.from_env()
        self.model = model
        self.runtime_factory = runtime_factory or (lambda: ADKAgentRuntime(self.config))

    async def synthesize(
        self,
        value: DecisionSynthesisInput,
        context: AgentExecutionContext,
        *,
        event_observer: EventObserver | None = None,
    ) -> DecisionSynthesisResult:
        prompt = (
            "Explain this authoritative structured ReturnGuard decision state. "
            "Author only the narrative contract.\n"
            + json.dumps(
                sanitize_structured_value(value.model_dump(mode="json")),
                sort_keys=True,
            )
        )
        log_action(
            logger,
            operation_type="agent",
            operation_name="decision_synthesis_agent",
            action="Synthesize authoritative risk, economics, and policy results",
            status="started",
            return_id=context.return_id,
            assessment_at=context.assessment_at,
        )
        try:
            narrative = await self.runtime_factory().run(
                create_decision_synthesis_agent(self.config, model=self.model),
                prompt,
                context,
                DecisionSynthesisNarrative,
                event_observer=event_observer,
            )
        except Exception as exc:
            log_action(
                logger,
                operation_type="agent",
                operation_name="decision_synthesis_agent",
                action="Use deterministic decision synthesis fallback",
                status="failed",
                return_id=context.return_id,
                assessment_at=context.assessment_at,
                level=logging.ERROR,
            )
            narrative = _fallback_narrative(value)
        result = finalize_decision_synthesis(value, narrative)
        log_action(
            logger,
            operation_type="agent",
            operation_name="decision_synthesis_agent",
            action="Validate protected decision synthesis result",
            status="completed",
            return_id=context.return_id,
            assessment_at=context.assessment_at,
        )
        log_decision_synthesis_summary(logger, result)
        return result
