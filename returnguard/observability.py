"""Small, dependency-free application logging helpers for ReturnGuard."""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Iterator


@dataclass(frozen=True)
class ExecutionContext:
    """Caller-supplied correlation identifiers for one execution."""

    trace_id: str | None = None
    assessment_id: str | None = None


_execution_context: ContextVar[ExecutionContext] = ContextVar(
    "returnguard_execution_context", default=ExecutionContext()
)


@contextmanager
def bind_execution_context(context: ExecutionContext | None) -> Iterator[None]:
    """Bind correlation identifiers for nested MCP/Intelligence operations."""

    token = _execution_context.set(context or ExecutionContext())
    try:
        yield
    finally:
        _execution_context.reset(token)


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _message(event: str, fields: dict[str, Any]) -> str:
    """Render a safe whitelist for consoles using the default message formatter."""

    layer = {
        "mcp_tool": "mcp",
        "intelligence_capability": "intelligence",
        "repository_query": "repository",
    }.get(fields.get("operation_type"), "application")
    values: list[tuple[str, Any]] = [
        ("event", "returnguard_operation"),
        ("layer", layer),
        ("tool", fields.get("tool_name")),
        ("capability", fields.get("capability_name")),
        ("operation", fields.get("repository_operation")),
        ("status", event),
        ("success", fields.get("success")),
        ("duration_ms", fields.get("duration_ms")),
        ("return_id", fields.get("return_id")),
        ("assessment_at", fields.get("assessment_at")),
        ("trace_id", fields.get("trace_id")),
        ("assessment_id", fields.get("assessment_id")),
        ("source_role", fields.get("source_role")),
        ("data_state", fields.get("data_state")),
        ("evidence_available", fields.get("evidence_available")),
        ("inspection_available", fields.get("inspection_available")),
        ("serial_comparison_performed", fields.get("serial_comparison_performed")),
        ("accessory_comparison_performed", fields.get("accessory_comparison_performed")),
        ("row_count", fields.get("row_count")),
        ("exception_type", fields.get("exception_type")),
    ]

    def safe(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return "_".join(str(value).split())[:160]

    return " ".join(f"{key}={safe(value)}" for key, value in values if value is not None)


def _safe_log(
    logger: logging.Logger,
    level: int,
    *,
    event: str,
    fields: dict[str, Any],
    exception: bool = False,
) -> None:
    """Emit a structured record without allowing logging failures to affect work."""

    try:
        message = _message(event, fields)
        if exception:
            logger.exception(message, extra={"event": event, **fields})
        else:
            logger.log(level, message, extra={"event": event, **fields})
    except Exception:  # pragma: no cover - defensive boundary around logging itself
        pass


@contextmanager
def logged_operation(
    logger: logging.Logger,
    *,
    operation_type: str,
    operation_name: str,
    return_id: str | None = None,
    assessment_at: datetime | None = None,
    source_role: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Log lifecycle and duration using safe metadata only.

    The yielded dictionary lets the caller add a few non-sensitive completion
    facts (for example, availability or row count) without logging payloads.
    """

    context = _execution_context.get()
    common = {
        "operation_type": operation_type,
        "operation_name": operation_name,
        "tool_name": operation_name if operation_type == "mcp_tool" else None,
        "capability_name": (
            operation_name if operation_type == "intelligence_capability" else None
        ),
        "repository_operation": (
            operation_name if operation_type == "repository_query" else None
        ),
        "return_id": return_id,
        "assessment_at": _timestamp(assessment_at),
        "trace_id": context.trace_id,
        "assessment_id": context.assessment_id,
        "source_role": source_role,
    }
    started = perf_counter()
    _safe_log(logger, logging.INFO, event="started", fields={**common, "success": None})
    completion: dict[str, Any] = {}
    try:
        yield completion
    except Exception:
        _safe_log(
            logger,
            logging.ERROR,
            event="failed",
            fields={
                **common,
                "success": False,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "exception_type": type(sys.exc_info()[1]).__name__,
            },
            exception=True,
        )
        raise
    else:
        _safe_log(
            logger,
            logging.INFO,
            event="completed",
            fields={
                **common,
                "success": True,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                **completion,
            },
        )
