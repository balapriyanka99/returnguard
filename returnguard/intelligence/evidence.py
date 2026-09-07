"""Evidence retrieval and typed record mapping."""

from __future__ import annotations

import json
from typing import Any

from .models import EvidenceRecord
from .repository import IntelligenceRepository


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def get_evidence_records(
    repository: IntelligenceRepository,
    return_id: str,
) -> list[EvidenceRecord]:
    result = []
    for row in repository.get_evidence(return_id):
        metadata = _json(row.get("claim_metadata"), {})
        result.append(EvidenceRecord(
            evidence_id=row["evidence_id"], return_id=row["return_id"],
            evidence_type=row.get("type"), stage=row.get("stage"), image_uri=row.get("image_uri"),
            reference_image_uri=row.get("reference_image_uri"), submitted_at=row.get("observed_at"),
            source=row.get("source_type"), uploaded_by=row.get("uploaded_by"),
            metadata=metadata if isinstance(metadata, dict) else {"value": metadata},
        ))
    return result
