#!/usr/bin/env python3
"""
Repair ReturnGuard inspection weights after canonicalizing expected_weight_kg.

Why this exists:
- The generator originally sampled expected_weight_kg independently in
  product_attributes and return_inspections.
- BigQuery expected_weight_kg was corrected to the canonical product baseline.
- actual_weight_kg had originally been generated as a scenario-specific ratio
  of the OLD inspection expected weight, so changing expected alone distorted
  those intended ratios.

This script restores semantic consistency WITHOUT regenerating the corpus:
  new_expected = product_attributes.expected_weight_kg
  ratio        = original_actual / original_inspection_expected
  new_actual   = round(new_expected * ratio, 3)

It reads the untouched rebuilt local CSVs as the source of the ORIGINAL
inspection expected/actual pair and updates only rows where both values exist.

By default it is DRY RUN. Add --apply to update BigQuery.

Usage:
  .venv/bin/python scripts/diagnostics/repair_inspection_weights.py
  .venv/bin/python scripts/diagnostics/repair_inspection_weights.py --apply
"""

from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd
from google.cloud import bigquery


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--project-id", default="return-guard-506407")
    p.add_argument("--controlled-dir", default="data/generated_returnguard_rebuilt")
    p.add_argument(
        "--datasets",
        nargs="+",
        default=["returnguard", "returnguard_rebuilt_stage"],
        help="BigQuery datasets to update",
    )
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main():
    a = parse_args()
    root = Path(a.controlled_dir)

    inspections = pd.read_csv(root / "return_inspections.csv")
    requests = pd.read_csv(root / "return_requests.csv", usecols=["return_id", "product_id"])
    products = pd.read_csv(root / "product_attributes.csv", usecols=["product_id", "expected_weight_kg"])

    df = inspections.merge(requests, on="return_id", how="left")
    df = df.merge(
        products.rename(columns={"expected_weight_kg": "canonical_expected_weight_kg"}),
        on="product_id",
        how="left",
    )

    repair = df[
        df["expected_weight_kg"].notna()
        & df["actual_weight_kg"].notna()
        & df["canonical_expected_weight_kg"].notna()
    ].copy()

    repair["original_ratio"] = repair["actual_weight_kg"] / repair["expected_weight_kg"]
    repair["new_expected_weight_kg"] = repair["canonical_expected_weight_kg"]
    repair["new_actual_weight_kg"] = (
        repair["new_expected_weight_kg"] * repair["original_ratio"]
    ).round(3)

    print("Inspection-weight semantic repair")
    print("=" * 100)
    print(f"Rows eligible: {len(repair)}")
    print()
    for _, r in repair.sort_values(["scenario_id", "return_id"]).iterrows():
        print(
            f"{r['return_id']:14s} {r['scenario_id']:6s} "
            f"old_exp={r['expected_weight_kg']:.3f} "
            f"old_actual={r['actual_weight_kg']:.3f} "
            f"ratio={r['original_ratio']:.4f} "
            f"-> new_exp={r['new_expected_weight_kg']:.3f} "
            f"new_actual={r['new_actual_weight_kg']:.3f}"
        )

    if not a.apply:
        print("\nDRY RUN ONLY. Re-run with --apply to update BigQuery.")
        return

    client = bigquery.Client(project=a.project_id)

    sql = """
    UPDATE `{project}.{dataset}.return_inspections`
    SET expected_weight_kg = @expected_weight,
        actual_weight_kg = @actual_weight
    WHERE return_id = @return_id
    """

    for dataset in a.datasets:
        print(f"\nUpdating {a.project_id}.{dataset}.return_inspections ...")
        for _, r in repair.iterrows():
            cfg = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("expected_weight", "FLOAT64", float(r["new_expected_weight_kg"])),
                    bigquery.ScalarQueryParameter("actual_weight", "FLOAT64", float(r["new_actual_weight_kg"])),
                    bigquery.ScalarQueryParameter("return_id", "STRING", str(r["return_id"])),
                ]
            )
            client.query(
                sql.format(project=a.project_id, dataset=dataset),
                job_config=cfg,
            ).result()
        print(f"Updated {len(repair)} rows.")

    print("\nDONE. Re-run the cross-table audit and the inspection weight query.")


if __name__ == "__main__":
    main()
