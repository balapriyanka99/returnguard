"""Return Economics construction from stored ReturnGuard cost fields."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import ReturnEconomics
from .repository import IntelligenceRepository


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def build_return_economics(
    repository: IntelligenceRepository,
    row: dict[str, Any],
    assessment_at: datetime,
) -> ReturnEconomics:
    raw = repository.get_economics(row["return_id"], assessment_at)
    item, product = _float(raw.get("current_item_value")), _float(raw.get("product_cost"))
    reverse = _float(raw.get("reverse_logistics_cost"))
    if reverse is None:
        reverse = _float(raw.get("logistics_reverse_cost"))
    inspection, recovery = _float(raw.get("inspection_cost")), _float(raw.get("recovery_value"))
    total = reverse + inspection if reverse is not None and inspection is not None else None
    net = total + item - recovery if total is not None and item is not None and recovery is not None else None
    present = sum(value is not None for value in (item, reverse, inspection, recovery))
    return ReturnEconomics(
        current_item_value=item, product_cost=product, reverse_logistics_cost=reverse,
        inspection_cost=inspection, recovery_value=recovery, total_operational_cost=total,
        estimated_net_return_cost=net, estimated_loss_exposure=None,
        economics_data_confidence="complete" if present == 4 else "limited" if present else "none",
        data_origin=("controlled_demo" if raw.get("source_type") == "synthetic_demo"
                     else raw.get("source_type") or ("live_source" if present else "insufficient_history")),
    )
