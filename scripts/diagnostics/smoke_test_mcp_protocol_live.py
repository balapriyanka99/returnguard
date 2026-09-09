#!/usr/bin/env python3
"""Read-only live MCP protocol smoke test against frozen ReturnGuard BigQuery data."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import datetime
from typing import Any

from mcp import Client

from returnguard.intelligence.config import IntelligenceConfig
from returnguard.intelligence.repository import BigQueryIntelligenceRepository
from returnguard.intelligence.service import ReturnIntelligenceService
from returnguard.mcp import ReturnGuardMCPTools, create_mcp_server
from returnguard.observability import ExecutionContext, bind_execution_context


CUSTOMER_RETURN_ID = "RTN-S01-001"
INSPECTION_RETURN_ID = "RTN-M08-001"
TRACE_ID = "smoke-trace-001"
ASSESSMENT_ID = "smoke-assessment-001"
EXPECTED_ORDER_ITEMS = "return-guard-506407.returnguard.source_order_items_snapshot"
EXPECTED_PRODUCTS = "return-guard-506407.returnguard.source_products_snapshot"
EXPECTED_TOOLS = {
    "get_customer_intelligence",
    "get_product_intelligence",
    "get_return_behavior",
    "get_network_intelligence",
    "calculate_return_economics",
    "get_return_evidence",
    "get_return_inspection",
}


class SmokeTestFailure(RuntimeError):
    """A safe smoke-test assertion failure."""


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def require(condition: bool, stage: str) -> None:
    if not condition:
        raise SmokeTestFailure(stage)


def sanitize_error(value: Any) -> str:
    """Retain a short diagnostic while redacting SQL, values, URIs, and secrets."""

    text = " ".join(str(value).split())
    sql = re.search(r"(?i)\b(?:SELECT|WITH|INSERT|UPDATE|DELETE|MERGE)\b", text)
    if sql:
        text = f"{text[:sql.start()].rstrip()} [SQL redacted]".strip()
    text = re.sub(r"(?i)\b(?:gs|https?)://\S+", "[URI redacted]", text)
    text = re.sub(r"@[A-Za-z_][A-Za-z0-9_]*", "[parameter redacted]", text)
    text = re.sub(r"'[^']*'", "'[value redacted]'", text)
    text = re.sub(r'"[^"]*"', '"[value redacted]"', text)
    return (text or "unavailable")[:600]


def failure_context(records: list[logging.LogRecord]) -> tuple[str, str, str, Exception | None]:
    failed = [record for record in records if getattr(record, "event", None) == "failed"]
    repository = next(
        (r for r in failed if getattr(r, "operation_type", None) == "repository_query"), None
    )
    capability = next(
        (r for r in failed if getattr(r, "operation_type", None) == "intelligence_capability"),
        None,
    )
    tool = next(
        (r for r in failed if getattr(r, "operation_type", None) == "mcp_tool"), None
    )
    recorded_exception = None
    for record in (repository, capability, tool):
        if record is not None and record.exc_info:
            recorded_exception = record.exc_info[1]
            break
    return (
        getattr(tool, "tool_name", None) or "unavailable",
        getattr(capability, "capability_name", None) or "unavailable",
        getattr(repository, "repository_operation", None) or "unavailable",
        recorded_exception,
    )


def assessment_at(
    repository: BigQueryIntelligenceRepository,
    return_id: str,
    context: ExecutionContext,
) -> datetime:
    with bind_execution_context(context):
        row = repository.get_return(return_id)
    require(row is not None, f"active return lookup ({return_id})")
    value = row.get("assessment_at")
    require(isinstance(value, datetime), f"stored assessment_at ({return_id})")
    return value


def arguments(return_id: str, value: datetime) -> dict[str, str]:
    return {
        "return_id": return_id,
        "assessment_at": value.isoformat(),
        "trace_id": TRACE_ID,
        "assessment_id": ASSESSMENT_ID,
    }


def structured_result(result: Any, stage: str) -> dict[str, Any]:
    require(not result.is_error, stage)
    payload = result.structured_content
    require(isinstance(payload, dict), stage)
    require(isinstance(payload.get("result"), dict), stage)
    return payload


def verify_safe_logs(
    records: list[logging.LogRecord],
    expected_serial: str,
    returned_serial: str,
) -> None:
    rendered = "\n".join(str(record.__dict__) for record in records)
    require(expected_serial not in rendered, "sensitive logging")
    require(returned_serial not in rendered, "sensitive logging")
    forbidden = (
        "SELECT ", "FROM `", "query_parameters", "expected_serial", "returned_serial",
        "authorization", "credentials", "access_token", "api_key", "service_account",
    )
    lowered = rendered.lower()
    require(all(value.lower() not in lowered for value in forbidden), "sensitive logging")


async def run_smoke_test() -> None:
    config = IntelligenceConfig.from_env()
    require(config.source_order_items == EXPECTED_ORDER_ITEMS, "frozen order_items routing")
    require(config.source_products == EXPECTED_PRODUCTS, "frozen products routing")
    require("bigquery-public-data" not in config.source_order_items, "public source routing")
    require("bigquery-public-data" not in config.source_products, "public source routing")

    context = ExecutionContext(trace_id=TRACE_ID, assessment_id=ASSESSMENT_ID)
    repository = BigQueryIntelligenceRepository(config=config)
    intelligence = ReturnIntelligenceService(repository)
    existing_tools = ReturnGuardMCPTools(intelligence)
    server = create_mcp_server(existing_tools)

    customer_at = assessment_at(repository, CUSTOMER_RETURN_ID, context)
    inspection_at = assessment_at(repository, INSPECTION_RETURN_ID, context)

    handler = CapturingHandler()
    application_logger = logging.getLogger("returnguard")
    previous_level = application_logger.level
    application_logger.setLevel(logging.INFO)
    application_logger.addHandler(handler)
    try:
        async with Client(server, raise_exceptions=True) as client:
            print("MCP client initialization: PASS")

            discovered = await client.list_tools()
            require({tool.name for tool in discovered.tools} == EXPECTED_TOOLS, "MCP list_tools")
            print("MCP list_tools (7/7): PASS")

            customer_call = await client.call_tool(
                "get_customer_intelligence", arguments(CUSTOMER_RETURN_ID, customer_at)
            )
            customer = structured_result(customer_call, "Customer Intelligence")
            require(customer["return_id"] == CUSTOMER_RETURN_ID, "customer return_id")
            require(customer["assessment_at"] == customer_at.isoformat(), "customer assessment_at")
            customer_data = customer["result"]
            require(
                customer_data.get("customer_history_data_origin")
                in {"live_source", "controlled_demo", "insufficient_history"},
                "customer provenance",
            )
            print("Customer Intelligence through MCP: PASS")

            inspection_call = await client.call_tool(
                "get_return_inspection", arguments(INSPECTION_RETURN_ID, inspection_at)
            )
            inspection = structured_result(inspection_call, "Inspection Intelligence")
            require(inspection["return_id"] == INSPECTION_RETURN_ID, "inspection return_id")
            require(
                inspection["assessment_at"] == inspection_at.isoformat(),
                "inspection assessment_at",
            )
            inspection_data = inspection["result"]
            expected_serial = inspection_data.get("expected_serial")
            returned_serial = inspection_data.get("returned_serial")
            require(bool(expected_serial), "inspection expected serial")
            require(bool(returned_serial), "inspection returned serial")
            require(inspection_data.get("serial_mismatch") is True, "inspection serial mismatch")
            require(expected_serial != returned_serial, "inspection serial comparison")
            print("Inspection Intelligence through MCP: PASS")
            print("Controlled serial mismatch: PASS")

        verify_safe_logs(handler.records, expected_serial, returned_serial)
    except Exception as exc:
        tool, capability, operation, recorded = failure_context(handler.records)
        diagnostic = recorded or exc
        print(f"Failing MCP tool: {tool}")
        print(f"Intelligence capability: {capability}")
        print(f"Repository operation: {operation}")
        print(f"Exception: {type(diagnostic).__name__}")
        print(f"Reason: {sanitize_error(diagnostic)}")
        raise
    finally:
        application_logger.removeHandler(handler)
        application_logger.setLevel(previous_level)

    print("Sensitive logging check: PASS")
    print(f"Cases exercised: {CUSTOMER_RETURN_ID}, {INSPECTION_RETURN_ID}")
    print("BigQuery writes: 0")
    print("Gemini calls: 0")


def main() -> int:
    print("RETURNGUARD LIVE MCP PROTOCOL SMOKE TEST")
    print()
    try:
        asyncio.run(run_smoke_test())
    except Exception:
        print()
        print("LIVE MCP CLIENT → SERVER → INTELLIGENCE → FROZEN BIGQUERY: FAIL")
        return 1
    print()
    print("LIVE MCP CLIENT → SERVER → INTELLIGENCE → FROZEN BIGQUERY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
