"""Minimal FastAPI boundary over the validated ReturnGuard services."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from returnguard.agents import (
    ADKAgentRuntime,
    AgentConfig,
    AgentExecutionContext,
    CopilotAnswer,
    HybridOrchestrator,
    MCPToolBridge,
    WorkflowIntent,
    decision_event_id,
    plan_orchestration,
)
from returnguard.agents.decision_repository import DecisionAssessment, BigQueryDecisionAssessmentRepository
from returnguard.agents.investigation_copilot import create_investigation_copilot
from returnguard.audit import AuditEvent, AuditRepository, BigQueryAuditRepository
from returnguard.api.repository import ApiReadRepository, BigQueryApiReadRepository
from returnguard.economics import EconomicsAssessment, EconomicsAssessmentRepository, BigQueryEconomicsAssessmentRepository
from returnguard.intelligence import BigQueryIntelligenceRepository, IntelligenceConfig, ReturnIntelligenceService, ReturnNotFoundError
from returnguard.mcp import ReturnGuardMCPTools
from returnguard.policy import PolicyAssessmentRepository, BigQueryPolicyAssessmentRepository, ReturnPolicyService
from returnguard.risk import BigQueryRiskEventRepository, RiskEventRepository, ReturnRiskService
from returnguard.observability import sanitize_merchant_text, sanitize_structured_value


class InvestigationRequest(BaseModel):
    intent: WorkflowIntent = WorkflowIntent.GENERAL_INVESTIGATION
    assessment_at: datetime | None = None
    assessment_id: str | None = None
    trace_id: str | None = None
    inspection_available: bool | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    assessment_at: datetime | None = None
    trace_id: str | None = None
    assessment_id: str | None = None


class CreateReturnRequest(BaseModel):
    order_item_id: int
    customer_id: int | None = None
    order_id: int | None = None
    reason: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)


@dataclass
class ApiDependencies:
    intelligence: ReturnIntelligenceService
    orchestrator: HybridOrchestrator
    policy_service: ReturnPolicyService
    audit: AuditRepository
    risk_events: RiskEventRepository | None = None
    economics: EconomicsAssessmentRepository | None = None
    policy: PolicyAssessmentRepository | None = None
    decisions: BigQueryDecisionAssessmentRepository | None = None
    agent_config: AgentConfig | None = None
    bridge: MCPToolBridge | None = None
    reads: ApiReadRepository | None = None


def build_default_dependencies() -> ApiDependencies:
    config = IntelligenceConfig.from_env()
    repository = BigQueryIntelligenceRepository(config)
    intelligence = ReturnIntelligenceService(repository)
    risk_events = BigQueryRiskEventRepository(config=config)
    economics = BigQueryEconomicsAssessmentRepository(config=config)
    policy = BigQueryPolicyAssessmentRepository(config=config)
    decisions = BigQueryDecisionAssessmentRepository(config=config)
    risk = ReturnRiskService(intelligence, event_repository=risk_events)
    policy_service = ReturnPolicyService(
        intelligence, risk=risk, economics_repository=economics, policy_repository=policy
    )
    tools = ReturnGuardMCPTools(intelligence)
    bridge = MCPToolBridge.from_returnguard_tools(tools)
    agent_config = AgentConfig.from_env()
    orchestrator = HybridOrchestrator(
        bridge, agent_config, policy_service=policy_service
    )
    return ApiDependencies(
        intelligence=intelligence, orchestrator=orchestrator,
        policy_service=policy_service, audit=BigQueryAuditRepository(config),
        risk_events=risk_events, economics=economics, policy=policy,
        decisions=decisions, agent_config=agent_config, bridge=bridge,
        reads=BigQueryApiReadRepository(config),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _safe_return(row: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "return_id", "order_id", "product_id", "status", "reason", "requested_at",
        "assessment_at", "anchor_sale_price", "source_type",
    )
    value = {key: row.get(key) for key in allowed if key in row}
    return sanitize_structured_value(value)


def _safe_result(result: Any) -> dict[str, Any]:
    value = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    value = sanitize_structured_value(value)
    if isinstance(value, dict):
        for key in ("scenario_id", "fraud_labels", "user_id", "network_identifiers", "linked_user_ids"):
            value.pop(key, None)
    return value


def _queue_item(row: dict[str, Any]) -> dict[str, Any]:
    risk = None if row.get("risk_score") is None else {
        "score": row["risk_score"], "band": str(row.get("risk_band", "")).lower()
    }
    return {
        "return_id": row["return_id"], "case_type": "CONTROLLED" if row.get("scenario_id") is not None else "SOURCE_BACKED",
        "source_type": "source_backed" if row.get("scenario_id") is None else "controlled",
        "customer": {"user_id": row.get("user_id"), "display_name": "Customer record"},
        "order_id": row.get("order_id"), "order_item_id": row.get("order_item_id"),
        "product": {"product_id": row.get("product_id"),
                    "name": row.get("product_name") or "Product details unavailable",
                    "category": row.get("product_category") or "Unknown", "image_url": None},
        "reason": row.get("reason") or "Unknown", "status": row.get("status") or "Unknown",
        "requested_at": row.get("requested_at"), "assessment_at": row.get("assessment_at"),
        "risk": risk, "decision": row.get("recommended_action"),
    }


def _detail_dto(data: Any, latest: dict[str, Any]) -> dict[str, Any]:
    metadata, customer, product = data.return_metadata, data.customer, data.product
    inspection = data.inspection
    missing_accessories = []
    if inspection and inspection.expected_accessories:
        present = {item.casefold() for item in inspection.accessories_present}
        missing_accessories = [item for item in inspection.expected_accessories if item.casefold() not in present]
    weight_delta = None
    weight_ratio = None
    if inspection and inspection.actual_weight_kg is not None and inspection.expected_weight_kg is not None:
        weight_delta = round(inspection.actual_weight_kg - inspection.expected_weight_kg, 6)
        if inspection.expected_weight_kg > 0:
            weight_ratio = abs(weight_delta) / inspection.expected_weight_kg
    risk_row = latest.get("risk") or {}
    economics_row = latest.get("economics") or {}
    policy_row = latest.get("policy") or {}
    decision_row = latest.get("decision") or {}
    reasons = risk_row.get("reasons_json") or []
    risk = None if not risk_row else {
        "score": risk_row.get("score"), "band": str(risk_row.get("band", "UNDETERMINED")).lower(),
        "coverage": risk_row.get("coverage"), "group_scores": risk_row.get("group_scores_json") or {},
        "product_mitigation": risk_row.get("product_mitigation", 0),
        "reason_codes": [item.get("code") for item in reasons],
        "signal_contributions": [{"name": item.get("code"), "severity": "high" if item.get("contribution", 0) >= 6 else "medium" if item.get("contribution", 0) >= 3 else "low", "supporting_fact": item.get("explanation", ""), "source": item.get("group", "deterministic"), "contribution_pct": item.get("contribution")} for item in reasons],
        "patterns": risk_row.get("patterns") or [], "limitations": risk_row.get("limitations") or [],
    }
    decision = None if not policy_row else {
        "recommended_action": policy_row.get("action"), "matched_policy_rule": policy_row.get("matched_rule"),
        "policy_name": policy_row.get("matched_rule"), "policy_version": policy_row.get("policy_version"),
        "policy_explanation": decision_row.get("decision_summary") or " ".join(policy_row.get("rationale") or []),
        "suggested_fee": policy_row.get("return_fee"), "limitations": decision_row.get("limitations") or [],
        "status": "completed", "confidence": "deterministic policy",
        "strongest_evidence": decision_row.get("strongest_evidence") or [],
        "mitigating_context": decision_row.get("mitigating_context") or [],
    }
    economics = None if not economics_row else {
        "item_value": economics_row.get("current_item_value"), "product_cost": economics_row.get("product_cost"),
        "reverse_logistics_cost": economics_row.get("reverse_logistics_cost"),
        "inspection_cost": economics_row.get("inspection_cost"), "recovery_value": economics_row.get("recovery_value"),
        "net_return_cost": economics_row.get("estimated_net_return_cost"), "calculation_at": economics_row.get("assessment_at"),
    }
    return sanitize_structured_value({
        "return_id": metadata.return_id, "source_type": "source_backed" if metadata.source_type == "source_backed" else "controlled",
        "return": {"order_id": metadata.order_id, "order_item_id": metadata.order_item_id,
                   "user_id": metadata.user_id, "product_id": metadata.product_id,
                   "reason": metadata.reason, "status": metadata.status,
                   "requested_at": metadata.requested_at, "assessment_at": metadata.assessment_at,
                   "sale_price": data.economics.current_item_value,
                   "product_category": product.category},
        "customer": {"status": "completed", "data_state": customer.customer_history_data_origin,
                     "summary": "Deterministic point-in-time customer history.",
                     "metrics": {"tenure_months": None if customer.customer_tenure_days is None else round(customer.customer_tenure_days / 30, 1),
                                 "order_count": customer.total_orders, "total_spend": customer.total_spend,
                                 "return_count": customer.lifetime_historical_returns,
                                 "return_rate": customer.lifetime_return_rate,
                                 "recent_return_count": customer.returns_30d},
                     "findings": [], "limitations": []},
        "product": {"status": "completed", "data_state": product.data_origin,
                    "summary": "Deterministic point-in-time product and category history.",
                    "metrics": {"historical_return_rate": product.product_return_rate,
                                "sample_count": product.product_items_observed,
                                "category_return_rate": product.category_return_rate},
                    "findings": ["Product return rate is elevated relative to category history."] if product.product_return_rate_elevated else [],
                    "limitations": []},
        "inspection": None if inspection is None else {"available": True, "item_present": inspection.item_present,
                       "condition": inspection.condition or "unknown", "expected_weight_kg": inspection.expected_weight_kg,
                       "actual_weight_kg": inspection.actual_weight_kg, "weight_delta_kg": weight_delta,
                       "weight_ratio": weight_ratio, "serial_comparison_performed": inspection.serial_mismatch is not None,
                       "serial_mismatch": inspection.serial_mismatch,
                       "accessory_comparison_performed": bool(inspection.expected_accessories),
                       "missing_accessories": missing_accessories, "inspected_at": inspection.inspected_at},
        "evidence": {"available": bool(data.evidence), "items": [{"evidence_id": item.evidence_id,
                     "evidence_type": item.evidence_type, "stage": item.stage,
                     "observed_at": item.submitted_at, "display_url": None} for item in data.evidence]},
        "network": {"available": data.network.network_history_confidence != "unavailable",
                    "contextual_evidence_only": True, "relationship_count": data.network.linked_return_count,
                    "relationships": [], "cluster_notes": "Controlled relationship context only; not standalone fraud evidence."},
        "economics": economics, "risk": risk, "decision": decision, "vision": None,
        "limitations": ([] if latest else ["No persisted assessment is available yet."]),
        "evidence_coverage": None if risk is None else str(risk.get("coverage", "")).lower(),
    })


def _unassessed_detail(row: dict[str, Any]) -> dict[str, Any]:
    """Safe detail projection for newly-created requests without assessment_at."""
    return sanitize_structured_value({
        "return_id": row.get("return_id"), "source_type": row.get("source_type"),
        "return": {"order_id": row.get("order_id"), "order_item_id": row.get("order_item_id"),
                   "user_id": row.get("user_id"), "product_id": row.get("product_id"),
                   "reason": row.get("reason"), "status": row.get("status"),
                   "requested_at": row.get("requested_at"), "assessment_at": None,
                   "sale_price": row.get("anchor_sale_price")},
        "customer": None, "product": None, "inspection": None,
        "evidence": {"available": False, "items": []}, "network": None,
        "economics": None, "risk": None, "decision": None, "vision": None,
        "limitations": ["No persisted assessment is available yet."],
        "evidence_coverage": None,
    })


def _audit(deps: ApiDependencies, **kwargs: Any) -> None:
    deps.audit.append(AuditEvent.create(**kwargs))


def create_app(dependencies: ApiDependencies | None = None) -> FastAPI:
    deps = dependencies
    application = FastAPI(title="ReturnGuard", version="1.0")
    origins = [item.strip() for item in os.getenv(
        "RETURNGUARD_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if item.strip()]
    application.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )

    def get_deps() -> ApiDependencies:
        nonlocal deps
        if deps is None:
            deps = build_default_dependencies()
        return deps

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "returnguard"}

    @application.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        rows = get_deps().intelligence.repository.list_returns(100)
        items = [_queue_item(row) for row in rows]
        assessed = [item for item in items if item["risk"] is not None]
        decisions = [item for item in items if item.get("decision")]
        attention_actions = {"ESCALATE_TO_SPECIALIST", "MANUAL_REVIEW", "REQUIRE_INSPECTION", "REQUEST_EVIDENCE"}
        attention = [item for item in items if item.get("decision") in attention_actions or (item.get("risk") and item["risk"]["band"] in {"high", "critical"})]
        bands = {"low": sum(1 for item in assessed if item["risk"]["band"] == "low"),
                 "medium": sum(1 for item in assessed if item["risk"]["band"] == "medium"),
                 "high": sum(1 for item in assessed if item["risk"]["band"] in {"high", "critical"})}
        return {"counts": {"total_returns": len(items), "controlled_returns": sum(item["source_type"] == "controlled" for item in items),
                            "source_backed_returns": sum(item["source_type"] == "source_backed" for item in items),
                            "needs_review": sum(item.get("decision") in {"MANUAL_REVIEW", "ESCALATE_TO_SPECIALIST"} for item in items),
                            "auto_approved": sum(item.get("decision") == "AUTO_APPROVE" for item in items),
                            "rejected": sum(item.get("decision") == "REJECT_RETURN" for item in items),
                            "awaiting_inspection": sum(item.get("decision") == "REQUIRE_INSPECTION" for item in items),
                            "completed": len(decisions)},
                "risk_distribution": bands, "financials": {"potential_exposure": None, "loss_prevented": None},
                "recent_returns": items[:10], "attention_returns": attention[:10]}

    @application.get("/api/returns")
    def list_returns(
        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
        q: str | None = None, status: str | None = None, source_type: str | None = None,
        risk_band: str | None = None,
    ) -> dict[str, Any]:
        rows = get_deps().intelligence.repository.list_returns(100)
        items = [_queue_item(row) for row in rows]
        if q:
            needle = q.casefold()
            items = [item for item in items if needle in " ".join((
                str(item.get("return_id", "")), str(item.get("order_id", "")),
                str(item.get("product", {}).get("name", "")),
                str(item.get("product", {}).get("product_id", "")),
                str(item.get("customer", {}).get("user_id", "")),
            )).casefold()]
        if status:
            items = [item for item in items if item["status"] == status]
        if source_type:
            items = [item for item in items if item["source_type"] == source_type]
        if risk_band:
            items = [item for item in items if item["risk"] and item["risk"]["band"] == risk_band.lower()]
        total = len(items)
        return {"items": items[offset:offset + limit], "total": total, "limit": limit, "offset": offset}

    @application.get("/api/returns/{return_id}")
    def return_detail(return_id: str) -> dict[str, Any]:
        service = get_deps().intelligence
        try:
            data = service.get_return_intelligence(return_id)
        except ReturnNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Return not found") from exc
        except ValueError:
            reads = get_deps().reads
            row = reads.get_return(return_id) if reads and hasattr(reads, "get_return") else None
            if row is None:
                raise HTTPException(status_code=404, detail="Return not found")
            return _unassessed_detail(row)
        latest = get_deps().reads.latest_for_return(return_id) if get_deps().reads else {}
        return _detail_dto(data, latest)

    @application.get("/api/source/customers")
    def source_customers(search: str | None = None, limit: int = Query(20, ge=1, le=20)) -> dict[str, Any]:
        reads = get_deps().reads
        if reads is None or not hasattr(reads, "source_customers"):
            raise HTTPException(status_code=503, detail="Source lookup unavailable")
        rows = reads.source_customers(search, min(limit, 20))
        return {"items": [{"user_id": row["user_id"], "display_name": f"Customer {row['user_id']}"} for row in rows]}

    @application.get("/api/source/customers/{user_id}/orders")
    def source_customer_orders(user_id: int) -> dict[str, Any]:
        reads = get_deps().reads
        if reads is None or not hasattr(reads, "source_orders"):
            raise HTTPException(status_code=503, detail="Source lookup unavailable")
        return {"items": reads.source_orders(user_id)}

    @application.get("/api/source/orders/{order_id}/items")
    def source_order_items(order_id: int) -> dict[str, Any]:
        reads = get_deps().reads
        if reads is None or not hasattr(reads, "source_order_items"):
            raise HTTPException(status_code=503, detail="Source lookup unavailable")
        return {"items": reads.source_order_items(order_id)}

    @application.post("/api/returns", status_code=201)
    def create_return(request: CreateReturnRequest) -> dict[str, Any]:
        reads = get_deps().reads
        if reads is None or not hasattr(reads, "create_return"):
            raise HTTPException(status_code=503, detail="Return creation unavailable")
        return_id = f"RTN-USER-{uuid.uuid4().hex[:12].upper()}"
        try:
            created = reads.create_return(return_id=return_id, order_item_id=request.order_item_id,
                                          customer_id=request.customer_id, order_id=request.order_id,
                                          reason=request.reason.strip(), comment=request.comment)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(get_deps(), return_id=return_id, assessment_id=None, event_type="RETURN_REQUESTED",
               event_at=created["requested_at"], actor_type="api", actor_name="returnguard_api",
               status_to="REQUESTED", summary="Return request created", trace_id=None)
        return {key: created[key] for key in ("return_id", "source_type", "status", "requested_at", "assessment_at")}

    @application.post("/api/returns/{return_id}/investigate")
    async def investigate(return_id: str, request: InvestigationRequest | None = None) -> dict[str, Any]:
        request = request or InvestigationRequest()
        d = get_deps()
        if request.intent in {WorkflowIntent.VISION_REVIEW, WorkflowIntent.RISK_POLICY_DECISION}:
            raise HTTPException(status_code=422, detail="Requested capability is unavailable in this checkpoint")
        try:
            effective = d.intelligence.resolve_assessment_at(return_id, request.assessment_at)
        except ReturnNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Return not found") from exc
        except ValueError:
            # Newly raised source-backed requests have no assessment_at yet;
            # use their immutable request timestamp as the first assessment
            # cutoff while preserving the caller override when supplied.
            row = d.reads.get_return(return_id) if d.reads and hasattr(d.reads, "get_return") else None
            if row is None or row.get("requested_at") is None:
                raise HTTPException(status_code=422, detail="Return has no usable assessment timestamp")
            effective = _utc(row["requested_at"])
        assessment_id = request.assessment_id or _id("assessment")
        trace_id = request.trace_id or _id("trace")
        context = AgentExecutionContext(
            return_id=return_id, assessment_at=_utc(effective),
            assessment_id=assessment_id, trace_id=trace_id, request_id=_id("request"),
        )
        inspection_available = request.inspection_available
        if inspection_available is None:
            inspection_available = d.intelligence.get_inspection(return_id, context.assessment_at) is not None
        plan = plan_orchestration(request.intent, context, inspection_available=inspection_available)
        _audit(d, return_id=return_id, assessment_id=assessment_id, event_type="ASSESSMENT_STARTED",
               event_at=context.assessment_at, actor_type="api", actor_name="returnguard_api",
               summary="Assessment started", trace_id=trace_id)
        try:
            result = await d.orchestrator.execute(plan, context)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Assessment dependency unavailable") from exc
        if result.risk is None or result.policy is None or result.decision_synthesis is None:
            raise HTTPException(status_code=503, detail="Authoritative assessment result unavailable")

        # Persist the exact computed artifacts; no recalculation occurs here.
        if d.risk_events:
            d.risk_events.append(result.risk)
        if d.economics and result.economics:
            from returnguard.economics.models import EconomicsAssessment
            from returnguard.intelligence.models import ReturnEconomics
            econ = ReturnEconomics(**result.economics.model_dump())
            d.economics.append(EconomicsAssessment.from_result(
                return_id=return_id, assessment_id=assessment_id,
                assessment_at=context.assessment_at, result=econ,
            ))
        if d.policy:
            d.policy.append(result.policy)
        if d.decisions:
            d.decisions.append(DecisionAssessment.from_result(result.decision_synthesis))
        _audit(d, return_id=return_id, assessment_id=assessment_id, event_type="RISK_ASSESSED",
               event_at=context.assessment_at, actor_type="service", actor_name="risk-v1",
               summary="Risk assessment completed", details={"risk_event_id": result.risk.risk_event_id,
               "score": result.risk.score, "band": result.risk.band.value}, trace_id=trace_id)
        _audit(d, return_id=return_id, assessment_id=assessment_id, event_type="POLICY_EVALUATED",
               event_at=context.assessment_at, actor_type="service", actor_name="policy-v1",
               summary="Policy evaluation completed", details={"matched_rule": result.policy.matched_rule,
               "action": result.policy.action.value}, trace_id=trace_id)
        _audit(d, return_id=return_id, assessment_id=assessment_id, event_type="DECISION_GENERATED",
               event_at=context.assessment_at, actor_type="agent", actor_name="decision_synthesis",
               summary="Decision synthesis generated", details={"decision_event_id": decision_event_id(result.decision_synthesis),
               "action": result.decision_synthesis.recommended_action.value}, trace_id=trace_id)
        return _safe_result(result)

    @application.post("/api/returns/{return_id}/ask")
    async def ask(return_id: str, request: AskRequest) -> dict[str, Any]:
        d = get_deps()
        try:
            effective = d.intelligence.resolve_assessment_at(return_id, request.assessment_at)
        except ReturnNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Return not found") from exc
        context = AgentExecutionContext(
            return_id=return_id, assessment_at=_utc(effective),
            assessment_id=request.assessment_id or _id("ask-assessment"),
            trace_id=request.trace_id or _id("ask-trace"), request_id=_id("request"),
        )
        if d.bridge is None:
            raise HTTPException(status_code=503, detail="Copilot unavailable")
        config = d.agent_config or AgentConfig.from_env()
        answer = await ADKAgentRuntime(config).run(
            create_investigation_copilot(d.bridge, context, config),
            request.question, context, CopilotAnswer,
        )
        safe = _safe_result(answer)
        safe["answer"] = sanitize_merchant_text(answer.answer)
        safe["trace_id"] = answer.trace_id or context.trace_id
        safe["assessment_id"] = answer.assessment_id or context.assessment_id
        return safe

    @application.get("/api/returns/{return_id}/assessments")
    def assessments(return_id: str) -> dict[str, Any]:
        d = get_deps()
        if d.intelligence.repository.get_return(return_id) is None:
            raise HTTPException(status_code=404, detail="Return not found")
        rows = d.reads.assessment_history(return_id) if d.reads else []
        return {"items": [{"assessment_id": row.get("assessment_id"),
                           "assessment_at": row.get("assessment_at"), "stage": "assessment",
                           "stage_label": "Risk assessment",
                           "risk": None if row.get("score") is None else {"score": row.get("score"),
                           "band": str(row.get("band", "")).lower()}, "decision": None} for row in rows]}

    @application.get("/api/returns/{return_id}/timeline")
    def timeline(return_id: str) -> list[dict[str, Any]]:
        d = get_deps()
        try:
            if d.intelligence.repository.get_return(return_id) is None:
                raise HTTPException(status_code=404, detail="Return not found")
            rows = d.audit.list_for_return(return_id)
        except ReturnNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Return not found") from exc
        return [{"id": row.get("audit_event_id"), "title": str(row.get("event_type", "Event")).replace("_", " ").title(),
                 "timestamp": row.get("event_at"), "facts": [row.get("summary")] if row.get("summary") else [],
                 "source_indicator": row.get("actor_name") or row.get("actor_type") or "ReturnGuard",
                 "severity": "flag" if row.get("event_type") in {"RISK_ASSESSED", "DECISION_GENERATED"} else "normal"}
                for row in rows]

    return application


app = create_app()
