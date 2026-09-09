"""ReturnGuard MCP application tools.

This module is intentionally transport-neutral: a later MCP server can
register these typed callables without coupling business services to an MCP
SDK. No Intelligence calculation or risk scoring lives here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from returnguard.intelligence.models import StructuredModel
from returnguard.intelligence.service import ReturnIntelligenceService
from returnguard.observability import bind_execution_context, logged_operation

from .contracts import (
    IntelligenceToolRequest,
    MCPToolResult,
    RiskAssessmentRequest,
    RiskAssessmentResult,
)
from .risk import RiskEngine, RiskEngineUnavailableError


logger = logging.getLogger(__name__)

TOOL_NAMES = (
    "get_customer_intelligence",
    "get_product_intelligence",
    "get_return_behavior",
    "get_network_intelligence",
    "calculate_return_economics",
    "get_return_evidence",
    "get_return_inspection",
    "assess_risk",
)


def _structured(value: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    if value is None:
        return None
    if isinstance(value, StructuredModel):
        return value.to_dict()
    if isinstance(value, list):
        return [item.to_dict() if isinstance(item, StructuredModel) else item for item in value]
    if isinstance(value, dict):
        return value
    raise TypeError(f"Tool delegate returned unsupported structured type: {type(value).__name__}")


class ReturnGuardMCPTools:
    """Eight typed tool entry points backed by injected deterministic services."""

    def __init__(
        self,
        intelligence: ReturnIntelligenceService,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self.intelligence = intelligence
        self.risk_engine = risk_engine

    def _intelligence_tool(
        self,
        tool_name: str,
        request: IntelligenceToolRequest,
        delegate: Callable[..., Any],
        *,
        accepts_assessment_at: bool,
        result_transform: Callable[[Any], Any] | None = None,
    ) -> MCPToolResult:
        with bind_execution_context(request.context):
            with logged_operation(
                logger,
                operation_type="mcp_tool",
                operation_name=tool_name,
                return_id=request.return_id,
                assessment_at=request.assessment_at,
            ) as log_result:
                effective_request_at = request.assessment_at
                if effective_request_at is None:
                    effective_request_at = self.intelligence.resolve_assessment_at(
                        request.return_id
                    )
                if accepts_assessment_at:
                    value = delegate(request.return_id, effective_request_at)
                else:
                    value = delegate(request.return_id)
                if result_transform is not None:
                    value = result_transform(value)
                payload = _structured(value)
                effective_assessment_at = getattr(value, "assessment_at", effective_request_at)
                if effective_assessment_at is not None:
                    log_result["assessment_at"] = effective_assessment_at.isoformat()
                log_result["result_state"] = "missing" if payload is None else "available"
                return MCPToolResult(
                    tool_name=tool_name,
                    return_id=request.return_id,
                    assessment_at=effective_assessment_at,
                    trace_id=request.context.trace_id,
                    assessment_id=request.context.assessment_id,
                    result=payload,
                )

    def get_customer_intelligence(self, request: IntelligenceToolRequest) -> MCPToolResult:
        return self._intelligence_tool(
            "get_customer_intelligence", request,
            self.intelligence.get_customer_intelligence, accepts_assessment_at=True,
        )

    def get_product_intelligence(self, request: IntelligenceToolRequest) -> MCPToolResult:
        return self._intelligence_tool(
            "get_product_intelligence", request,
            self.intelligence.get_product_intelligence, accepts_assessment_at=True,
        )

    def get_return_behavior(self, request: IntelligenceToolRequest) -> MCPToolResult:
        return self._intelligence_tool(
            "get_return_behavior", request,
            self.intelligence.get_return_behavior_intelligence, accepts_assessment_at=True,
        )

    def get_network_intelligence(self, request: IntelligenceToolRequest) -> MCPToolResult:
        return self._intelligence_tool(
            "get_network_intelligence", request,
            self.intelligence.get_network_intelligence, accepts_assessment_at=True,
        )

    def calculate_return_economics(self, request: IntelligenceToolRequest) -> MCPToolResult:
        return self._intelligence_tool(
            "calculate_return_economics", request,
            self.intelligence.get_return_economics, accepts_assessment_at=True,
        )

    def get_return_evidence(self, request: IntelligenceToolRequest) -> MCPToolResult:
        def evidence_payload(records: Any) -> dict[str, Any]:
            serialized = _structured(records) or []
            visual_available = any(
                bool(record.get("image_uri") or record.get("reference_image_uri"))
                for record in serialized
            )
            return {
                "records": serialized,
                "visual_evidence_available": visual_available,
                "visual_evidence_state": "available" if visual_available else "insufficient",
            }

        return self._intelligence_tool(
            "get_return_evidence", request,
            self.intelligence.get_evidence, accepts_assessment_at=True,
            result_transform=evidence_payload,
        )

    def get_return_inspection(self, request: IntelligenceToolRequest) -> MCPToolResult:
        return self._intelligence_tool(
            "get_return_inspection", request,
            self.intelligence.get_inspection, accepts_assessment_at=True,
        )

    def assess_risk(self, request: RiskAssessmentRequest) -> RiskAssessmentResult:
        with bind_execution_context(request.context):
            with logged_operation(
                logger,
                operation_type="mcp_tool",
                operation_name="assess_risk",
                return_id=request.return_id,
                assessment_at=request.assessment_at,
            ):
                if self.risk_engine is None:
                    raise RiskEngineUnavailableError(
                        "A deterministic Risk Engine must be injected before assess_risk can run"
                    )
                return self.risk_engine.assess_risk(request)
