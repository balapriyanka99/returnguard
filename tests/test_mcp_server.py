from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from mcp import Client

from returnguard.mcp import (
    MCPToolResult,
    STREAMABLE_HTTP_PATH,
    create_mcp_server,
    create_streamable_http_app,
)


AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)
EXPECTED_TOOLS = {
    "get_customer_intelligence",
    "get_product_intelligence",
    "get_return_behavior",
    "get_network_intelligence",
    "calculate_return_economics",
    "get_return_evidence",
    "get_return_inspection",
}


class FakeReturnGuardMCPTools:
    """Injected existing-tool boundary fake; it is not an Intelligence fake."""

    def __init__(self) -> None:
        self.calls = []

    def _result(self, tool_name, request):
        self.calls.append((tool_name, request))
        return MCPToolResult(
            tool_name=tool_name,
            return_id=request.return_id,
            assessment_at=request.assessment_at,
            trace_id=request.context.trace_id,
            assessment_id=request.context.assessment_id,
            result={"data_origin": "controlled_demo", "structured": True},
        )

    def get_customer_intelligence(self, request):
        return self._result("get_customer_intelligence", request)

    def get_product_intelligence(self, request):
        return self._result("get_product_intelligence", request)

    def get_return_behavior(self, request):
        return self._result("get_return_behavior", request)

    def get_network_intelligence(self, request):
        return self._result("get_network_intelligence", request)

    def calculate_return_economics(self, request):
        return self._result("calculate_return_economics", request)

    def get_return_evidence(self, request):
        return self._result("get_return_evidence", request)

    def get_return_inspection(self, request):
        return self._result("get_return_inspection", request)


class MCPProtocolServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_lists_and_calls_registered_protocol_tool(self):
        backend = FakeReturnGuardMCPTools()
        server = create_mcp_server(backend)

        async with Client(server) as client:
            listed = await client.list_tools()
            self.assertEqual({tool.name for tool in listed.tools}, EXPECTED_TOOLS)
            customer_tool = next(
                tool for tool in listed.tools if tool.name == "get_customer_intelligence"
            )
            self.assertIn("assessment_at", customer_tool.input_schema["properties"])
            self.assertIn("trace_id", customer_tool.input_schema["properties"])
            self.assertIn("assessment_id", customer_tool.input_schema["properties"])

            result = await client.call_tool(
                "get_customer_intelligence",
                {
                    "return_id": "RTN-S01-001",
                    "assessment_at": AT.isoformat(),
                    "trace_id": "protocol-trace-001",
                    "assessment_id": "protocol-assessment-001",
                },
            )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["return_id"], "RTN-S01-001")
        self.assertEqual(result.structured_content["result"]["data_origin"], "controlled_demo")
        self.assertEqual(len(backend.calls), 1)
        tool_name, request = backend.calls[0]
        self.assertEqual(tool_name, "get_customer_intelligence")
        self.assertEqual(request.return_id, "RTN-S01-001")
        self.assertEqual(request.assessment_at, AT)
        self.assertEqual(request.context.trace_id, "protocol-trace-001")
        self.assertEqual(request.context.assessment_id, "protocol-assessment-001")

    async def test_streamable_http_asgi_app_uses_mcp_path(self):
        app = create_streamable_http_app(FakeReturnGuardMCPTools())
        self.assertTrue(any(getattr(route, "path", None) == STREAMABLE_HTTP_PATH for route in app.routes))

    def test_protocol_adapter_has_no_repository_or_bigquery_access(self):
        source = Path("returnguard/mcp/server.py").read_text(encoding="utf-8")
        self.assertNotIn("google.cloud", source)
        self.assertNotIn("BigQuery", source)
        self.assertNotIn("IntelligenceRepository", source)
        self.assertNotIn(".query(", source)
        self.assertNotIn("assess_risk", source)


if __name__ == "__main__":
    unittest.main()
