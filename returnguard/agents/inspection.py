"""Bounded Google ADK Inspection Agent."""

from google.adk.agents import LlmAgent

from .config import AgentConfig
from .contracts import AgentExecutionContext, SpecialistResult
from .mcp_bridge import MCPToolBridge
from .prompts import INSPECTION_INSTRUCTION


ALLOWED_TOOLS = ("get_return_inspection",)


def create_inspection_agent(
    bridge: MCPToolBridge,
    context: AgentExecutionContext,
    config: AgentConfig | None = None,
    *,
    model=None,
) -> LlmAgent:
    config = config or AgentConfig.from_env()
    return LlmAgent(
        name="inspection_agent",
        description="Interprets grounded deterministic warehouse inspection facts.",
        model=model or config.model,
        instruction=INSPECTION_INSTRUCTION,
        tools=bridge.adk_tools(context, ALLOWED_TOOLS),
        output_schema=SpecialistResult,
    )
