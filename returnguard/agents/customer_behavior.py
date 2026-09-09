"""Bounded Google ADK Customer Behavior Agent."""

from google.adk.agents import LlmAgent

from .config import AgentConfig
from .contracts import AgentExecutionContext, SpecialistResult
from .mcp_bridge import MCPToolBridge
from .prompts import CUSTOMER_INSTRUCTION


ALLOWED_TOOLS = ("get_customer_intelligence", "get_return_behavior")


def create_customer_behavior_agent(
    bridge: MCPToolBridge,
    context: AgentExecutionContext,
    config: AgentConfig | None = None,
    *,
    model=None,
) -> LlmAgent:
    config = config or AgentConfig.from_env()
    return LlmAgent(
        name="customer_behavior_agent",
        description="Interprets grounded customer purchase and return behavior.",
        model=model or config.model,
        instruction=CUSTOMER_INSTRUCTION,
        tools=bridge.adk_tools(context, ALLOWED_TOOLS),
        output_schema=SpecialistResult,
    )
