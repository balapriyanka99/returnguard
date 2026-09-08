"""Official MCP protocol server adapter for ReturnGuard Intelligence tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from mcp.server import MCPServer

from returnguard.observability import ExecutionContext

from .contracts import IntelligenceToolRequest, MCPToolResult
from .tools import ReturnGuardMCPTools


SERVER_NAME = "returnguard-intelligence"
SERVER_TITLE = "ReturnGuard Intelligence MCP Server"
SERVER_VERSION = "1.0.0"
STREAMABLE_HTTP_PATH = "/mcp"


async def _delegate(
    operation: Callable[[IntelligenceToolRequest], MCPToolResult],
    *,
    return_id: str,
    assessment_at: datetime | None,
    trace_id: str | None,
    assessment_id: str | None,
) -> dict[str, Any]:
    request = IntelligenceToolRequest(
        return_id=return_id,
        assessment_at=assessment_at,
        context=ExecutionContext(trace_id=trace_id, assessment_id=assessment_id),
    )
    result = operation(request)
    return result.to_dict()


def create_mcp_server(tools: ReturnGuardMCPTools) -> MCPServer:
    """Register seven protocol tools around an injected existing tool backend."""

    server = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        version=SERVER_VERSION,
        description=(
            "Deterministic ReturnGuard customer, product, behavior, network, "
            "economics, evidence, and inspection intelligence."
        ),
    )

    @server.tool(
        name="get_customer_intelligence",
        description=(
            "Retrieve point-in-time customer historical purchase and return intelligence "
            "for a ReturnGuard case. Missing history remains insufficient evidence and "
            "must not be interpreted as suspicious."
        ),
        structured_output=True,
    )
    async def get_customer_intelligence(
        return_id: str,
        assessment_at: datetime | None = None,
        trace_id: str | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return await _delegate(
            tools.get_customer_intelligence,
            return_id=return_id, assessment_at=assessment_at,
            trace_id=trace_id, assessment_id=assessment_id,
        )

    @server.tool(
        name="get_product_intelligence",
        description=(
            "Retrieve point-in-time product and category return intelligence, including "
            "historical behavior and product-to-category comparison where supported."
        ),
        structured_output=True,
    )
    async def get_product_intelligence(
        return_id: str,
        assessment_at: datetime | None = None,
        trace_id: str | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return await _delegate(
            tools.get_product_intelligence,
            return_id=return_id, assessment_at=assessment_at,
            trace_id=trace_id, assessment_id=assessment_id,
        )

    @server.tool(
        name="get_return_behavior",
        description=(
            "Retrieve point-in-time historical return-behavior signals for a ReturnGuard "
            "case while preserving controlled, live, and insufficient-history provenance."
        ),
        structured_output=True,
    )
    async def get_return_behavior(
        return_id: str,
        assessment_at: datetime | None = None,
        trace_id: str | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return await _delegate(
            tools.get_return_behavior,
            return_id=return_id, assessment_at=assessment_at,
            trace_id=trace_id, assessment_id=assessment_id,
        )

    @server.tool(
        name="get_network_intelligence",
        description=(
            "Retrieve controlled ReturnGuard relationship and shared-IP-cluster context. "
            "This is contextual synthetic evidence, not proof that a linked user or return "
            "is fraudulent or suspicious, and it is not live source-event evidence."
        ),
        structured_output=True,
    )
    async def get_network_intelligence(
        return_id: str,
        assessment_at: datetime | None = None,
        trace_id: str | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return await _delegate(
            tools.get_network_intelligence,
            return_id=return_id, assessment_at=assessment_at,
            trace_id=trace_id, assessment_id=assessment_id,
        )

    @server.tool(
        name="calculate_return_economics",
        description=(
            "Return deterministic ReturnGuard economics and cost intelligence for a case; "
            "this tool does not make a policy or risk decision."
        ),
        structured_output=True,
    )
    async def calculate_return_economics(
        return_id: str,
        assessment_at: datetime | None = None,
        trace_id: str | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return await _delegate(
            tools.calculate_return_economics,
            return_id=return_id, assessment_at=assessment_at,
            trace_id=trace_id, assessment_id=assessment_id,
        )

    @server.tool(
        name="get_return_evidence",
        description=(
            "Retrieve stored evidence metadata, URI availability, and context for a return. "
            "This retrieval tool performs no visual or image analysis."
        ),
        structured_output=True,
    )
    async def get_return_evidence(
        return_id: str,
        assessment_at: datetime | None = None,
        trace_id: str | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return await _delegate(
            tools.get_return_evidence,
            return_id=return_id, assessment_at=assessment_at,
            trace_id=trace_id, assessment_id=assessment_id,
        )

    @server.tool(
        name="get_return_inspection",
        description=(
            "Retrieve deterministic warehouse inspection intelligence, including available "
            "item-presence, condition, accessory, weight, and serial-comparison indicators."
        ),
        structured_output=True,
    )
    async def get_return_inspection(
        return_id: str,
        assessment_at: datetime | None = None,
        trace_id: str | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return await _delegate(
            tools.get_return_inspection,
            return_id=return_id, assessment_at=assessment_at,
            trace_id=trace_id, assessment_id=assessment_id,
        )

    return server


def create_streamable_http_app(
    tools: ReturnGuardMCPTools,
    *,
    path: str = STREAMABLE_HTTP_PATH,
):
    """Create an ASGI-compatible Streamable HTTP application without serving it."""

    return create_mcp_server(tools).streamable_http_app(streamable_http_path=path)
