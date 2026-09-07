"""Thin facade and aggregate entry point for Intelligence capabilities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import customer, economics, evidence, inspection, network, product, return_behavior
from .models import (
    CustomerIntelligence,
    EvidenceRecord,
    InspectionRecord,
    NetworkIntelligence,
    ProductIntelligence,
    ReturnBehaviorIntelligence,
    ReturnEconomics,
    ReturnIntelligence,
    ReturnMetadata,
)
from .repository import IntelligenceRepository


class ReturnNotFoundError(LookupError):
    """The requested return is not in the active ReturnGuard dataset."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ReturnIntelligenceService:
    """Resolve return context and delegate to deterministic capabilities."""

    def __init__(self, repository: IntelligenceRepository) -> None:
        self.repository = repository

    def _context(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> tuple[dict[str, Any], datetime]:
        current = self.repository.get_return(return_id)
        if current is None:
            raise ReturnNotFoundError(f"Active return not found: {return_id}")
        effective = assessment_at or current.get("assessment_at")
        if effective is None:
            raise ValueError(f"Return {return_id} has no assessment_at")
        return current, _utc(effective)

    @staticmethod
    def _metadata(row: dict[str, Any], assessment_at: datetime) -> ReturnMetadata:
        return ReturnMetadata(
            return_id=row["return_id"],
            scenario_id=row["scenario_id"],
            generator_version=row["generator_version"],
            user_id=int(row["user_id"]),
            order_item_id=int(row["order_item_id"]),
            order_id=int(row["order_id"]),
            product_id=int(row["product_id"]),
            assessment_at=assessment_at,
            requested_at=row.get("requested_at"),
            reason=row.get("reason"),
            responsibility=row.get("responsibility"),
            status=row.get("status"),
            source_type=row.get("source_type"),
        )

    def get_customer_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> CustomerIntelligence:
        row, effective = self._context(return_id, assessment_at)
        product_result = product.build_product_intelligence(self.repository, row, effective)
        return customer.build_customer_intelligence(
            self.repository, row, effective, product_result.category
        )

    def get_product_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> ProductIntelligence:
        row, effective = self._context(return_id, assessment_at)
        return product.build_product_intelligence(self.repository, row, effective)

    def get_return_behavior_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> ReturnBehaviorIntelligence:
        customer_result = self.get_customer_intelligence(return_id, assessment_at)
        return return_behavior.build_return_behavior(customer_result)

    def get_network_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> NetworkIntelligence:
        row, effective = self._context(return_id, assessment_at)
        return network.build_network_intelligence(self.repository, row, effective)

    def get_return_economics(self, return_id: str) -> ReturnEconomics:
        row, _ = self._context(return_id)
        return economics.build_return_economics(self.repository, row)

    def get_evidence(self, return_id: str) -> list[EvidenceRecord]:
        self._context(return_id)
        return evidence.get_evidence_records(self.repository, return_id)

    def get_inspection(self, return_id: str) -> InspectionRecord | None:
        self._context(return_id)
        return inspection.get_inspection_record(self.repository, return_id)

    def get_return_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> ReturnIntelligence:
        row, effective = self._context(return_id, assessment_at)
        product_result = product.build_product_intelligence(self.repository, row, effective)
        customer_result = customer.build_customer_intelligence(
            self.repository, row, effective, product_result.category
        )
        return ReturnIntelligence(
            return_metadata=self._metadata(row, effective),
            customer=customer_result,
            product=product_result,
            return_behavior=return_behavior.build_return_behavior(customer_result),
            network=network.build_network_intelligence(self.repository, row, effective),
            economics=economics.build_return_economics(self.repository, row),
            evidence=evidence.get_evidence_records(self.repository, return_id),
            inspection=inspection.get_inspection_record(self.repository, return_id),
        )
