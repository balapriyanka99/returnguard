#!/usr/bin/env python3
"""Freeze bounded The Look source data for verified ReturnGuard demo cases."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from google.cloud import bigquery

from verify_fresh_source_semantics import DEFAULT_PROJECT, SOURCE_DATASET, Case, read_cases


DEFAULT_INPUT_DIR = Path("~/Documents/ReturnGuard_ExpectedSerial_Candidate_2026-09-07").expanduser()
ORDER_ITEM_FIELDS = (
    "id", "order_id", "user_id", "product_id", "inventory_item_id", "status",
    "sale_price", "created_at", "shipped_at", "delivered_at", "returned_at",
)
PRODUCT_FIELDS = (
    "id", "cost", "category", "name", "brand", "retail_price", "department",
    "sku", "distribution_center_id",
)
USER_FIELDS = (
    "id", "created_at", "age", "gender", "city", "state", "country",
    "postal_code", "traffic_source",
)
ORDER_FIELDS = (
    "order_id", "user_id", "status", "created_at", "shipped_at",
    "delivered_at", "returned_at", "num_of_item",
)
EVENT_FIELDS = (
    "id", "user_id", "session_id", "sequence_number", "created_at", "ip_address",
    "city", "state", "postal_code", "browser", "traffic_source", "uri", "event_type",
)


def query_rows(
    client: bigquery.Client,
    sql: str,
    parameters: list[bigquery.ArrayQueryParameter | bigquery.ScalarQueryParameter],
) -> list[dict[str, Any]]:
    config = bigquery.QueryJobConfig(query_parameters=parameters)
    return [dict(row.items()) for row in client.query(sql, job_config=config).result()]


def query_order_items(client: bigquery.Client, cases: list[Case]) -> list[dict[str, Any]]:
    """Capture anchor, customer, product, and full category-history rows."""
    sql = f"""
    WITH case_categories AS (
      SELECT DISTINCT category
      FROM `{SOURCE_DATASET}.products`
      WHERE id IN UNNEST(@case_product_ids)
        AND category IS NOT NULL
    )
    SELECT oi.id, oi.order_id, oi.user_id, oi.product_id, oi.inventory_item_id,
           oi.status, oi.sale_price, oi.created_at, oi.shipped_at,
           oi.delivered_at, oi.returned_at
    FROM `{SOURCE_DATASET}.order_items` oi
    LEFT JOIN `{SOURCE_DATASET}.products` p ON p.id = oi.product_id
    WHERE oi.created_at <= @max_assessment_at
      AND (
        oi.id IN UNNEST(@anchor_ids)
        OR oi.user_id IN UNNEST(@case_user_ids)
        OR oi.product_id IN UNNEST(@case_product_ids)
        OR p.category IN (SELECT category FROM case_categories)
      )
    """
    return query_rows(client, sql, [
        bigquery.ScalarQueryParameter(
            "max_assessment_at", "TIMESTAMP", max(case.assessment_at for case in cases)
        ),
        bigquery.ArrayQueryParameter("anchor_ids", "INT64", sorted({case.order_item_id for case in cases})),
        bigquery.ArrayQueryParameter("case_user_ids", "INT64", sorted({case.user_id for case in cases})),
        bigquery.ArrayQueryParameter("case_product_ids", "INT64", sorted({case.product_id for case in cases})),
    ])


def query_products(client: bigquery.Client, product_ids: list[int]) -> list[dict[str, Any]]:
    sql = f"""
    SELECT id, cost, category, name, brand, retail_price, department, sku,
           distribution_center_id
    FROM `{SOURCE_DATASET}.products`
    WHERE id IN UNNEST(@product_ids)
    """
    return query_rows(client, sql, [
        bigquery.ArrayQueryParameter("product_ids", "INT64", product_ids)
    ])


def query_users(client: bigquery.Client, user_ids: list[int]) -> list[dict[str, Any]]:
    sql = f"""
    SELECT id, created_at, age, gender, city, state, country, postal_code,
           traffic_source
    FROM `{SOURCE_DATASET}.users`
    WHERE id IN UNNEST(@user_ids)
    """
    return query_rows(client, sql, [
        bigquery.ArrayQueryParameter("user_ids", "INT64", user_ids)
    ])


def query_orders(client: bigquery.Client, order_ids: list[int]) -> list[dict[str, Any]]:
    sql = f"""
    SELECT order_id, user_id, status, created_at, shipped_at, delivered_at,
           returned_at, num_of_item
    FROM `{SOURCE_DATASET}.orders`
    WHERE order_id IN UNNEST(@order_ids)
    """
    return query_rows(client, sql, [
        bigquery.ArrayQueryParameter("order_ids", "INT64", order_ids)
    ])


def query_events(
    client: bigquery.Client,
    user_ids: list[int],
    max_assessment_at: datetime,
) -> list[dict[str, Any]]:
    sql = f"""
    SELECT id, user_id, session_id, sequence_number, created_at, ip_address,
           city, state, postal_code, browser, traffic_source, uri, event_type
    FROM `{SOURCE_DATASET}.events`
    WHERE user_id IN UNNEST(@network_user_ids)
      AND created_at <= @max_assessment_at
    """
    return query_rows(client, sql, [
        bigquery.ArrayQueryParameter("network_user_ids", "INT64", user_ids),
        bigquery.ScalarQueryParameter("max_assessment_at", "TIMESTAMP", max_assessment_at),
    ])


def read_network_users(input_dir: Path) -> set[int]:
    path = input_dir / "synthetic_network_links.csv"
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        return {
            int(value)
            for row in rows
            for value in (row.get("user_id"), row.get("linked_user_id"))
            if value not in (None, "")
        }


def csv_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> int:
    materialized = list(rows)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in materialized:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})
    return len(materialized)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_generation_metadata(input_dir: Path) -> dict[str, Any]:
    path = input_dir / "generation_manifest.json"
    if not path.exists():
        return {"generator_version": None, "generator_seed": None}
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    return {
        "generator_version": manifest.get("generator_version"),
        "generator_seed": manifest.get("seed"),
        "inspection_contract_version": manifest.get("inspection_contract_version"),
    }


def verify_integrity(
    cases: list[Case],
    order_items: list[dict[str, Any]],
    products: list[dict[str, Any]],
    users: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    network_user_ids: set[int],
) -> dict[str, Any]:
    item_ids = [int(row["id"]) for row in order_items]
    items_by_id = {int(row["id"]): row for row in order_items}
    anchors_present = 0
    anchor_matches = 0
    for case in cases:
        anchor = items_by_id.get(case.order_item_id)
        if anchor is None:
            continue
        anchors_present += 1
        tuple_matches = (
            int(anchor["order_id"]), int(anchor["user_id"]), int(anchor["product_id"])
        ) == (case.order_id, case.user_id, case.product_id)
        price_matches = math.isclose(
            float(anchor["sale_price"]), case.anchor_sale_price,
            rel_tol=1e-9, abs_tol=1e-6,
        )
        anchor_matches += tuple_matches and price_matches

    referenced_products = {int(row["product_id"]) for row in order_items if row.get("product_id") is not None}
    referenced_users = {int(row["user_id"]) for row in order_items if row.get("user_id") is not None}
    referenced_orders = {int(row["order_id"]) for row in order_items if row.get("order_id") is not None}
    captured_products = {int(row["id"]) for row in products}
    captured_users = {int(row["id"]) for row in users}
    captured_orders = {int(row["order_id"]) for row in orders}
    missing_products = sorted(referenced_products - captured_products)
    missing_users = sorted((referenced_users | network_user_ids) - captured_users)
    missing_orders = sorted(referenced_orders - captured_orders)
    duplicate_items = len(item_ids) - len(set(item_ids))
    history_fields = {"id", "order_id", "user_id", "product_id", "status", "sale_price", "created_at", "returned_at"}
    history_reproducible = all(
        history_fields.issubset(row)
        and row["created_at"] is not None
        and row["created_at"] <= max(case.assessment_at for case in cases)
        for row in order_items
    )
    current_exclusion_reproducible = len({case.order_item_id for case in cases}) == len(cases)
    passed = (
        len(cases) == anchors_present == anchor_matches
        and duplicate_items == 0
        and not missing_products
        and not missing_users
        and not missing_orders
        and history_reproducible
        and current_exclusion_reproducible
    )
    return {
        "case_count": len(cases),
        "anchors_present": anchors_present,
        "anchor_tuple_and_price_matches": anchor_matches,
        "duplicate_order_item_ids": duplicate_items,
        "missing_product_ids": missing_products,
        "missing_user_ids": missing_users,
        "missing_order_ids": missing_orders,
        "current_case_exclusion_reproducible": current_exclusion_reproducible,
        "point_in_time_history_reproducible": history_reproducible,
        "status": "PASS" if passed else "FAIL",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--project-id", default=os.getenv("RETURNGUARD_GCP_PROJECT", DEFAULT_PROJECT),
        help="Billing project for five read-only BigQuery queries",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    snapshot_dir = output_dir / "source_snapshot"
    if not input_dir.is_dir():
        raise ValueError(f"Working dataset does not exist: {input_dir}")
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing output directory: {output_dir}")

    cases = read_cases(input_dir / "return_requests.csv")
    if not cases:
        raise ValueError("return_requests.csv contains no cases")
    max_assessment_at = max(case.assessment_at for case in cases)
    client = bigquery.Client(project=args.project_id)

    order_items = sorted(query_order_items(client, cases), key=lambda row: int(row["id"]))
    product_ids = sorted({int(row["product_id"]) for row in order_items if row.get("product_id") is not None})
    order_user_ids = {int(row["user_id"]) for row in order_items if row.get("user_id") is not None}
    order_ids = sorted({int(row["order_id"]) for row in order_items if row.get("order_id") is not None})
    network_user_ids = read_network_users(input_dir) | {case.user_id for case in cases}
    all_user_ids = sorted(order_user_ids | network_user_ids)

    products = sorted(query_products(client, product_ids), key=lambda row: int(row["id"]))
    users = sorted(query_users(client, all_user_ids), key=lambda row: int(row["id"]))
    orders = sorted(query_orders(client, order_ids), key=lambda row: int(row["order_id"]))
    events = sorted(
        query_events(client, sorted(network_user_ids), max_assessment_at),
        key=lambda row: (int(row["user_id"]), row["created_at"], int(row["id"])),
    )

    integrity = verify_integrity(cases, order_items, products, users, orders, network_user_ids)
    snapshot_dir.mkdir(parents=True)
    outputs = {
        "source_order_items_snapshot.csv": (order_items, ORDER_ITEM_FIELDS),
        "source_products_snapshot.csv": (products, PRODUCT_FIELDS),
        "source_users_snapshot.csv": (users, USER_FIELDS),
        "source_orders_snapshot.csv": (orders, ORDER_FIELDS),
        "source_events_snapshot.csv": (events, EVENT_FIELDS),
    }
    row_counts = {
        name: write_csv(snapshot_dir / name, rows, fields)
        for name, (rows, fields) in outputs.items()
    }
    metadata = load_generation_metadata(input_dir)
    manifest = {
        "snapshot_created_at": datetime.now(timezone.utc).isoformat(),
        "source_dataset": SOURCE_DATASET,
        "source_tables": {
            "CORE": [f"{SOURCE_DATASET}.order_items", f"{SOURCE_DATASET}.products"],
            "ENRICHMENT": [f"{SOURCE_DATASET}.users", f"{SOURCE_DATASET}.orders"],
            "NETWORK_PROVENANCE": [f"{SOURCE_DATASET}.events"],
        },
        "snapshot_csv_row_counts": row_counts,
        "returnguard_case_count": len(cases),
        "unique_anchor_order_item_ids": len({case.order_item_id for case in cases}),
        "relevant_user_count": len(all_user_ids),
        "network_provenance_user_count": len(network_user_ids),
        "relevant_product_count": len(product_ids),
        "relevant_order_count": len(order_ids),
        "assessment_at_min": min(case.assessment_at for case in cases).isoformat(),
        "assessment_at_max": max_assessment_at.isoformat(),
        **metadata,
        "provenance": (
            "Point-in-time reproducibility snapshot of public The Look source rows "
            "supporting the verified 80-case ReturnGuard demonstration. Individual "
            "calculations must still apply each return's assessment_at and exclude "
            "its current order_item_id."
        ),
        "network_provenance_notice": (
            "The events snapshot is bounded source provenance/context only. It is not "
            "runtime network evidence; runtime Network Intelligence continues to use "
            "controlled ReturnGuard synthetic_network_links."
        ),
        "integrity": integrity,
    }
    manifest["sha256"] = {
        name: sha256(snapshot_dir / name) for name in outputs
    }
    with (snapshot_dir / "snapshot_manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print("SOURCE SNAPSHOT")
    print(f"Cases: {len(cases)}")
    print(f"Anchors present: {integrity['anchors_present']}/{len(cases)}")
    print(f"Anchor tuple/price matches: {integrity['anchor_tuple_and_price_matches']}/{len(cases)}")
    print(f"Order item rows: {len(order_items)}")
    print(f"Product rows: {len(products)}")
    print(f"User rows: {len(users)}")
    print(f"Order rows: {len(orders)}")
    print(f"Network provenance event rows: {len(events)}")
    print(f"Duplicate order_item IDs: {integrity['duplicate_order_item_ids']}")
    print(f"Current-case exclusion reproducible: {integrity['current_case_exclusion_reproducible']}")
    print(f"SNAPSHOT INTEGRITY: {integrity['status']}")
    return 0 if integrity["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
