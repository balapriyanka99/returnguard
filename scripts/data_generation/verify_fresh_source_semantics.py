#!/usr/bin/env python3
"""Read-only verification of fresh ReturnGuard anchors against live The Look.

This deliberately verifies only source-dependent assumptions. Generated-table
structure and controlled scenario fixtures remain the responsibility of the
generator's existing validation.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery

from generate_returnguard_synthetic_enrichment import SCENARIO_CONTRACTS


SOURCE_DATASET = "bigquery-public-data.thelook_ecommerce"
DEFAULT_INPUT_DIR = Path("~/Documents/ReturnGuard_Fresh_2026-09-07").expanduser()
DEFAULT_PROJECT = "return-guard-506407"
SPECIAL_PRODUCT_SCENARIOS = frozenset({"S12", "S12V2", "M02", "M03", "M05", "M06", "M08"})


@dataclass(frozen=True)
class Case:
    return_id: str
    scenario_id: str
    order_item_id: int
    order_id: int
    user_id: int
    product_id: int
    assessment_at: datetime
    anchor_sale_price: float
    controlled_customer_return_count: int | None


@dataclass
class CaseResult:
    case: Case
    anchor_match: bool
    source_contract_pass: bool
    customer_items: int
    customer_returns: int
    customer_return_rate: float | None
    product_items: int
    product_returns: int
    product_return_rate: float | None
    customer_history_origin: str
    source_requirement: str
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.anchor_match and self.source_contract_pass


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def optional_int(value: str | None) -> int | None:
    return None if value is None or value.strip() == "" else int(float(value))


def read_cases(path: Path) -> list[Case]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [
        Case(
            return_id=row["return_id"],
            scenario_id=row["scenario_id"],
            order_item_id=int(row["order_item_id"]),
            order_id=int(row["order_id"]),
            user_id=int(row["user_id"]),
            product_id=int(row["product_id"]),
            assessment_at=parse_timestamp(row["assessment_at"]),
            anchor_sale_price=float(row["anchor_sale_price"]),
            controlled_customer_return_count=optional_int(row.get("customer_historical_return_count")),
        )
        for row in rows
    ]


def query_relevant_order_items(
    client: bigquery.Client,
    cases: list[Case],
) -> list[dict[str, Any]]:
    """Fetch the union needed for anchors, customer history, and product history."""
    sql = f"""
    SELECT id, order_id, user_id, product_id, status, created_at, returned_at,
           CAST(sale_price AS FLOAT64) AS sale_price
    FROM `{SOURCE_DATASET}.order_items`
    WHERE created_at <= @max_assessment_at
      AND (
        id IN UNNEST(@anchor_ids)
        OR user_id IN UNNEST(@user_ids)
        OR product_id IN UNNEST(@product_ids)
      )
    """
    parameters = [
        bigquery.ScalarQueryParameter(
            "max_assessment_at", "TIMESTAMP", max(case.assessment_at for case in cases)
        ),
        bigquery.ArrayQueryParameter("anchor_ids", "INT64", sorted({case.order_item_id for case in cases})),
        bigquery.ArrayQueryParameter("user_ids", "INT64", sorted({case.user_id for case in cases})),
        bigquery.ArrayQueryParameter("product_ids", "INT64", sorted({case.product_id for case in cases})),
    ]
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters))
    return [dict(row.items()) for row in job.result()]


def query_network_pool_users(
    client: bigquery.Client,
    assessment_at: datetime,
) -> tuple[int, int | None]:
    """Return enough facts to establish whether a distinct attributable peer exists."""
    sql = f"""
    SELECT COUNT(DISTINCT user_id) AS user_count, MIN(user_id) AS only_or_min_user_id
    FROM `{SOURCE_DATASET}.events`
    WHERE user_id IS NOT NULL
      AND ip_address IS NOT NULL
      AND created_at <= @assessment_at
    """
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment_at)
    ])
    row = next(iter(client.query(sql, job_config=config).result()))
    return int(row["user_count"]), (None if row["only_or_min_user_id"] is None else int(row["only_or_min_user_id"]))


def is_cancelled(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").lower() == "cancelled"


def returned_by(row: dict[str, Any], assessment_at: datetime) -> bool:
    returned_at = row.get("returned_at")
    return (
        str(row.get("status") or "").lower() == "returned"
        and returned_at is not None
        and returned_at <= assessment_at
    )


def history_metrics(rows: list[dict[str, Any]], assessment_at: datetime) -> tuple[int, int, float | None]:
    eligible = [
        row for row in rows
        if row["created_at"] <= assessment_at and not is_cancelled(row)
    ]
    returned = sum(returned_by(row, assessment_at) for row in eligible)
    return len(eligible), returned, (returned / len(eligible) if eligible else None)


def network_peer_available(
    network_fact: tuple[int, int | None],
    current_user_id: int,
) -> bool:
    user_count, only_or_min_user = network_fact
    return user_count >= 2 or (user_count == 1 and only_or_min_user != current_user_id)


def format_rate(value: float | None) -> str:
    return "NULL" if value is None else f"{value:.4f}"


def verify(
    cases: list[Case],
    source_rows: list[dict[str, Any]],
    network_facts: dict[datetime, tuple[int, int | None]],
) -> list[CaseResult]:
    anchors = {int(row["id"]): row for row in source_rows}
    by_user: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_product: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        if row.get("user_id") is not None:
            by_user[int(row["user_id"])].append(row)
        if row.get("product_id") is not None:
            by_product[int(row["product_id"])].append(row)

    results = []
    for case in cases:
        contract = SCENARIO_CONTRACTS.get(case.scenario_id)
        failures: list[str] = []
        anchor = anchors.get(case.order_item_id)
        anchor_match = anchor is not None
        if anchor is None:
            failures.append(f"anchor order_item_id {case.order_item_id} not found by assessment_at")
        else:
            if anchor["created_at"] > case.assessment_at:
                anchor_match = False
                failures.append(
                    f"anchor created_at={anchor['created_at'].isoformat()} is after assessment_at"
                )
            expected_tuple = (case.order_item_id, case.order_id, case.user_id, case.product_id)
            observed_tuple = (
                int(anchor["id"]), int(anchor["order_id"]),
                int(anchor["user_id"]), int(anchor["product_id"]),
            )
            if observed_tuple != expected_tuple:
                anchor_match = False
                failures.append(f"anchor tuple expected={expected_tuple} observed={observed_tuple}")
            if not math.isclose(float(anchor["sale_price"]), case.anchor_sale_price, rel_tol=1e-9, abs_tol=1e-6):
                anchor_match = False
                failures.append(
                    f"anchor sale_price expected={case.anchor_sale_price:.6f} observed={float(anchor['sale_price']):.6f}"
                )

        customer_rows = [
            row for row in by_user.get(case.user_id, [])
            if int(row["id"]) != case.order_item_id
        ]
        product_rows = [
            row for row in by_product.get(case.product_id, [])
            if int(row["id"]) != case.order_item_id
        ]
        customer_items, customer_returns, customer_rate = history_metrics(customer_rows, case.assessment_at)
        product_items, product_returns, product_rate = history_metrics(product_rows, case.assessment_at)

        if contract is None:
            failures.append(f"unknown scenario contract {case.scenario_id}")
            requirements = ["contract missing"]
        else:
            requirements = []
            anchor_requirements = contract.anchor_requirements
            if "min_price" in anchor_requirements:
                threshold = float(anchor_requirements["min_price"])
                requirements.append(f"source sale_price >= {threshold:.2f}")
                if anchor is None or float(anchor["sale_price"]) < threshold:
                    observed_price = "MISSING" if anchor is None else f"{float(anchor['sale_price']):.2f}"
                    failures.append(
                        f"source sale_price {observed_price} < {threshold:.2f}"
                    )
            if anchor_requirements.get("product_with_return_history"):
                requirements.append("source product returned_items >= 1")
                if product_returns < 1:
                    failures.append(f"source product returned_items={product_returns}, expected >= 1")
            if anchor_requirements.get("network_pool_required"):
                requirements.append("live attributable network peer available")
                if not network_peer_available(network_facts[case.assessment_at], case.user_id):
                    failures.append("no distinct live attributable network-pool peer by assessment_at")
            if "min_historical_returns" in anchor_requirements:
                threshold = int(anchor_requirements["min_historical_returns"])
                if "customer_historical_return_count" in contract.controlled_signals:
                    requirements.append(f"customer returns >= {threshold} (CONTROLLED; live not required)")
                else:
                    requirements.append(f"source customer returned_items >= {threshold}")
                    if customer_returns < threshold:
                        failures.append(f"source customer returned_items={customer_returns}, expected >= {threshold}")
            if anchor_requirements.get("requires_accessories"):
                requirements.append("accessories fixture (CONTROLLED/generated; not a The Look fact)")

        controlled_customer = bool(
            contract and "customer_historical_return_count" in contract.controlled_signals
        )
        results.append(CaseResult(
            case=case,
            anchor_match=anchor_match,
            source_contract_pass=not any(
                failure for failure in failures if not failure.startswith("anchor ")
            ),
            customer_items=customer_items,
            customer_returns=customer_returns,
            customer_return_rate=customer_rate,
            product_items=product_items,
            product_returns=product_returns,
            product_return_rate=product_rate,
            customer_history_origin="CONTROLLED" if controlled_customer else "SOURCE",
            source_requirement="; ".join(requirements) if requirements else "anchor identity only",
            failures=failures,
        ))
    return results


def print_report(results: list[CaseResult]) -> bool:
    grouped: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        grouped[result.case.scenario_id].append(result)

    print("Scenario | Cases | Anchor Match | Source Contract | Result")
    print("--- | ---: | ---: | ---: | ---")
    scenario_order = [scenario for scenario in SCENARIO_CONTRACTS if scenario in grouped]
    for scenario in scenario_order:
        rows = grouped[scenario]
        anchors = sum(row.anchor_match for row in rows)
        contracts = sum(row.source_contract_pass for row in rows)
        passed = all(row.passed for row in rows)
        print(f"{scenario} | {len(rows)} | {anchors}/{len(rows)} | {contracts}/{len(rows)} | {'PASS' if passed else 'FAIL'}")

    failures = [result for result in results if not result.passed]
    if failures:
        print("\nFAILURE DETAILS")
        print("return_id | scenario | anchor | customer history | product history | failed condition")
        print("--- | --- | --- | --- | --- | ---")
        for result in failures:
            customer = (
                f"items={result.customer_items}, returned={result.customer_returns}, "
                f"rate={format_rate(result.customer_return_rate)}, origin={result.customer_history_origin}"
            )
            product = (
                f"product_id={result.case.product_id}, items={result.product_items}, "
                f"returned={result.product_returns}, rate={format_rate(result.product_return_rate)}"
            )
            print(
                f"{result.case.return_id} | {result.case.scenario_id} | "
                f"{'MATCH' if result.anchor_match else 'MISMATCH'} | {customer} | {product} | "
                f"{' ; '.join(result.failures)}"
            )

    print("\nSPECIAL PRODUCT-HISTORY DETAILS")
    print("return_id | scenario | product_id | observed_items | returned_items | return_rate | requirement | result")
    print("--- | --- | ---: | ---: | ---: | ---: | --- | ---")
    for result in results:
        if result.case.scenario_id in SPECIAL_PRODUCT_SCENARIOS:
            print(
                f"{result.case.return_id} | {result.case.scenario_id} | {result.case.product_id} | "
                f"{result.product_items} | {result.product_returns} | {format_rate(result.product_return_rate)} | "
                f"{result.source_requirement} | {'PASS' if result.passed else 'FAIL'}"
            )

    passed = not failures
    print(f"\nFRESH SOURCE SEMANTICS: {'PASS' if passed else 'FAIL'}")
    return passed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument(
        "--project-id",
        default=os.getenv("RETURNGUARD_GCP_PROJECT", DEFAULT_PROJECT),
        help="Billing project for read-only BigQuery queries",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_path = args.input_dir.expanduser() / "return_requests.csv"
    try:
        cases = read_cases(request_path)
        if not cases:
            raise ValueError(f"No cases found in {request_path}")
        client = bigquery.Client(project=args.project_id)
        source_rows = query_relevant_order_items(client, cases)
        network_assessments = {
            case.assessment_at
            for case in cases
            if SCENARIO_CONTRACTS[case.scenario_id].anchor_requirements.get("network_pool_required")
        }
        network_facts = {
            assessment_at: query_network_pool_users(client, assessment_at)
            for assessment_at in network_assessments
        }
        return 0 if print_report(verify(cases, source_rows, network_facts)) else 1
    except Exception as exc:
        print(f"Verification error: {exc}", file=sys.stderr)
        print("\nFRESH SOURCE SEMANTICS: FAIL")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
