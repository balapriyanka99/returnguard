"""Thin facade and aggregate entry point for Intelligence capabilities."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from returnguard.observability import logged_operation

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


logger = logging.getLogger(__name__)


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

    def resolve_assessment_at(
        self, return_id: str, assessment_at: datetime | None = None
    ) -> datetime:
        """Return the caller override or the case's stored assessment timestamp."""

        return self._context(return_id, assessment_at)[1]

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
        with logged_operation(
            logger, operation_type="intelligence_capability",
            operation_name="customer", return_id=return_id, assessment_at=assessment_at,
        ) as log_result:
            row, effective = self._context(return_id, assessment_at)
            product_result = product.build_product_intelligence(self.repository, row, effective)
            result = customer.build_customer_intelligence(
                self.repository, row, effective, product_result.category
            )
            log_result["assessment_at"] = effective.isoformat()
            log_result["data_state"] = result.customer_history_data_origin
            return result

    def get_product_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> ProductIntelligence:
        with logged_operation(
            logger, operation_type="intelligence_capability",
            operation_name="product", return_id=return_id, assessment_at=assessment_at,
        ) as log_result:
            row, effective = self._context(return_id, assessment_at)
            result = product.build_product_intelligence(self.repository, row, effective)
            log_result["assessment_at"] = effective.isoformat()
            log_result["data_state"] = result.data_origin
            return result

    def get_return_behavior_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> ReturnBehaviorIntelligence:
        with logged_operation(
            logger, operation_type="intelligence_capability",
            operation_name="return_behavior", return_id=return_id,
            assessment_at=assessment_at,
        ) as log_result:
            customer_result = self.get_customer_intelligence(return_id, assessment_at)
            result = return_behavior.build_return_behavior(customer_result)
            log_result["assessment_at"] = customer_result.assessment_at.isoformat()
            log_result["data_state"] = result.data_origin
            return result

    def get_network_intelligence(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
    ) -> NetworkIntelligence:
        with logged_operation(
            logger, operation_type="intelligence_capability",
            operation_name="network", return_id=return_id, assessment_at=assessment_at,
        ) as log_result:
            row, effective = self._context(return_id, assessment_at)
            result = network.build_network_intelligence(self.repository, row, effective)
            log_result["assessment_at"] = effective.isoformat()
            log_result["data_state"] = result.data_origin
            return result

    def get_return_economics(
        self, return_id: str, assessment_at: datetime | None = None
    ) -> ReturnEconomics:
        with logged_operation(
            logger, operation_type="intelligence_capability",
            operation_name="economics", return_id=return_id, assessment_at=assessment_at,
        ) as log_result:
            row, effective = self._context(return_id, assessment_at)
            result = economics.build_return_economics(self.repository, row, effective)
            log_result["assessment_at"] = effective.isoformat()
            log_result["data_state"] = result.data_origin
            return result

    def get_evidence(
        self, return_id: str, assessment_at: datetime | None = None
    ) -> list[EvidenceRecord]:
        with logged_operation(
            logger, operation_type="intelligence_capability",
            operation_name="evidence", return_id=return_id, assessment_at=assessment_at,
        ) as log_result:
            _, effective = self._context(return_id, assessment_at)
            result = evidence.get_evidence_records(self.repository, return_id, effective)
            log_result["assessment_at"] = effective.isoformat()
            log_result["evidence_available"] = bool(result)
            return result

    def get_inspection(
        self, return_id: str, assessment_at: datetime | None = None
    ) -> InspectionRecord | None:
        with logged_operation(
            logger, operation_type="intelligence_capability",
            operation_name="inspection", return_id=return_id, assessment_at=assessment_at,
        ) as log_result:
            _, effective = self._context(return_id, assessment_at)
            result = inspection.get_inspection_record(self.repository, return_id, effective)
            log_result["assessment_at"] = effective.isoformat()
            log_result["inspection_available"] = result is not None
            log_result["serial_comparison_performed"] = bool(
                result and result.expected_serial is not None and result.returned_serial is not None
            )
            log_result["accessory_comparison_performed"] = bool(
                result and (result.expected_accessories or result.accessories_present)
            )
            return result

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
            economics=economics.build_return_economics(self.repository, row, effective),
            evidence=evidence.get_evidence_records(self.repository, return_id, effective),
            inspection=inspection.get_inspection_record(self.repository, return_id, effective),
        )
