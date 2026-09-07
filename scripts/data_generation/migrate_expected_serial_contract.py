#!/usr/bin/env python3
"""Copy a ReturnGuard dataset and add the explicit S07/M08 serial baseline."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from generate_returnguard_synthetic_enrichment import serial_for


SOURCE_CONTRACT_VERSION = "rg-synth-v2.0.0"
TARGET_INSPECTION_CONTRACT_VERSION = "rg-synth-v2.0.1"
SERIAL_SCENARIOS = frozenset({"S07", "M08"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: sha256(path)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def case_index(return_id: str, scenario_id: str) -> int:
    prefix = f"RTN-{scenario_id}-"
    if not return_id.startswith(prefix):
        raise ValueError(f"Unexpected {scenario_id} return_id: {return_id}")
    return int(return_id.removeprefix(prefix))


def build_migrated_inspections(
    requests: dict[str, dict[str, str]],
    inspections: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    affected_ids: list[str] = []
    migrated: list[dict[str, str]] = []
    for original in inspections:
        row = dict(original)
        row["expected_serial"] = ""
        scenario_id = row["scenario_id"]
        if scenario_id in SERIAL_SCENARIOS:
            return_id = row["return_id"]
            request = requests.get(return_id)
            if request is None:
                raise ValueError(f"Missing return_request for {return_id}")
            if request["scenario_id"] != scenario_id:
                raise ValueError(f"Scenario mismatch for {return_id}")
            source_version = row["generator_version"]
            if source_version != SOURCE_CONTRACT_VERSION:
                raise ValueError(
                    f"{return_id} has source inspection version {source_version}; "
                    f"expected {SOURCE_CONTRACT_VERSION}"
                )
            expected = serial_for(
                int(request["product_id"]),
                scenario_id,
                case_index(return_id, scenario_id),
                version=source_version,
            )
            if row["returned_serial"] != f"{expected}-SWAP":
                raise ValueError(
                    f"{return_id}: returned_serial does not match the deterministic "
                    "expected_serial + '-SWAP' convention"
                )
            row["expected_serial"] = expected
            row["generator_version"] = TARGET_INSPECTION_CONTRACT_VERSION
            affected_ids.append(return_id)
        migrated.append(row)
    if not affected_ids:
        raise ValueError("No S07/M08 inspection rows found")
    return migrated, affected_ids


def verify_business_preservation(
    original: list[dict[str, str]],
    migrated: list[dict[str, str]],
    affected_ids: set[str],
) -> None:
    if len(original) != len(migrated):
        raise ValueError("Inspection row count changed")
    for before, after in zip(original, migrated):
        if before["return_id"] != after["return_id"]:
            raise ValueError("Inspection row ordering or identity changed")
        allowed_changes = {"expected_serial"}
        if before["return_id"] in affected_ids:
            allowed_changes.add("generator_version")
        for field, value in before.items():
            if field not in allowed_changes and after.get(field) != value:
                raise ValueError(f"Unexpected business-value change: {before['return_id']}.{field}")


def update_json_artifacts(output_dir: Path, affected_ids: list[str]) -> None:
    manifest_path = output_dir / "generation_manifest.json"
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["inspection_contract_version"] = TARGET_INSPECTION_CONTRACT_VERSION
    migrations = manifest.setdefault("versioned_migrations", [])
    migrations.append({
        "name": "explicit_expected_serial",
        "from_version": SOURCE_CONTRACT_VERSION,
        "to_version": TARGET_INSPECTION_CONTRACT_VERSION,
        "affected_table": "return_inspections",
        "affected_return_ids": affected_ids,
        "business_values_preserved": True,
    })
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    validation_path = output_dir / "case_validation_report.json"
    with validation_path.open(encoding="utf-8") as handle:
        validation = json.load(handle)
    validation["inspection_serial_contract"] = {
        "version": TARGET_INSPECTION_CONTRACT_VERSION,
        "affected_return_ids": affected_ids,
        "expected_serial_present": True,
        "returned_serial_present": True,
        "serials_differ": True,
        "controlled_swap_convention_valid": True,
        "status": "PASS",
    }
    with validation_path.open("w", encoding="utf-8") as handle:
        json.dump(validation, handle, indent=2)
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise ValueError(f"Input dataset directory does not exist: {input_dir}")
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing output directory: {output_dir}")
    if input_dir == output_dir:
        raise ValueError("Input and output directories must differ")

    request_fields, request_rows = read_csv(input_dir / "return_requests.csv")
    del request_fields
    requests = {row["return_id"]: row for row in request_rows}
    inspection_fields, inspection_rows = read_csv(input_dir / "return_inspections.csv")
    if "expected_serial" in inspection_fields and any(row.get("expected_serial") for row in inspection_rows):
        raise ValueError("Input already contains populated expected_serial values")
    migrated_rows, affected_ids = build_migrated_inspections(requests, inspection_rows)
    verify_business_preservation(inspection_rows, migrated_rows, set(affected_ids))

    input_hashes = file_hashes(input_dir)
    shutil.copytree(input_dir, output_dir)
    fields = [field for field in inspection_fields if field != "expected_serial"]
    returned_position = fields.index("returned_serial")
    fields.insert(returned_position, "expected_serial")
    write_csv(output_dir / "return_inspections.csv", fields, migrated_rows)
    update_json_artifacts(output_dir, affected_ids)

    output_hashes = file_hashes(output_dir)
    unchanged_csvs = sorted(
        name for name, digest in input_hashes.items()
        if name.endswith(".csv")
        and name != "return_inspections.csv"
        and output_hashes.get(name) == digest
    )
    expected_unchanged_csvs = sorted(
        name for name in input_hashes
        if name.endswith(".csv") and name != "return_inspections.csv"
    )
    if unchanged_csvs != expected_unchanged_csvs:
        raise ValueError("One or more unrelated CSV files changed")

    migration_report: dict[str, Any] = {
        "migration": "explicit_expected_serial",
        "source_contract_version": SOURCE_CONTRACT_VERSION,
        "target_inspection_contract_version": TARGET_INSPECTION_CONTRACT_VERSION,
        "affected_return_ids": affected_ids,
        "affected_inspection_rows": len(affected_ids),
        "inspection_row_count_preserved": len(inspection_rows),
        "unrelated_csv_hashes_preserved": True,
        "unchanged_csv_files": unchanged_csvs,
        "input_file_sha256": input_hashes,
        "output_file_sha256": output_hashes,
        "serial_convention_verified": True,
        "business_values_preserved": True,
        "status": "PASS",
    }
    with (output_dir / "expected_serial_migration_report.json").open("w", encoding="utf-8") as handle:
        json.dump(migration_report, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print("EXPECTED SERIAL MIGRATION")
    print(f"Input rows: {len(inspection_rows)}")
    print(f"Affected S07/M08 rows: {len(affected_ids)}")
    print("Returned serial convention: PASS")
    print(f"Unrelated CSV hashes preserved: {len(unchanged_csvs)}/{len(expected_unchanged_csvs)}")
    print("Business-value preservation: PASS")
    print(f"Output: {output_dir}")
    print("EXPECTED SERIAL MIGRATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
