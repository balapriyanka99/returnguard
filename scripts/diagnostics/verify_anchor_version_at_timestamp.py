#!/usr/bin/env python3
"""
Verify ReturnGuard controlled anchors against a historical The Look version.

READ ONLY.

Reads controlled anchors from:
  data/generated_returnguard/return_requests.csv

Queries:
  bigquery-public-data.thelook_ecommerce.order_items
  FOR SYSTEM_TIME AS OF <timestamp>

Then compares, for every controlled case:
  order_item_id
  order_id
  user_id
  product_id
  anchor_sale_price

Outputs:
  - readable terminal summary
  - CSV report
  - JSON report

Example:
  python scripts/diagnostics/verify_anchor_version_at_timestamp.py \
    --as-of 2026-09-06T12:00:00+00:00

Optional:
  --scenario-sample 1
      Verify one case per scenario first.

  --return-id RTN-M08-002
      Verify only one return.

No BigQuery tables are modified.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd
from google.cloud import bigquery


DEFAULT_REQUESTS = "data/generated_returnguard/return_requests.csv"
DEFAULT_SOURCE = "bigquery-public-data.thelook_ecommerce.order_items"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--as-of",
        required=True,
        help="Historical timestamp, e.g. 2026-09-06T12:00:00+00:00",
    )
    p.add_argument("--requests", default=DEFAULT_REQUESTS)
    p.add_argument("--source-table", default=DEFAULT_SOURCE)
    p.add_argument("--project", default="return-guard-506407")
    p.add_argument("--return-id")
    p.add_argument(
        "--scenario-sample",
        type=int,
        default=0,
        help="If >0, verify this many cases per scenario instead of all cases.",
    )
    p.add_argument(
        "--output-dir",
        default="data/generated_returnguard/diagnostics",
    )
    return p.parse_args()


def normalize_expected(df: pd.DataFrame) -> pd.DataFrame:
    required = [
        "return_id",
        "scenario_id",
        "order_item_id",
        "order_id",
        "user_id",
        "product_id",
        "anchor_sale_price",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing required columns in return_requests.csv: {missing}")

    out = df[required].copy()

    for col in ["order_item_id", "order_id", "user_id", "product_id"]:
        out[col] = pd.to_numeric(out[col], errors="raise").astype("int64")
    out["anchor_sale_price"] = pd.to_numeric(
        out["anchor_sale_price"], errors="raise"
    ).astype(float)

    return out


def main():
    args = parse_args()

    try:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"Invalid --as-of timestamp: {args.as_of}") from exc

    df = pd.read_csv(args.requests)
    expected = normalize_expected(df)

    if args.return_id:
        expected = expected[expected["return_id"] == args.return_id].copy()
        if expected.empty:
            raise SystemExit(f"{args.return_id} not found in {args.requests}")

    if args.scenario_sample > 0:
        expected = (
            expected.sort_values(["scenario_id", "return_id"])
            .groupby("scenario_id", as_index=False, group_keys=False)
            .head(args.scenario_sample)
            .copy()
        )

    ids = sorted(expected["order_item_id"].unique().tolist())

    client = bigquery.Client(project=args.project)

    sql = f"""
    SELECT
      id,
      order_id,
      user_id,
      product_id,
      sale_price
    FROM `{args.source_table}`
    FOR SYSTEM_TIME AS OF @as_of
    WHERE id IN UNNEST(@ids)
    """

    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", as_of),
            bigquery.ArrayQueryParameter("ids", "INT64", ids),
        ]
    )

    observed_rows = list(client.query(sql, job_config=cfg).result())

    observed = pd.DataFrame(
        [
            {
                "order_item_id": int(r["id"]),
                "observed_order_id": int(r["order_id"]),
                "observed_user_id": int(r["user_id"]),
                "observed_product_id": int(r["product_id"]),
                "observed_sale_price": float(r["sale_price"]),
            }
            for r in observed_rows
        ]
    )

    if observed.empty:
        observed = pd.DataFrame(
            columns=[
                "order_item_id",
                "observed_order_id",
                "observed_user_id",
                "observed_product_id",
                "observed_sale_price",
            ]
        )

    report = expected.merge(observed, on="order_item_id", how="left")

    report["anchor_found"] = report["observed_order_id"].notna()

    report["order_id_match"] = (
        report["anchor_found"]
        & (report["order_id"] == report["observed_order_id"])
    )
    report["user_id_match"] = (
        report["anchor_found"]
        & (report["user_id"] == report["observed_user_id"])
    )
    report["product_id_match"] = (
        report["anchor_found"]
        & (report["product_id"] == report["observed_product_id"])
    )

    report["sale_price_match"] = report.apply(
        lambda r: (
            bool(r["anchor_found"])
            and math.isclose(
                float(r["anchor_sale_price"]),
                float(r["observed_sale_price"]),
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
        ),
        axis=1,
    )

    report["exact_anchor_match"] = (
        report["anchor_found"]
        & report["order_id_match"]
        & report["user_id_match"]
        & report["product_id_match"]
        & report["sale_price_match"]
    )

    def reason(row):
        if not row["anchor_found"]:
            return "anchor_not_found"
        bad = []
        if not row["order_id_match"]:
            bad.append("order_id")
        if not row["user_id_match"]:
            bad.append("user_id")
        if not row["product_id_match"]:
            bad.append("product_id")
        if not row["sale_price_match"]:
            bad.append("sale_price")
        return "MATCH" if not bad else "mismatch:" + ",".join(bad)

    report["result"] = report.apply(reason, axis=1)

    total = len(report)
    matched = int(report["exact_anchor_match"].sum())
    found = int(report["anchor_found"].sum())

    scenario_summary = (
        report.groupby("scenario_id")
        .agg(
            cases=("return_id", "count"),
            anchors_found=("anchor_found", "sum"),
            exact_matches=("exact_anchor_match", "sum"),
        )
        .reset_index()
    )
    scenario_summary["status"] = scenario_summary.apply(
        lambda r: "PASS" if r["cases"] == r["exact_matches"] else "FAIL",
        axis=1,
    )

    print("\nReturnGuard historical anchor verification")
    print("=" * 88)
    print(f"AS OF timestamp : {as_of.isoformat()}")
    print(f"Cases checked   : {total}")
    print(f"Anchors found   : {found}/{total}")
    print(f"Exact matches   : {matched}/{total}")
    print(f"Overall result  : {'PASS' if matched == total else 'FAIL'}")

    print("\nScenario summary")
    print("-" * 88)
    print(
        scenario_summary.to_string(
            index=False,
            columns=["scenario_id", "cases", "anchors_found", "exact_matches", "status"],
        )
    )

    failures = report[~report["exact_anchor_match"]].copy()

    if failures.empty:
        print("\nAll checked controlled anchors match this historical source version.")
    else:
        print("\nFailures")
        print("-" * 88)
        cols = [
            "return_id",
            "scenario_id",
            "order_item_id",
            "order_id",
            "observed_order_id",
            "user_id",
            "observed_user_id",
            "product_id",
            "observed_product_id",
            "anchor_sale_price",
            "observed_sale_price",
            "result",
        ]
        print(failures[cols].to_string(index=False))

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    stamp = as_of.strftime("%Y%m%dT%H%M%SZ")
    csv_path = outdir / f"anchor_verification_{stamp}.csv"
    json_path = outdir / f"anchor_verification_{stamp}.json"

    report.to_csv(csv_path, index=False)

    payload = {
        "as_of": as_of.isoformat(),
        "source_table": args.source_table,
        "cases_checked": total,
        "anchors_found": found,
        "exact_matches": matched,
        "overall_status": "PASS" if matched == total else "FAIL",
        "scenario_summary": scenario_summary.to_dict(orient="records"),
        "cases": report.where(pd.notnull(report), None).to_dict(orient="records"),
    }

    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"\nCSV report  : {csv_path}")
    print(f"JSON report : {json_path}")


if __name__ == "__main__":
    main()
