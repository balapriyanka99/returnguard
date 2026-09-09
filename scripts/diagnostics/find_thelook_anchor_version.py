#!/usr/bin/env python3
"""
Find the historical The Look version that matches a ReturnGuard controlled anchor.

READ ONLY.

The script:
1. Reads the expected anchor fields from ReturnGuard return_requests.
2. Scans historical versions of public The Look order_items using
   FOR SYSTEM_TIME AS OF at a configurable interval.
3. Reports timestamps where the live historical row matches the controlled anchor.
4. Also prints every distinct incarnation observed for that order_item_id.

Usage:
  python scripts/find_thelook_anchor_version.py --return-id RTN-M08-002

Optional:
  --hours-back 168       # default 7 days
  --step-minutes 30      # default 30 minutes
  --project return-guard-506407
  --dataset returnguard

This script performs queries only; it does not write BigQuery data.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from google.cloud import bigquery


def norm_number(v: Any) -> float | None:
    if v is None:
        return None
    return float(v)


def clean(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--return-id", required=True)
    p.add_argument("--project", default="return-guard-506407")
    p.add_argument("--dataset", default="returnguard")
    p.add_argument("--hours-back", type=int, default=168)
    p.add_argument("--step-minutes", type=int, default=30)
    p.add_argument("--output")
    args = p.parse_args()

    client = bigquery.Client(project=args.project)
    rr_table = f"{args.project}.{args.dataset}.return_requests"

    rr_sql = f"""
    SELECT
      return_id,
      order_item_id,
      order_id,
      user_id,
      product_id,
      anchor_sale_price,
      generator_version,
      requested_at,
      assessment_at
    FROM `{rr_table}`
    WHERE return_id = @return_id
    """
    rr_cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("return_id", "STRING", args.return_id)
        ]
    )
    rr_rows = list(client.query(rr_sql, job_config=rr_cfg).result())
    if not rr_rows:
        raise SystemExit(f"Return {args.return_id!r} not found.")

    rr = dict(rr_rows[0].items())
    expected = {
        "id": int(rr["order_item_id"]),
        "order_id": int(rr["order_id"]),
        "user_id": int(rr["user_id"]),
        "product_id": int(rr["product_id"]),
        "sale_price": norm_number(rr["anchor_sale_price"]),
    }

    print("ReturnGuard / The Look historical anchor scanner")
    print("READ-ONLY: no BigQuery writes will be performed.")
    print(f"Return ID : {args.return_id}")
    print(f"Expected  : {expected}")
    print(f"Generator : {rr.get('generator_version')}")
    print(f"Requested : {clean(rr.get('requested_at'))}")

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=args.hours_back)
    step = timedelta(minutes=args.step_minutes)

    query = """
    SELECT
      id, order_id, user_id, product_id, inventory_item_id,
      status, sale_price, created_at, shipped_at, delivered_at, returned_at
    FROM `bigquery-public-data.thelook_ecommerce.order_items`
    FOR SYSTEM_TIME AS OF @as_of
    WHERE id = @id
    """
    distinct = {}
    exact_matches = []
    errors = []

    t = start
    checked = 0
    while t <= now:
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", t),
                bigquery.ScalarQueryParameter("id", "INT64", expected["id"]),
            ]
        )
        try:
            rows = list(client.query(query, job_config=cfg).result())
            checked += 1
            if rows:
                row = dict(rows[0].items())
                signature = (
                    row.get("order_id"),
                    row.get("user_id"),
                    row.get("product_id"),
                    norm_number(row.get("sale_price")),
                )
                key = repr(signature)
                item = distinct.setdefault(
                    key,
                    {
                        "signature": {
                            "order_id": row.get("order_id"),
                            "user_id": row.get("user_id"),
                            "product_id": row.get("product_id"),
                            "sale_price": norm_number(row.get("sale_price")),
                        },
                        "first_seen_as_of": t.isoformat(),
                        "last_seen_as_of": t.isoformat(),
                        "samples": 0,
                    },
                )
                item["last_seen_as_of"] = t.isoformat()
                item["samples"] += 1

                same = (
                    row.get("order_id") == expected["order_id"]
                    and row.get("user_id") == expected["user_id"]
                    and row.get("product_id") == expected["product_id"]
                    and abs(norm_number(row.get("sale_price")) - expected["sale_price"]) < 1e-6
                )
                if same:
                    exact_matches.append(
                        {
                            "as_of": t.isoformat(),
                            "row": {k: clean(v) for k, v in row.items()},
                        }
                    )
        except Exception as exc:
            errors.append(
                {
                    "as_of": t.isoformat(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

        t += step

    print(f"\nHistorical timestamps checked: {checked}")
    print(f"Distinct incarnations observed: {len(distinct)}")
    print(f"Exact controlled-anchor matches: {len(exact_matches)}")

    print("\nDistinct incarnations:")
    for i, item in enumerate(
        sorted(distinct.values(), key=lambda x: x["first_seen_as_of"]), 1
    ):
        print(
            f"{i:2d}. {item['first_seen_as_of']} → {item['last_seen_as_of']} "
            f"{item['signature']}  samples={item['samples']}"
        )

    if exact_matches:
        print("\nMATCH FOUND.")
        print(f"First sampled match: {exact_matches[0]['as_of']}")
        print(f"Last sampled match : {exact_matches[-1]['as_of']}")
        print(
            "Run again around that interval with --step-minutes 5 if you want "
            "a tighter source-version boundary."
        )
    else:
        print("\nNO EXACT MATCH FOUND IN THE SAMPLED WINDOW.")
        print(
            "This can mean the matching version is outside the available "
            "time-travel window, or it existed between sampling points. "
            "Try a smaller --step-minutes value if the query budget permits."
        )

    report = {
        "generated_at": now.isoformat(),
        "mode": "read_only",
        "return_id": args.return_id,
        "expected_anchor": expected,
        "hours_back": args.hours_back,
        "step_minutes": args.step_minutes,
        "checked_timestamps": checked,
        "distinct_incarnations": list(distinct.values()),
        "exact_matches": exact_matches,
        "errors": errors[:20],
        "error_count": len(errors),
    }

    output = Path(
        args.output
        or f"data/generated_returnguard/thelook_anchor_version_{args.return_id}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=clean), encoding="utf-8")
    print(f"\nJSON report written to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
