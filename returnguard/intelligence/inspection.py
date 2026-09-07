"""Inspection retrieval and typed record mapping."""

from __future__ import annotations

import json
from typing import Any

from .models import InspectionRecord
from .repository import IntelligenceRepository


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def get_inspection_record(
    repository: IntelligenceRepository,
    return_id: str,
) -> InspectionRecord | None:
    row = repository.get_inspection(return_id)
    if row is None:
        return None
    accessories = _json(row.get("accessories_present"), [])
    expected_accessories = _json(row.get("expected_accessories"), [])
    expected_serial = row.get("expected_serial")
    returned_serial = row.get("returned_serial")
    return InspectionRecord(
        return_id=row["return_id"],
        product_id=None if row.get("product_id") is None else int(row["product_id"]),
        actual_weight_kg=_float(row.get("actual_weight_kg")),
        expected_serial=expected_serial, returned_serial=returned_serial,
        serial_mismatch=(
            None if expected_serial is None or returned_serial is None
            else expected_serial != returned_serial
        ),
        item_present=row.get("item_present"),
        condition=row.get("condition"), accessories_present=accessories if isinstance(accessories, list) else [],
        expected_accessories=(expected_accessories if isinstance(expected_accessories, list) else []),
        inspection_location=row.get("inspection_location"), inspected_at=row.get("inspected_at"),
        expected_weight_kg=_float(row.get("expected_weight_kg")), source_type=row.get("source_type"),
        product_attributes_source_type=row.get("product_attributes_source_type"),
        product_attributes_generator_version=row.get("product_attributes_generator_version"),
        scenario_id=row.get("scenario_id"), generator_version=row.get("generator_version"),
    )
