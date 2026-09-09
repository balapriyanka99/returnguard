"""Agent-side bridge to ReturnGuard facts through the real MCP protocol."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from google.adk.tools import FunctionTool
from mcp import Client

from returnguard.mcp.server import create_mcp_server

from .contracts import AgentExecutionContext


APPROVED_TOOLS = frozenset({
    "get_customer_intelligence", "get_product_intelligence", "get_return_behavior",
    "get_network_intelligence", "calculate_return_economics",
    "get_return_evidence", "get_return_inspection",
})


class MCPToolBridge:
    """Open an official MCP client session for each bounded agent tool call."""

    def __init__(self, server: Any, client_factory: Callable[..., Client] = Client) -> None:
        self._server = server
        self._client_factory = client_factory

    @classmethod
    def from_returnguard_tools(cls, tools: Any) -> "MCPToolBridge":
        return cls(create_mcp_server(tools))

    async def call(self, tool_name: str, context: AgentExecutionContext) -> dict[str, Any]:
        if tool_name not in APPROVED_TOOLS:
            raise ValueError(f"Tool is not approved for ReturnGuard agents: {tool_name}")
        arguments = {
            "return_id": context.return_id,
            "assessment_at": context.assessment_at.isoformat(),
            "trace_id": context.trace_id,
            "assessment_id": context.assessment_id,
        }
        async with self._client_factory(self._server, raise_exceptions=True) as client:
            result = await client.call_tool(tool_name, arguments)
        if result.is_error or not isinstance(result.structured_content, dict):
            raise RuntimeError(f"MCP tool failed without a structured result: {tool_name}")
        return result.structured_content

    def adk_tools(
        self, context: AgentExecutionContext, allowed_tools: Iterable[str]
    ) -> list[FunctionTool]:
        names = tuple(allowed_tools)
        unknown = set(names) - APPROVED_TOOLS
        if unknown:
            raise ValueError(f"Unapproved ReturnGuard agent tools: {sorted(unknown)}")
        return [FunctionTool(self._bound_callable(name, context)) for name in names]

    def _bound_callable(self, tool_name: str, context: AgentExecutionContext):
        async def invoke() -> dict[str, Any]:
            return await self.call(tool_name, context)

        invoke.__name__ = tool_name
        invoke.__doc__ = f"Retrieve deterministic ReturnGuard facts using {tool_name}."
        return invoke
