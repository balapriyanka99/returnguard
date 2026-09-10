"""Typed immutable snapshot of economics used by one assessment."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from pydantic import BaseModel

from returnguard.intelligence.models import ReturnEconomics


ECONOMICS_VERSION = "economics-v1"
_NUMERIC_QUANTUM = Decimal("0.000000001")


def _numeric(value: float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(_NUMERIC_QUANTUM, rounding=ROUND_HALF_UP)


def economics_event_id(
    return_id: str,
    assessment_id: str | None,
    assessment_at: datetime,
    economics_version: str = ECONOMICS_VERSION,
) -> str:
    material = "|".join((
        return_id,
        assessment_id or "",
        assessment_at.isoformat(),
        economics_version,
    ))
    return "ECON-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class EconomicsAssessment(BaseModel):
    economics_event_id: str
    return_id: str
    assessment_id: str | None = None
    assessment_at: datetime
    economics_version: str = ECONOMICS_VERSION
    current_item_value: Decimal | None
    product_cost: Decimal | None
    reverse_logistics_cost: Decimal | None
    inspection_cost: Decimal | None
    recovery_value: Decimal | None
    total_operational_cost: Decimal | None
    estimated_net_return_cost: Decimal | None
    estimated_loss_exposure: Decimal | None
    confidence: str
    data_origin: str

    @classmethod
    def from_result(
        cls,
        *,
        return_id: str,
        assessment_id: str | None,
        assessment_at: datetime,
        result: ReturnEconomics,
    ) -> "EconomicsAssessment":
        return cls(
            economics_event_id=economics_event_id(
                return_id, assessment_id, assessment_at
            ),
            return_id=return_id,
            assessment_id=assessment_id,
            assessment_at=assessment_at,
            current_item_value=_numeric(result.current_item_value),
            product_cost=_numeric(result.product_cost),
            reverse_logistics_cost=_numeric(result.reverse_logistics_cost),
            inspection_cost=_numeric(result.inspection_cost),
            recovery_value=_numeric(result.recovery_value),
            total_operational_cost=_numeric(result.total_operational_cost),
            estimated_net_return_cost=_numeric(result.estimated_net_return_cost),
            estimated_loss_exposure=_numeric(result.estimated_loss_exposure),
            confidence=result.economics_data_confidence,
            data_origin=result.data_origin,
        )
