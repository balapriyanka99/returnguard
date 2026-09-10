#!/usr/bin/env python3
"""Read-only live smoke for deterministic Risk-v1 and Policy-v1."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from returnguard.intelligence import (
    BigQueryIntelligenceRepository,
    IntelligenceConfig,
    ReturnIntelligenceService,
)
from returnguard.policy import ReturnPolicyService
from returnguard.risk import RiskBand


PROHIBITED = {
    "fraud_labels", "expected_serial", "returned_serial", "image_uri",
    "reference_image_uri", "network_identifiers", "linked_user_ids",
    "ip_address", "session_id",
}

CORE_RISK_DOMAINS = (
    "CUSTOMER_BEHAVIOR",
    "PRODUCT_CONTEXT",
    "INSPECTION",
    "VISION",
)


class DiagnosticIntelligenceService(ReturnIntelligenceService):
    """Retain safe results already loaded during the normal risk evaluation."""

    diagnostic_customer: Any = None
    diagnostic_return_behavior: Any = None

    def get_customer_intelligence(self, return_id, assessment_at=None):
        result = super().get_customer_intelligence(return_id, assessment_at)
        self.diagnostic_customer = result
        return result

    def get_return_behavior_intelligence(self, return_id, assessment_at=None):
        result = super().get_return_behavior_intelligence(return_id, assessment_at)
        self.diagnostic_return_behavior = result
        return result


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--return-id", default="RTN-M08-002")
    parser.add_argument(
        "--assessment-at", default="2026-09-02T23:59:59+00:00"
    )
    parser.add_argument("--assessment-id", default="risk-policy-smoke-assessment")
    return parser.parse_args(argv)


def _validate_frozen_sources(config: IntelligenceConfig) -> None:
    expected_prefix = f"{config.project_id}.{config.dataset_id}.source_"
    if not config.source_order_items.startswith(expected_prefix):
        raise AssertionError("order_items runtime source is not frozen ReturnGuard data")
    if not config.source_products.startswith(expected_prefix):
        raise AssertionError("products runtime source is not frozen ReturnGuard data")
    if "bigquery-public-data.thelook_ecommerce" in (
        config.source_order_items + config.source_products
    ):
        raise AssertionError("public The Look is configured as a runtime source")


def prohibited_paths(value: Any, path: str = "result") -> list[str]:
    failures = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).casefold() in PROHIBITED:
                failures.append(child_path)
            failures.extend(prohibited_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            failures.extend(prohibited_paths(child, f"{path}[{index}]"))
    return failures


def print_risk_diagnostics(risk: Any) -> None:
    """Print only safe, compact fields from an already validated assessment."""

    print("Risk group scores:")
    for group, contribution in risk.group_scores.items():
        print(f"  {group}: {contribution:+d}")

    print(f"Product mitigation: {risk.product_mitigation:+d}")

    print("Risk reasons:")
    if risk.reasons:
        for reason in risk.reasons:
            print(f"  {reason.code}: {reason.contribution:+d}")
    else:
        print("  none")

    print("Patterns:")
    if risk.patterns:
        for pattern in risk.patterns:
            print(f"  {pattern.value}")
    else:
        print("  none")

    available = set(risk.evaluable_domains)
    print("Evaluable core domains:")
    if available:
        for domain in CORE_RISK_DOMAINS:
            if domain in available:
                print(f"  {domain}")
    else:
        print("  none")

    print("Unavailable core domains:")
    unavailable = [domain for domain in CORE_RISK_DOMAINS if domain not in available]
    if unavailable:
        for domain in unavailable:
            print(f"  {domain}")
    else:
        print("  none")

    print("Limitations:")
    if risk.limitations:
        for limitation in risk.limitations:
            print(f"  {limitation}")
    else:
        print("  none")


def print_customer_return_value_diagnostic(
    customer: Any,
    return_behavior: Any,
) -> None:
    """Print safe aggregate values retained from deterministic Intelligence."""

    print("Customer return-value diagnostic:")
    print(f"  assessment_at: {customer.assessment_at.isoformat()}")
    print(f"  total_items: {customer.total_items}")
    print(f"  historical_return_count: {return_behavior.historical_return_count}")
    print(f"  returned_value: {return_behavior.returned_value}")
    print(f"  purchased_value: {customer.purchased_value}")
    print(f"  total_spend: {customer.total_spend}")
    print(
        "  returned_value_to_purchase_value_ratio: "
        f"{return_behavior.returned_value_to_purchase_value_ratio}"
    )
    print(f"  customer_history_data_origin: {customer.customer_history_data_origin}")
    print("  current_anchor_excluded_by_query: true")


def main() -> int:
    args = parse_args()
    try:
        requested_at = parse_timestamp(args.assessment_at)
        config = IntelligenceConfig.from_env()
        _validate_frozen_sources(config)
        intelligence = DiagnosticIntelligenceService(
            BigQueryIntelligenceRepository(config)
        )
        risk, policy = ReturnPolicyService(intelligence).evaluate(
            args.return_id,
            requested_at,
            assessment_id=args.assessment_id,
        )
        if risk.return_id != args.return_id:
            raise AssertionError("Risk result return_id mismatch")
        if risk.assessment_at != requested_at:
            raise AssertionError("Risk result assessment_at mismatch")
        if risk.assessment_id != args.assessment_id:
            raise AssertionError("Risk result assessment_id mismatch")
        if risk.engine_version != "risk-v1" or policy.policy_version != "policy-v1":
            raise AssertionError("Unexpected Risk/Policy version")
        if (
            policy.return_id != risk.return_id
            or policy.assessment_id != risk.assessment_id
            or policy.assessment_at != risk.assessment_at
        ):
            raise AssertionError("Policy did not preserve risk assessment identity")
        if risk.band == RiskBand.UNDETERMINED and risk.score is not None:
            raise AssertionError("UNDETERMINED risk must not have a numeric score")
        if risk.band != RiskBand.UNDETERMINED and risk.score is None:
            raise AssertionError("Determined risk band must have a numeric score")
        serialized = {
            "risk": risk.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
        }
        if prohibited_paths(serialized):
            raise AssertionError("Prohibited raw fields appeared in structured output")
        if (
            intelligence.diagnostic_customer is None
            or intelligence.diagnostic_return_behavior is None
        ):
            raise AssertionError("Risk evaluation did not produce customer diagnostics")

        print("RETURNGUARD RISK/POLICY LIVE SMOKE: PASS")
        print("Frozen order_items source: PASS")
        print("Frozen products source: PASS")
        print("Configured Intelligence sources:")
        print(f"  order_items: {config.source_order_items}")
        print(f"  products: {config.source_products}")
        print(f"Return: {risk.return_id}")
        print(f"Assessment ID: {risk.assessment_id}")
        print(f"Risk engine: {risk.engine_version}")
        print(f"Risk score: {risk.score if risk.score is not None else 'UNDETERMINED'}")
        print(f"Risk band: {risk.band.value}")
        print(f"Coverage: {risk.coverage.value}")
        print_risk_diagnostics(risk)
        print_customer_return_value_diagnostic(
            intelligence.diagnostic_customer,
            intelligence.diagnostic_return_behavior,
        )
        print(f"Policy: {policy.policy_version}")
        print(f"Policy rule: {policy.matched_rule}")
        print(f"Policy action: {policy.action.value}")
        print("Prohibited-field check: PASS")
        print("BigQuery writes: 0")
        return 0
    except Exception as exc:
        print("RETURNGUARD RISK/POLICY LIVE SMOKE: FAIL")
        print(f"Failure type: {type(exc).__name__}")
        print("BigQuery writes: 0")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
