#!/usr/bin/env python3
"""
ReturnGuard mapping-specific drift diagnostic.

READ ONLY.

Compares the frozen source_order_items_snapshot with the current live
bigquery-public-data.thelook_ecommerce.order_items rows for the SAME item IDs.

It answers:
  - Did order_id mappings change?
  - Did user_id mappings change?
  - Did product_id mappings change?
  - Did inventory_item_id mappings change?
  - Which non-mapping fields changed, and how often?
  - Are ALL rows changing only because of timestamp/status/etc. drift?

It also compares product identity fields in source_products_snapshot.

Outputs:
  - concise terminal summary
  - JSON report with field-level counts and sample changed rows

Environment variables:
  RETURNGUARD_PROJECT   default: return-guard-506407
  RETURNGUARD_DATASET   default: returnguard
  THELOOK_PROJECT       default: bigquery-public-data
  THELOOK_DATASET       default: thelook_ecommerce
  MAPPING_DRIFT_REPORT_PATH
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from google.cloud import bigquery


RG_PROJECT = os.getenv("RETURNGUARD_PROJECT", "return-guard-506407")
RG_DATASET = os.getenv("RETURNGUARD_DATASET", "returnguard")
TL_PROJECT = os.getenv("THELOOK_PROJECT", "bigquery-public-data")
TL_DATASET = os.getenv("THELOOK_DATASET", "thelook_ecommerce")

REPORT_PATH = Path(
    os.getenv(
        "MAPPING_DRIFT_REPORT_PATH",
        "data/generated_returnguard/thelook_mapping_drift_report.json",
    )
)

SNAPSHOT_ORDER_ITEMS = f"{RG_PROJECT}.{RG_DATASET}.source_order_items_snapshot"
LIVE_ORDER_ITEMS = f"{TL_PROJECT}.{TL_DATASET}.order_items"

SNAPSHOT_PRODUCTS = f"{RG_PROJECT}.{RG_DATASET}.source_products_snapshot"
LIVE_PRODUCTS = f"{TL_PROJECT}.{TL_DATASET}.products"

ORDER_ITEM_FIELDS = [
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

MAPPING_FIELDS = [
    "order_id",
    "user_id",
    "product_id",
    "inventory_item_id",
]

NON_MAPPING_FIELDS = [
    "status",
    "created_at",
    "shipped_at",
    "delivered_at",
    "returned_at",
    "sale_price",
]

PRODUCT_FIELDS = [
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


def safe(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def norm(v: Any) -> Any:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    return v


def common_fields(client: bigquery.Client, left: str, right: str, wanted: list[str]) -> list[str]:
    left_schema = {f.name for f in client.get_table(left).schema}
    right_schema = {f.name for f in client.get_table(right).schema}
    return [f for f in wanted if f in left_schema and f in right_schema]


def fetch_snapshot(client: bigquery.Client, table: str, fields: list[str]) -> list[dict[str, Any]]:
    cols = ", ".join(f"`{f}`" for f in fields)
    return [dict(r.items()) for r in client.query(f"SELECT {cols} FROM `{table}`").result()]


def fetch_live_by_ids(
    client: bigquery.Client,
    table: str,
    fields: list[str],
    ids: list[int],
) -> list[dict[str, Any]]:
    if not ids:
        return []
    cols = ", ".join(f"`{f}`" for f in fields)
    sql = f"""
    SELECT {cols}
    FROM `{table}`
    WHERE id IN UNNEST(@ids)
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "INT64", ids)]
    )
    return [dict(r.items()) for r in client.query(sql, job_config=cfg).result()]


def compare_rows(
    snapshot_rows: list[dict[str, Any]],
    live_rows: list[dict[str, Any]],
    fields: list[str],
    sample_limit: int = 10,
) -> dict[str, Any]:
    snap = {r["id"]: r for r in snapshot_rows}
    live = {r["id"]: r for r in live_rows}

    field_change_counts = {f: 0 for f in fields if f != "id"}
    mapping_row_count = 0
    non_mapping_only_count = 0
    unchanged_count = 0
    missing_live_ids: list[int] = []
    mapping_samples: list[dict[str, Any]] = []
    non_mapping_samples: list[dict[str, Any]] = []

    for row_id, srow in snap.items():
        lrow = live.get(row_id)
        if lrow is None:
            missing_live_ids.append(row_id)
            continue

        changes: dict[str, dict[str, Any]] = {}
        for field in fields:
            if field == "id":
                continue
            sv = srow.get(field)
            lv = lrow.get(field)
            if norm(sv) != norm(lv):
                field_change_counts[field] += 1
                changes[field] = {"snapshot": safe(sv), "live": safe(lv)}

        if not changes:
            unchanged_count += 1
            continue

        mapping_changes = {k: v for k, v in changes.items() if k in MAPPING_FIELDS}
        if mapping_changes:
            mapping_row_count += 1
            if len(mapping_samples) < sample_limit:
                mapping_samples.append(
                    {"id": row_id, "changed_mappings": mapping_changes}
                )
        else:
            non_mapping_only_count += 1
            if len(non_mapping_samples) < sample_limit:
                non_mapping_samples.append(
                    {"id": row_id, "changed_fields": changes}
                )

    return {
        "snapshot_rows": len(snapshot_rows),
        "live_matches": len(live_rows),
        "missing_live_count": len(missing_live_ids),
        "missing_live_sample": missing_live_ids[:sample_limit],
        "unchanged_rows": unchanged_count,
        "rows_with_mapping_drift": mapping_row_count,
        "rows_with_only_non_mapping_drift": non_mapping_only_count,
        "field_change_counts": field_change_counts,
        "mapping_drift_samples": mapping_samples,
        "non_mapping_drift_samples": non_mapping_samples,
    }


def main() -> int:
    print("ReturnGuard / The Look mapping drift diagnostic")
    print("READ-ONLY: no BigQuery writes will be performed.")
    print(f"Compared at: {datetime.now(timezone.utc).isoformat()}")

    client = bigquery.Client(project=RG_PROJECT)

    order_fields = common_fields(
        client, SNAPSHOT_ORDER_ITEMS, LIVE_ORDER_ITEMS, ORDER_ITEM_FIELDS
    )
    if "id" not in order_fields:
        raise RuntimeError("Order-item id is missing from one of the compared tables.")

    snapshot_order_rows = fetch_snapshot(client, SNAPSHOT_ORDER_ITEMS, order_fields)
    ids = [int(r["id"]) for r in snapshot_order_rows if r.get("id") is not None]
    live_order_rows = fetch_live_by_ids(client, LIVE_ORDER_ITEMS, order_fields, ids)

    order_report = compare_rows(snapshot_order_rows, live_order_rows, order_fields)

    product_fields = common_fields(
        client, SNAPSHOT_PRODUCTS, LIVE_PRODUCTS, PRODUCT_FIELDS
    )
    snapshot_product_rows = fetch_snapshot(client, SNAPSHOT_PRODUCTS, product_fields)
    pids = [int(r["id"]) for r in snapshot_product_rows if r.get("id") is not None]
    live_product_rows = fetch_live_by_ids(client, LIVE_PRODUCTS, product_fields, pids)

    product_report = compare_rows(
        snapshot_product_rows,
        live_product_rows,
        product_fields,
    )

    print("\n=== ORDER ITEM MAPPING DRIFT ===")
    print(f"Snapshot rows                    : {order_report['snapshot_rows']}")
    print(f"Live matched rows                : {order_report['live_matches']}")
    print(f"Unchanged rows                   : {order_report['unchanged_rows']}")
    print(f"Rows with mapping drift          : {order_report['rows_with_mapping_drift']}")
    print(
        "Rows with only non-mapping drift : "
        f"{order_report['rows_with_only_non_mapping_drift']}"
    )
    print(f"Missing live rows                : {order_report['missing_live_count']}")

    print("\nField-level change counts:")
    for field, count in order_report["field_change_counts"].items():
        marker = "  <-- MAPPING" if field in MAPPING_FIELDS else ""
        print(f"  {field:20s} {count:8d}{marker}")

    if order_report["rows_with_mapping_drift"]:
        print("\nWARNING: RELATIONAL MAPPING DRIFT DETECTED.")
        print("Inspect mapping_drift_samples in the JSON report.")
    else:
        print("\nPASS: No order/user/product/inventory mapping drift detected.")
        print("The order-item differences are confined to non-mapping fields.")

    print("\n=== PRODUCT TABLE ===")
    print(f"Snapshot rows           : {product_report['snapshot_rows']}")
    print(f"Unchanged rows          : {product_report['unchanged_rows']}")
    print(f"Rows with any differences: "
          f"{product_report['rows_with_mapping_drift'] + product_report['rows_with_only_non_mapping_drift']}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "snapshot_order_items": SNAPSHOT_ORDER_ITEMS,
        "live_order_items": LIVE_ORDER_ITEMS,
        "snapshot_products": SNAPSHOT_PRODUCTS,
        "live_products": LIVE_PRODUCTS,
        "mapping_fields": MAPPING_FIELDS,
        "order_items": order_report,
        "products": product_report,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=safe), encoding="utf-8")
    print(f"\nJSON report written to: {REPORT_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
