#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery

THELOOK_PROJECT = os.getenv("THELOOK_PROJECT", "bigquery-public-data")
THELOOK_DATASET = os.getenv("THELOOK_DATASET", "thelook_ecommerce")
REPORT_PATH = Path(
    os.getenv(
        "METADATA_REPORT_PATH",
        "data/generated_returnguard/thelook_table_metadata_report.json",
    )
)

def iso(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else value

def main():
    client = bigquery.Client()
    dataset_id = f"{THELOOK_PROJECT}.{THELOOK_DATASET}"
    dataset = client.get_dataset(dataset_id)
    location = dataset.location

    print("The Look BigQuery metadata report")
    print("READ-ONLY: no BigQuery writes will be performed.")
    print(f"Dataset : {dataset_id}")
    print(f"Location: {location}")
    print(f"Checked : {datetime.now(timezone.utc).isoformat()}")

    table_details = {}
    for item in client.list_tables(dataset):
        full_id = f"{THELOOK_PROJECT}.{THELOOK_DATASET}.{item.table_id}"
        t = client.get_table(full_id)
        table_details[item.table_id] = {
            "table_name": item.table_id,
            "table_type": getattr(t, "table_type", None),
            "created": iso(getattr(t, "created", None)),
            "modified": iso(getattr(t, "modified", None)),
            "num_rows_tables_get": getattr(t, "num_rows", None),
        }

    region = f"region-{str(location).lower()}"
    sql = f'''
    SELECT
      table_schema,
      table_name,
      table_type,
      storage_last_modified_time,
      total_rows
    FROM `{THELOOK_PROJECT}.{region}`.INFORMATION_SCHEMA.TABLE_STORAGE
    WHERE table_schema = @dataset
      AND NOT deleted
    ORDER BY table_name
    '''
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("dataset", "STRING", THELOOK_DATASET)
        ]
    )
    storage_rows = list(client.query(sql, job_config=cfg, location=location).result())
    storage = {
        r["table_name"]: {
            "storage_last_modified_time": iso(r["storage_last_modified_time"]),
            "total_rows_table_storage": r["total_rows"],
            "storage_table_type": r["table_type"],
        }
        for r in storage_rows
    }

    rows = []
    for name in sorted(set(table_details) | set(storage)):
        row = {"table_name": name}
        row.update(table_details.get(name, {}))
        row.update(storage.get(name, {}))
        rows.append(row)

    print("\n%-26s %-24s %-24s %-12s" % (
        "TABLE", "DATA LAST WRITTEN", "METADATA MODIFIED", "ROWS"
    ))
    print("-" * 92)
    for row in rows:
        rows_value = row.get("total_rows_table_storage")
        if rows_value is None:
            rows_value = row.get("num_rows_tables_get", "-")
        print("%-26s %-24s %-24s %-12s" % (
            row["table_name"][:26],
            str(row.get("storage_last_modified_time") or "-")[:24],
            str(row.get("modified") or "-")[:24],
            str(rows_value)[:12],
        ))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "project": THELOOK_PROJECT,
        "dataset": THELOOK_DATASET,
        "location": location,
        "tables": rows,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nJSON report written to: {REPORT_PATH}")

if __name__ == "__main__":
    main()
