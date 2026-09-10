#!/usr/bin/env python3
"""Explicit write/read smoke for immutable economics and policy assessments."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from returnguard.economics import BigQueryEconomicsAssessmentRepository
from returnguard.intelligence import (
    BigQueryIntelligenceRepository,
    IntelligenceConfig,
    ReturnIntelligenceService,
)
from returnguard.policy import (
    BigQueryPolicyAssessmentRepository,
    ReturnPolicyService,
    policy_event_id,
)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--return-id", default="RTN-M05-002")
    parser.add_argument(
        "--assessment-at", default="2026-09-02T23:59:59+00:00"
    )
    parser.add_argument("--assessment-id", default="economics-policy-persistence-smoke")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Required acknowledgement that two append-only MERGE jobs will run.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    if not args.persist:
        print("PERSISTENCE SMOKE: REFUSED — pass --persist to authorize two writes")
        return 2

    try:
        config = IntelligenceConfig.from_env()
        client_repository = BigQueryIntelligenceRepository(config)
        economics_repository = BigQueryEconomicsAssessmentRepository(config)
        policy_repository = BigQueryPolicyAssessmentRepository(config)
        service = ReturnPolicyService(
            ReturnIntelligenceService(client_repository),
            economics_repository=economics_repository,
            policy_repository=policy_repository,
        )
        _, policy, economics_assessment = service.evaluate_and_record(
            args.return_id,
            timestamp(args.assessment_at),
            assessment_id=args.assessment_id,
        )

        economics_row = economics_repository.get(
            economics_assessment.economics_event_id
        )
        policy_row = policy_repository.get(policy_event_id(policy))
        if economics_row is None:
            raise AssertionError("Economics assessment was not readable after persistence")
        if policy_row is None:
            raise AssertionError("Policy assessment was not readable after persistence")
        if economics_row.get("return_id") != args.return_id:
            raise AssertionError("Economics assessment return identity mismatch")
        if policy_row.get("return_id") != args.return_id:
            raise AssertionError("Policy assessment return identity mismatch")

        print("RETURNGUARD ECONOMICS/POLICY PERSISTENCE SMOKE: PASS")
        print(f"Return: {args.return_id}")
        print(f"Assessment ID: {args.assessment_id}")
        print("Economics append/read: PASS")
        print("Policy append/read: PASS")
        print("Insert-only MERGE writes: 2")
        return 0
    except Exception as exc:
        print("RETURNGUARD ECONOMICS/POLICY PERSISTENCE SMOKE: FAIL")
        print(f"Failure type: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
