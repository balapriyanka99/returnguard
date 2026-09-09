#!/usr/bin/env python3
"""
Read-only drift checker for ReturnGuard's frozen The Look snapshots.

Compares:
  1) ReturnGuard-owned source_order_items_snapshot
     vs bigquery-public-data.thelook_ecommerce.order_items

  2) ReturnGuard-owned source_products_snapshot
     vs bigquery-public-data.thelook_ecommerce.products

The script DOES NOT modify BigQuery data.

Usage:
  python scripts/verify_thelook_snapshot_drift.py

Optional environment variables:
  RETURNGUARD_PROJECT=return-guard-506407
  RETURNGUARD_DATASET=returnguard
  THELOOK_PROJECT=bigquery-public-data
  THELOOK_DATASET=thelook_ecommerce
  DRIFT_REPORT_PATH=data/generated_returnguard/thelook_snapshot_drift_report.json

Requirements:
  google-cloud-bigquery
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from google.cloud import bigquery


RETURNGUARD_PROJECT = os.getenv("RETURNGUARD_PROJECT", "return-guard-506407")
RETURNGUARD_DATASET = os.getenv("RETURNGUARD_DATASET", "returnguard")
THELOOK_PROJECT = os.getenv("THELOOK_PROJECT", "bigquery-public-data")
THELOOK_DATASET = os.getenv("THELOOK_DATASET", "thelook_ecommerce")
REPORT_PATH = Path(
    os.getenv(
        "DRIFT_REPORT_PATH",
        "data/generated_returnguard/thelook_snapshot_drift_report.json",
    )
)

SNAPSHOT_ORDER_ITEMS = (
    f"{RETURNGUARD_PROJECT}.{RETURNGUARD_DATASET}.source_order_items_snapshot"
)
SNAPSHOT_PRODUCTS = (
    f"{RETURNGUARD_PROJECT}.{RETURNGUARD_DATASET}.source_products_snapshot"
)

LIVE_ORDER_ITEMS = f"{THELOOK_PROJECT}.{THELOOK_DATASET}.order_items"
LIVE_PRODUCTS = f"{THELOOK_PROJECT}.{THELOOK_DATASET}.products"

# Deliberately compare only fields that exist in BOTH tables.
# This makes the script resilient if the snapshot contains extra provenance fields.
PREFERRED_ORDER_ITEM_FIELDS = [
    "id",
    "order_id",
    "user_id",
    "product_id",
    "inventory_item_id",
    "status",
    "created_at",
    "shipped_at",
    "delivered_at",
    "returned_at",
    "sale_price",
]

PREFERRED_PRODUCT_FIELDS = [
    "id",
    "cost",
    "category",
    "name",
    "brand",
    "retail_price",
    "department",
    "sku",
    "distribution_center_id",
]


@dataclass
class TableDriftSummary:
    snapshot_table: str
    live_table: str
    id_field: str
    compared_fields: list[str]
    snapshot_rows: int
    live_matches: int
    missing_in_live: int
    changed_rows: int
    unchanged_rows: int
    duplicate_snapshot_ids: int
    duplicate_live_ids: int


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def normalize(value: Any) -> Any:
    """
    Normalize values only for comparison.
    Datetimes are compared by exact UTC instant where possible.
    Decimals are converted to strings to avoid binary float artifacts.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    if isinstance(value, Decimal):
        return format(value, "f")

    return value


def get_schema_fields(client: bigquery.Client, table_id: str) -> set[str]:
    table = client.get_table(table_id)
    return {field.name for field in table.schema}


def choose_common_fields(
    snapshot_fields: set[str],
    live_fields: set[str],
    preferred_fields: Iterable[str],
) -> list[str]:
    return [
        field
        for field in preferred_fields
        if field in snapshot_fields and field in live_fields
    ]


def fetch_rows(
    client: bigquery.Client,
    table_id: str,
    fields: list[str],
) -> list[dict[str, Any]]:
    select_list = ", ".join(f"`{field}`" for field in fields)
    sql = f"SELECT {select_list} FROM `{table_id}`"
    return [dict(row.items()) for row in client.query(sql).result()]


def index_rows(rows: list[dict[str, Any]], id_field: str):
    indexed: dict[Any, dict[str, Any]] = {}
    duplicates: list[Any] = []

    for row in rows:
        row_id = row.get(id_field)
        if row_id in indexed:
            duplicates.append(row_id)
        else:
            indexed[row_id] = row

    return indexed, duplicates


def compare_tables(
    client: bigquery.Client,
    snapshot_table: str,
    live_table: str,
    preferred_fields: list[str],
    id_field: str = "id",
) -> tuple[TableDriftSummary, list[dict[str, Any]], list[Any]]:
    snapshot_schema = get_schema_fields(client, snapshot_table)
    live_schema = get_schema_fields(client, live_table)

    fields = choose_common_fields(snapshot_schema, live_schema, preferred_fields)

    if id_field not in fields:
        raise RuntimeError(
            f"Required ID field {id_field!r} is not present in both "
            f"{snapshot_table} and {live_table}."
        )

    snapshot_rows = fetch_rows(client, snapshot_table, fields)

    # Compare ONLY the live rows whose IDs are in the snapshot.
    # This avoids scanning / reporting unrelated new live rows.
    snapshot_ids = [row[id_field] for row in snapshot_rows if row[id_field] is not None]

    if snapshot_ids:
        select_list = ", ".join(f"`{field}`" for field in fields)
        live_sql = f"""
        SELECT {select_list}
        FROM `{live_table}`
        WHERE `{id_field}` IN UNNEST(@ids)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("ids", "INT64", snapshot_ids)
            ]
        )
        live_rows = [
            dict(row.items())
            for row in client.query(live_sql, job_config=job_config).result()
        ]
    else:
        live_rows = []

    snapshot_index, duplicate_snapshot_ids = index_rows(snapshot_rows, id_field)
    live_index, duplicate_live_ids = index_rows(live_rows, id_field)

    changes: list[dict[str, Any]] = []
    missing_in_live: list[Any] = []
    unchanged = 0

    for row_id, snapshot_row in snapshot_index.items():
        live_row = live_index.get(row_id)

        if live_row is None:
            missing_in_live.append(row_id)
            continue

        field_changes: dict[str, dict[str, Any]] = {}

        for field in fields:
            if field == id_field:
                continue

            before = snapshot_row.get(field)
            after = live_row.get(field)

            if normalize(before) != normalize(after):
                field_changes[field] = {
                    "snapshot": json_safe(before),
                    "live": json_safe(after),
                }

        if field_changes:
            changes.append(
                {
                    "id": row_id,
                    "changed_fields": field_changes,
                }
            )
        else:
            unchanged += 1

    summary = TableDriftSummary(
        snapshot_table=snapshot_table,
        live_table=live_table,
        id_field=id_field,
        compared_fields=fields,
        snapshot_rows=len(snapshot_rows),
        live_matches=len(live_rows),
        missing_in_live=len(missing_in_live),
        changed_rows=len(changes),
        unchanged_rows=unchanged,
        duplicate_snapshot_ids=len(duplicate_snapshot_ids),
        duplicate_live_ids=len(duplicate_live_ids),
    )

    return summary, changes, missing_in_live


def print_summary(title: str, summary: TableDriftSummary) -> None:
    print(f"\n=== {title} ===")
    print(f"Snapshot table : {summary.snapshot_table}")
    print(f"Live table     : {summary.live_table}")
    print(f"Compared fields: {', '.join(summary.compared_fields)}")
    print(f"Snapshot rows  : {summary.snapshot_rows}")
    print(f"Live matches   : {summary.live_matches}")
    print(f"Unchanged rows : {summary.unchanged_rows}")
    print(f"Changed rows   : {summary.changed_rows}")
    print(f"Missing in live: {summary.missing_in_live}")
    print(f"Duplicate snapshot IDs: {summary.duplicate_snapshot_ids}")
    print(f"Duplicate live IDs    : {summary.duplicate_live_ids}")


def main() -> int:
    print("ReturnGuard / The Look snapshot drift verification")
    print("READ-ONLY: no BigQuery writes will be performed.")
    print(f"Compared at: {datetime.now(timezone.utc).isoformat()}")

    client = bigquery.Client(project=RETURNGUARD_PROJECT)

    try:
        order_summary, order_changes, order_missing = compare_tables(
            client=client,
            snapshot_table=SNAPSHOT_ORDER_ITEMS,
            live_table=LIVE_ORDER_ITEMS,
            preferred_fields=PREFERRED_ORDER_ITEM_FIELDS,
            id_field="id",
        )

        product_summary, product_changes, product_missing = compare_tables(
            client=client,
            snapshot_table=SNAPSHOT_PRODUCTS,
            live_table=LIVE_PRODUCTS,
            preferred_fields=PREFERRED_PRODUCT_FIELDS,
            id_field="id",
        )
    except Exception as exc:
        print(f"\nERROR: drift verification failed: {type(exc).__name__}: {exc}")
        return 1

    print_summary("ORDER ITEMS", order_summary)
    print_summary("PRODUCTS", product_summary)

    total_changed = order_summary.changed_rows + product_summary.changed_rows
    total_missing = order_summary.missing_in_live + product_summary.missing_in_live

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "returnguard_project": RETURNGUARD_PROJECT,
        "returnguard_dataset": RETURNGUARD_DATASET,
        "thelook_project": THELOOK_PROJECT,
        "thelook_dataset": THELOOK_DATASET,
        "order_items": {
            "summary": asdict(order_summary),
            "changed_rows": order_changes,
            "missing_live_ids": order_missing,
        },
        "products": {
            "summary": asdict(product_summary),
            "changed_rows": product_changes,
            "missing_live_ids": product_missing,
        },
        "overall": {
            "changed_rows": total_changed,
            "missing_live_rows": total_missing,
            "drift_detected": bool(total_changed or total_missing),
        },
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, default=json_safe),
        encoding="utf-8",
    )

    print(f"\nJSON report written to: {REPORT_PATH}")

    if total_changed or total_missing:
        print("\nRESULT: DRIFT DETECTED")
        print(
            "The frozen ReturnGuard snapshot differs from the corresponding "
            "current live The Look rows."
        )
    else:
        print("\nRESULT: NO DRIFT DETECTED")
        print(
            "For the snapshot IDs and compared fields, live The Look currently "
            "matches the frozen ReturnGuard snapshot."
        )

    # Drift is informational, not a script failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
