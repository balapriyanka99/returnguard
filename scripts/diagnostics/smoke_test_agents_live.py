#!/usr/bin/env python3
"""Credentialed read-only ADK -> MCP -> frozen BigQuery smoke test."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from returnguard.agents import (
    ADKAgentRuntime,
    AgentConfig,
    AgentExecutionContext,
    CopilotAnswer,
    MCPToolBridge,
    create_investigation_copilot,
)
from returnguard.intelligence import (
    BigQueryIntelligenceRepository,
    IntelligenceConfig,
    ReturnIntelligenceService,
)
from returnguard.mcp import ReturnGuardMCPTools


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--return-id", required=True)
    parser.add_argument("--assessment-at", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--trace-id", default="agent-smoke-trace")
    parser.add_argument("--assessment-id", default="agent-smoke-assessment")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    config = AgentConfig.from_env()
    config.validate_for_live_model()
    context = AgentExecutionContext(
        return_id=args.return_id,
        assessment_at=datetime.fromisoformat(args.assessment_at.replace("Z", "+00:00")),
        trace_id=args.trace_id,
        assessment_id=args.assessment_id,
        request_id=args.assessment_id,
    )
    repository = BigQueryIntelligenceRepository(IntelligenceConfig.from_env())
    intelligence = ReturnIntelligenceService(repository)
    bridge = MCPToolBridge.from_returnguard_tools(ReturnGuardMCPTools(intelligence))
    copilot = create_investigation_copilot(bridge, context, config)
    result = await ADKAgentRuntime(config).run(
        copilot, args.question, context, CopilotAnswer
    )
    print("RETURNGUARD AGENT LIVE SMOKE: PASS")
    print(f"Status: {result.status}")
    print(f"Tools used: {', '.join(result.tools_used) if result.tools_used else 'none'}")


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
