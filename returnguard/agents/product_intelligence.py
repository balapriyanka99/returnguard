"""Bounded Google ADK Product Intelligence Agent."""

from google.adk.agents import LlmAgent

from .config import AgentConfig
from .contracts import AgentExecutionContext, SpecialistResult
from .mcp_bridge import MCPToolBridge
from .prompts import PRODUCT_INSTRUCTION


ALLOWED_TOOLS = ("get_product_intelligence",)


def create_product_intelligence_agent(
    bridge: MCPToolBridge,
    context: AgentExecutionContext,
    config: AgentConfig | None = None,
    *,
    model=None,
) -> LlmAgent:
    config = config or AgentConfig.from_env()
    return LlmAgent(
        name="product_intelligence_agent",
        description="Interprets grounded product and category return history.",
        model=model or config.model,
        instruction=PRODUCT_INSTRUCTION,
        tools=bridge.adk_tools(context, ALLOWED_TOOLS),
        output_schema=SpecialistResult,
    )
