"""Small, dependency-free application logging helpers for ReturnGuard."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Iterator


_SENSITIVE_TEXT_MARKERS = (
    "expected_serial",
    "expected serial",
    "returned_serial",
    "returned serial",
    "image_uri",
    "image uri",
    "reference_image_uri",
    "reference image uri",
    "linked_user_id",
    "linked user id",
    "linked user",
    "linked account",
    "network_identifier",
    "network identifier",
    "ip_address",
    "ip address",
    "session_id",
    "session id",
    "device_identifier",
    "device identifier",
    "device_id",
    "device id",
    "fraud_labels",
    "fraud labels",
    "scenario_id",
    "scenario id",
    "scenario ",
)
_URI_PATTERN = re.compile(r"(?i)\b(?:gs|https?)://\S+")
_IPV4_PATTERN = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_LONG_HEX_PATTERN = re.compile(r"(?i)\b[0-9a-f]{24,}\b")
_CONTROLLED_SERIAL_PATTERN = re.compile(r"\bRG-[A-Za-z0-9-]+\b")
_PROHIBITED_STRUCTURED_KEYS = frozenset({
    "expected_serial", "returned_serial", "image_uri", "reference_image_uri",
    "linked_user_id", "linked_user_ids", "network_identifier",
    "network_identifiers", "ip_address", "session_id", "device_id",
    "device_identifier", "fraud_labels", "scenario_id", "reason_details",
    "risk_event_id", "economics_event_id", "policy_event_id",
})


def sanitize_readable_text(value: Any) -> str:
    """Conservatively sanitize model-authored text before human-readable logs."""

    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    folded = text.casefold()
    if any(marker in folded for marker in _SENSITIVE_TEXT_MARKERS):
        return "[redacted sensitive content]"
    text = _URI_PATTERN.sub("[redacted uri]", text)
    text = _IPV4_PATTERN.sub("[redacted ip]", text)
    text = _LONG_HEX_PATTERN.sub("[redacted identifier]", text)
    return _CONTROLLED_SERIAL_PATTERN.sub("[redacted serial]", text)


def sanitize_structured_value(value: Any) -> Any:
    """Recursively remove prohibited keys and sanitize model-authored strings."""

    if isinstance(value, dict):
        return {
            str(key): sanitize_structured_value(child)
            for key, child in value.items()
            if str(key).casefold() not in _PROHIBITED_STRUCTURED_KEYS
        }
    if isinstance(value, list):
        return [sanitize_structured_value(child) for child in value]
    if isinstance(value, tuple):
        return [sanitize_structured_value(child) for child in value]
    if isinstance(value, str):
        return sanitize_readable_text(value)
    return value


def _human_log(logger: logging.Logger, lines: list[str]) -> None:
    """Emit an additional readable record without affecting business behavior."""

    try:
        logger.info("\n".join(lines))
    except Exception:  # pragma: no cover - logging must remain non-fatal
        pass


def _append_values(
    lines: list[str],
    label: str,
    values: list[Any],
    *,
    indent: str = "  ",
) -> None:
    lines.append(f"{indent}{label}:")
    if values:
        lines.extend(f"{indent}  - {sanitize_readable_text(value)}" for value in values)
    else:
        lines.append(f"{indent}  none")


_AGENT_DISPLAY_NAMES = {
    "customer_behavior_agent": "Customer Behavior Agent",
    "product_intelligence_agent": "Product Intelligence Agent",
    "inspection_agent": "Inspection Agent",
}


def log_specialist_summary(logger: logging.Logger, result: Any) -> None:
    """Log a validated bounded specialist result without tool payloads."""

    name = getattr(result, "agent_name", "")
    display = _AGENT_DISPLAY_NAMES.get(name)
    if display is None:
        return
    status = getattr(getattr(result, "status", None), "value", result.status)
    lines = [
        f"[AGENT] {display} completed",
        f"  status: {sanitize_readable_text(status)}",
        f"  summary: {sanitize_readable_text(result.summary)}",
    ]
    findings = []
    for finding in result.findings:
        title = sanitize_readable_text(finding.title)
        description = sanitize_readable_text(finding.description)
        findings.append(f"{title} — {description}" if description else title)
    _append_values(lines, "findings", findings)
    _append_values(lines, "limitations", list(result.limitations))
    _append_values(
        lines,
        "tools",
        list(dict.fromkeys(item.tool_name for item in result.tool_evidence)),
    )
    _human_log(logger, lines)


def log_orchestration_summary(logger: logging.Logger, result: Any) -> None:
    """Log deterministic orchestration metadata and its validated synthesis."""

    intent = getattr(getattr(result, "request_intent", None), "value", result.request_intent)
    status = getattr(getattr(result, "status", None), "value", result.status)
    lines = [
        "[ORCHESTRATOR] Investigation completed",
        f"  intent: {sanitize_readable_text(intent)}",
        f"  status: {sanitize_readable_text(status)}",
        f"  summary: {sanitize_readable_text(result.summary)}",
    ]
    findings = [
        f"{finding.title} — {finding.description}"
        for specialist in result.specialist_results
        for finding in specialist.findings
    ]
    _append_values(lines, "findings", findings)
    _append_values(lines, "limitations", list(result.limitations))
    _append_values(lines, "executed specialists", list(result.agents_executed))
    skipped = [f"{name}: {reason}" for name, reason in result.agents_skipped.items()]
    skipped.extend(
        f"{item.capability}: {item.reason}" for item in result.missing_capabilities
    )
    _append_values(lines, "skipped/unavailable specialists", skipped)
    _human_log(logger, lines)


def log_risk_summary(logger: logging.Logger, result: Any) -> None:
    """Log safe deterministic Risk-v1 outputs, never underlying raw evidence."""

    score = "UNDETERMINED" if result.score is None else result.score
    band = getattr(result.band, "value", result.band)
    coverage = getattr(result.coverage, "value", result.coverage)
    lines = [
        "[RISK] Assessment completed",
        f"  score: {score}",
        f"  band: {band}",
        f"  evidence coverage: {coverage}",
        "  group scores:",
    ]
    lines.extend(
        f"    {name}: {contribution:+d}"
        for name, contribution in result.group_scores.items()
    )
    lines.append(f"  product mitigation: {result.product_mitigation:+d}")
    _append_values(
        lines,
        "strongest reasons",
        [f"{reason.code}: {reason.contribution:+d}" for reason in result.reasons],
    )
    _append_values(
        lines,
        "patterns",
        [getattr(pattern, "value", pattern) for pattern in result.patterns],
    )
    _append_values(lines, "limitations", list(result.limitations))
    _human_log(logger, lines)


def log_policy_summary(logger: logging.Logger, result: Any) -> None:
    """Log safe Policy-v1 outputs and its already-normalized economics summary."""

    action = getattr(result.action, "value", result.action)
    economics = result.economics
    lines = [
        "[POLICY] Evaluation completed",
        f"  action: {action}",
        f"  matched rule: {result.matched_rule}",
        f"  policy version: {result.policy_version}",
        (
            "  normalized reason: "
            f"{getattr(result.normalized_reason, 'value', result.normalized_reason)}"
        ),
        f"  return fee: {result.return_fee if result.return_fee is not None else 'none'}",
        "  economics:",
        f"    item value: {economics.current_item_value}",
        f"    reverse logistics: {economics.reverse_logistics_cost}",
        f"    inspection cost: {economics.inspection_cost}",
        f"    recovery value: {economics.recovery_value}",
        "    estimated net return cost: unavailable",
    ]
    if result.fee_reason is not None:
        pricing = getattr(result, "pricing", None)
        if pricing is not None:
            lines.extend([
                "  pricing:",
                f"    base fee: {pricing.pricing_base_fee}",
                f"    raw multiplier: {pricing.pricing_raw_multiplier}",
                f"    sample-size cap: {pricing.pricing_sample_size_cap}",
                f"    capped multiplier: {pricing.pricing_capped_multiplier}",
                f"    positive behavior signals: {pricing.pricing_behavior_signal_count}",
                f"    item-value cap: {pricing.pricing_item_value_cap}",
                (
                    "    product mitigation effect: "
                    f"{getattr(pricing.product_mitigation_effect, 'value', pricing.product_mitigation_effect)}"
                ),
            ])
        _append_values(lines, "fee rationale", list(result.rationale))
    else:
        _append_values(lines, "rationale", list(result.rationale))
    _append_values(lines, "limitations", list(getattr(result, "limitations", [])))
    _human_log(logger, lines)


def log_decision_synthesis_summary(logger: logging.Logger, result: Any) -> None:
    """Log a protected DecisionSynthesisResult without raw model/tool payloads."""

    score = "UNDETERMINED" if result.risk_score is None else result.risk_score
    network = getattr(result, "network_context", None)
    lines = [
        "[DECISION SYNTHESIS]",
        f"  return: {sanitize_readable_text(result.return_id)}",
        f"  assessment: {sanitize_readable_text(result.assessment_id or 'not supplied')}",
        f"  risk: {score} / {getattr(result.risk_band, 'value', result.risk_band)}",
        f"  coverage: {getattr(result.risk_coverage, 'value', result.risk_coverage)}",
        f"  matched policy rule: {sanitize_readable_text(result.matched_policy_rule)}",
        f"  action: {getattr(result.recommended_action, 'value', result.recommended_action)}",
        f"  summary: {sanitize_readable_text(result.decision_summary)}",
    ]
    _append_values(lines, "strongest evidence", list(result.strongest_evidence))
    _append_values(lines, "mitigating context", list(result.mitigating_context))
    if network is not None and network.summary:
        _append_values(lines, "network context", [
            network.summary,
            *([network.merchant_explanation] if network.merchant_explanation else []),
        ])
    economics = result.economics_summary
    _append_values(lines, "economics", [
        f"item value: {economics.current_item_value}",
        f"operational cost: {economics.total_operational_cost}",
        f"recovery value: {economics.recovery_value}",
        f"net return cost: {economics.estimated_net_return_cost}",
    ])
    _append_values(lines, "policy reasoning", list(result.policy_reasoning))
    _append_values(lines, "limitations", list(result.limitations))
    _append_values(lines, "specialists used", list(result.specialists_used))
    _human_log(logger, lines)


@dataclass(frozen=True)
class ExecutionContext:
    """Caller-supplied correlation identifiers for one execution."""

    trace_id: str | None = None
    assessment_id: str | None = None
    request_id: str | None = None


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
        "agent": "agent",
        "risk_engine": "risk",
        "policy_engine": "policy",
    }.get(fields.get("operation_type"), "application")
    values: list[tuple[str, Any]] = [
        ("event", "returnguard_operation"),
        ("layer", layer),
        ("tool", fields.get("tool_name")),
        ("capability", fields.get("capability_name")),
        ("operation", fields.get("repository_operation")),
        ("agent", fields.get("agent_name")),
        ("action", fields.get("action")),
        ("status", event),
        ("success", fields.get("success")),
        ("duration_ms", fields.get("duration_ms")),
        ("return_id", fields.get("return_id")),
        ("assessment_at", fields.get("assessment_at")),
        ("trace_id", fields.get("trace_id")),
        ("assessment_id", fields.get("assessment_id")),
        ("request_id", fields.get("request_id")),
        ("tools_selected", fields.get("tools_selected")),
        ("tool_count", fields.get("tool_count")),
        ("validation_error", fields.get("validation_error")),
        ("source_role", fields.get("source_role")),
        ("data_state", fields.get("data_state")),
        ("evidence_available", fields.get("evidence_available")),
        ("inspection_available", fields.get("inspection_available")),
        ("serial_comparison_performed", fields.get("serial_comparison_performed")),
        ("accessory_comparison_performed", fields.get("accessory_comparison_performed")),
        ("row_count", fields.get("row_count")),
        ("exception_type", fields.get("exception_type")),
    ]

    def safe(key: str, value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (list, tuple, set, frozenset)):
            return "[" + ",".join("_".join(str(item).split())[:80] for item in value) + "]"
        text = str(value)[:160]
        if key in {"action", "validation_error"}:
            return json.dumps(text)
        return "_".join(text.split())

    return " ".join(
        f"{key}={safe(key, value)}" for key, value in values if value is not None
    )


_OPERATION_ACTIONS = {
    ("agent", "investigation_copilot"): "Investigate return using grounded tools",
    ("agent", "customer_behavior_agent"): "Interpret grounded customer behavior",
    ("agent", "product_intelligence_agent"): "Interpret grounded product intelligence",
    ("agent", "inspection_agent"): "Interpret deterministic inspection facts",
    ("agent", "orchestrator_agent"): "Coordinate bounded ReturnGuard specialists",
    ("risk_engine", "risk-v1"): "Calculate deterministic suspiciousness score",
    ("policy_engine", "policy-v1"): "Evaluate deterministic return policy",
    ("mcp_tool", "get_customer_intelligence"): "Fetch deterministic customer intelligence",
    ("mcp_tool", "get_product_intelligence"): "Fetch deterministic product intelligence",
    ("mcp_tool", "get_return_behavior"): "Fetch deterministic return behavior",
    ("mcp_tool", "get_network_intelligence"): "Fetch controlled network context",
    ("mcp_tool", "calculate_return_economics"): "Calculate deterministic return economics",
    ("mcp_tool", "get_return_evidence"): "Fetch return evidence metadata",
    ("mcp_tool", "get_return_inspection"): "Fetch deterministic inspection intelligence",
    ("repository_query", "get_return"): "Load controlled return case",
    ("repository_query", "get_customer_history"): "Load point-in-time customer history",
    ("repository_query", "get_product_history"): "Load point-in-time product history",
    ("repository_query", "get_product_category"): "Load frozen product category",
    ("repository_query", "get_network_links"): "Load controlled network relationships",
    ("repository_query", "get_economics"): "Load deterministic return economics inputs",
    ("repository_query", "get_evidence"): "Load return evidence metadata",
    ("repository_query", "get_inspection"): "Load warehouse inspection facts",
}


def _operation_action(operation_type: str, operation_name: str) -> str:
    action = _OPERATION_ACTIONS.get((operation_type, operation_name))
    if action:
        return action
    return f"Execute ReturnGuard {operation_type.replace('_', ' ')} {operation_name}"


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


def log_action(
    logger: logging.Logger,
    *,
    operation_type: str,
    operation_name: str,
    action: str,
    status: str,
    return_id: str | None = None,
    assessment_at: datetime | None = None,
    tools_selected: list[str] | None = None,
    tool_count: int | None = None,
    validation_error: str | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit one safe structured operational milestone."""

    context = _execution_context.get()
    fields = {
        "operation_type": operation_type,
        "operation_name": operation_name,
        "agent_name": operation_name if operation_type == "agent" else None,
        "action": action,
        "return_id": return_id,
        "assessment_at": _timestamp(assessment_at),
        "trace_id": context.trace_id,
        "assessment_id": context.assessment_id,
        "request_id": context.request_id,
        "tools_selected": tools_selected,
        "tool_count": tool_count,
        "validation_error": validation_error,
    }
    _safe_log(logger, level, event=status, fields=fields)


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
        "agent_name": operation_name if operation_type == "agent" else None,
        "action": _operation_action(operation_type, operation_name),
        "return_id": return_id,
        "assessment_at": _timestamp(assessment_at),
        "trace_id": context.trace_id,
        "assessment_id": context.assessment_id,
        "request_id": context.request_id,
        "source_role": source_role,
    }
    started = perf_counter()
    _safe_log(logger, logging.INFO, event="started", fields={**common, "success": None})
    completion: dict[str, Any] = {}
    try:
        yield completion
    except Exception:
        suppress_exception_trace = bool(completion.get("suppress_exception_trace"))
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
            exception=not suppress_exception_trace,
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
