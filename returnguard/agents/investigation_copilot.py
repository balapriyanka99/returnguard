"""Merchant-facing Google ADK Investigation Copilot."""

from google.adk.agents import LlmAgent

from .config import AgentConfig
from .contracts import AgentExecutionContext, CopilotAnswer
from .mcp_bridge import APPROVED_TOOLS, MCPToolBridge
from .prompts import COPILOT_INSTRUCTION


def create_investigation_copilot(
    bridge: MCPToolBridge,
    context: AgentExecutionContext,
    config: AgentConfig | None = None,
    *,
    model=None,
) -> LlmAgent:
    config = config or AgentConfig.from_env()
    return LlmAgent(
        name="investigation_copilot",
        description="Answers merchant questions by dynamically selecting grounded ReturnGuard tools.",
        model=model or config.model,
        instruction=COPILOT_INSTRUCTION,
        tools=bridge.adk_tools(context, sorted(APPROVED_TOOLS)),
        output_schema=CopilotAnswer,
    )
