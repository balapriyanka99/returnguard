"""Minimal Google ADK execution runtime with structured-output validation."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any, TypeVar

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, ValidationError

from returnguard.observability import (
    ExecutionContext,
    bind_execution_context,
    log_action,
    logged_operation,
)

from .config import AgentConfig
from .contracts import AgentExecutionContext, AgentStatus


logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=BaseModel)
EventObserver = Callable[[dict[str, Any]], None]
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _identifier(value: Any) -> str | None:
    text = value if isinstance(value, str) else None
    return text if text and _SAFE_IDENTIFIER.fullmatch(text) else None


def _structured_output_summary(event: Any) -> dict[str, Any]:
    """Extract only contract metadata, never model output values or evidence."""

    raw = getattr(event, "output", None)
    if raw is None and getattr(event, "content", None):
        text = "".join(
            part.text or "" for part in (event.content.parts or []) if part.text
        )
        if text:
            try:
                raw = json.loads(text)
            except (TypeError, ValueError):
                raw = None
    if not isinstance(raw, dict):
        return {}
    fields = set(raw)
    specialist_fields = {
        "agent_name", "return_id", "assessment_at", "status", "summary",
        "findings", "confidence", "tool_evidence",
    }
    orchestration_fields = {
        "request_intent", "return_id", "assessment_at", "status",
        "agents_selected", "agents_executed", "specialist_results",
    }
    summary: dict[str, Any] = {}
    if specialist_fields.issubset(fields):
        summary["output_contract"] = "SpecialistResult"
        summary["findings_count"] = (
            len(raw["findings"]) if isinstance(raw.get("findings"), list) else 0
        )
        summary["tool_evidence_count"] = (
            len(raw["tool_evidence"])
            if isinstance(raw.get("tool_evidence"), list) else 0
        )
        if raw.get("status") in {item.value for item in AgentStatus}:
            summary["status"] = raw["status"]
        if raw.get("confidence") in {"high", "medium", "low", "unavailable"}:
            summary["confidence"] = raw["confidence"]
    elif orchestration_fields.issubset(fields):
        summary["output_contract"] = "OrchestrationResult"
        summary["specialist_result_count"] = (
            len(raw["specialist_results"])
            if isinstance(raw.get("specialist_results"), list) else 0
        )
    return summary


def _safe_event_metadata(event: Any) -> dict[str, Any]:
    """Describe one ADK event without exposing arguments, responses, or content."""

    parts = event.content.parts if event.content and event.content.parts else []
    calls: list[dict[str, str]] = []
    responses: list[str] = []
    for part in parts:
        if part.function_call is not None:
            name = _identifier(part.function_call.name)
            if not name:
                continue
            call = {"name": name}
            if name == "transfer_to_agent":
                args = part.function_call.args
                target = _identifier(
                    args.get("agent_name") if isinstance(args, dict) else None
                )
                if target:
                    call["target_agent"] = target
            calls.append(call)
        if part.function_response is not None:
            name = _identifier(part.function_response.name)
            if name:
                responses.append(name)
    action_target = _identifier(
        getattr(getattr(event, "actions", None), "transfer_to_agent", None)
    )
    if action_target and not any(call.get("target_agent") for call in calls):
        calls.append({"name": "transfer_to_agent", "target_agent": action_target})
    return {
        "author": _identifier(getattr(event, "author", None)),
        "function_calls": calls,
        "function_responses": responses,
        "is_final_response": bool(event.is_final_response()),
        **_structured_output_summary(event),
    }


class ADKAgentRuntime:
    def __init__(self, config: AgentConfig, session_service=None) -> None:
        self.config = config
        self.session_service = session_service or InMemorySessionService()

    async def run(
        self,
        agent: LlmAgent,
        prompt: str,
        context: AgentExecutionContext,
        output_type: type[OutputT],
        *,
        event_observer: EventObserver | None = None,
    ) -> OutputT:
        self.config.validate_for_live_model()
        correlation = ExecutionContext(
            context.trace_id, context.assessment_id, context.request_id
        )
        with bind_execution_context(correlation):
            with logged_operation(
                logger, operation_type="agent", operation_name=agent.name,
                return_id=context.return_id, assessment_at=context.assessment_at,
            ) as log_result:
                session_id = context.request_id or context.assessment_id or context.trace_id
                if not session_id:
                    raise ValueError("request_id, assessment_id, or trace_id is required for an ADK run")
                existing = await self.session_service.get_session(
                    app_name=self.config.app_name, user_id="returnguard-operator",
                    session_id=session_id,
                )
                if existing is None:
                    await self.session_service.create_session(
                        app_name=self.config.app_name, user_id="returnguard-operator",
                        session_id=session_id,
                    )
                runner = Runner(
                    app_name=self.config.app_name, agent=agent,
                    session_service=self.session_service,
                )
                final = None
                selected_tools: list[str] = []
                synthesis_logged = False
                message = types.Content(role="user", parts=[types.Part(text=prompt)])
                log_action(
                    logger,
                    operation_type="agent",
                    operation_name=agent.name,
                    action="Request grounded tool selection from Gemini",
                    status="requested",
                    return_id=context.return_id,
                    assessment_at=context.assessment_at,
                )
                async for event in runner.run_async(
                    user_id="returnguard-operator", session_id=session_id, new_message=message,
                ):
                    if event_observer is not None:
                        try:
                            event_observer(_safe_event_metadata(event))
                        except Exception:
                            # Diagnostics must never alter agent execution.
                            pass
                    parts = event.content.parts if event.content and event.content.parts else []
                    event_tools = [
                        part.function_call.name
                        for part in parts
                        if part.function_call is not None and part.function_call.name
                    ]
                    new_tools = [name for name in event_tools if name not in selected_tools]
                    if new_tools:
                        selected_tools.extend(new_tools)
                        log_action(
                            logger,
                            operation_type="agent",
                            operation_name=agent.name,
                            action="Gemini selected grounded tools",
                            status="selected",
                            return_id=context.return_id,
                            assessment_at=context.assessment_at,
                            tools_selected=selected_tools,
                            tool_count=len(selected_tools),
                        )
                    if not synthesis_logged and any(
                        part.function_response is not None for part in parts
                    ):
                        log_action(
                            logger,
                            operation_type="agent",
                            operation_name=agent.name,
                            action="Synthesize grounded agent response",
                            status="started",
                            return_id=context.return_id,
                            assessment_at=context.assessment_at,
                            tool_count=len(selected_tools),
                        )
                        synthesis_logged = True
                    if event.is_final_response():
                        final = event
                if final is None:
                    raise RuntimeError(f"ADK agent produced no final response: {agent.name}")
                raw = final.output
                if raw is None and final.content and final.content.parts:
                    raw = "".join(part.text or "" for part in final.content.parts)
                if selected_tools and not synthesis_logged:
                    log_action(
                        logger,
                        operation_type="agent",
                        operation_name=agent.name,
                        action="Synthesize grounded agent response",
                        status="started",
                        return_id=context.return_id,
                        assessment_at=context.assessment_at,
                        tool_count=len(selected_tools),
                    )
                try:
                    result = (
                        output_type.model_validate(raw)
                        if isinstance(raw, dict)
                        else output_type.model_validate_json(
                            raw if isinstance(raw, str) else json.dumps(raw)
                        )
                    )
                except ValidationError as exc:
                    # Pydantic exception text may embed rejected model input. Keep
                    # outer lifecycle telemetry, but do not attach that traceback.
                    log_result["suppress_exception_trace"] = True
                    log_action(
                        logger,
                        operation_type="agent",
                        operation_name=agent.name,
                        action="Validate structured agent response",
                        status="failed",
                        return_id=context.return_id,
                        assessment_at=context.assessment_at,
                        validation_error=(
                            f"{exc.error_count()} validation error(s) for "
                            f"{output_type.__name__}"
                        ),
                        level=logging.ERROR,
                    )
                    raise
                log_result["agent_status"] = getattr(result, "status", None)
                return result
