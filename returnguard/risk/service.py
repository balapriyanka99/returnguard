"""Composition boundary from deterministic Intelligence to Risk-v1."""

from __future__ import annotations

import logging
from datetime import datetime

from returnguard.intelligence import ReturnIntelligenceService
from returnguard.observability import logged_operation, log_risk_summary

from .engine import RiskV1Engine
from .models import RiskAssessment
from .repository import RiskEventRepository


logger = logging.getLogger(__name__)


class ReturnRiskService:
    def __init__(
        self,
        intelligence: ReturnIntelligenceService,
        engine: RiskV1Engine | None = None,
        event_repository: RiskEventRepository | None = None,
    ) -> None:
        self.intelligence = intelligence
        self.engine = engine or RiskV1Engine()
        self.event_repository = event_repository

    def assess(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
        *,
        assessment_id: str | None = None,
    ) -> RiskAssessment:
        effective = self.intelligence.resolve_assessment_at(return_id, assessment_at)
        with logged_operation(
            logger,
            operation_type="risk_engine",
            operation_name="risk-v1",
            return_id=return_id,
            assessment_at=effective,
        ):
            customer = self.intelligence.get_customer_intelligence(return_id, effective)
            behavior = self.intelligence.get_return_behavior_intelligence(return_id, effective)
            product = self.intelligence.get_product_intelligence(return_id, effective)
            inspection = self.intelligence.get_inspection(return_id, effective)
            evidence = self.intelligence.get_evidence(return_id, effective)
            network = self.intelligence.get_network_intelligence(return_id, effective)
            result = self.engine.assess(
                return_id=return_id,
                assessment_id=assessment_id,
                assessment_at=effective,
                customer=customer,
                behavior=behavior,
                product=product,
                inspection=inspection,
                evidence=evidence,
                network=network,
            )
            log_risk_summary(logger, result)
            return result

    def assess_and_record(
        self,
        return_id: str,
        assessment_at: datetime | None = None,
        *,
        assessment_id: str | None = None,
    ) -> RiskAssessment:
        if self.event_repository is None:
            raise RuntimeError("A RiskEventRepository is required to persist risk events")
        assessment = self.assess(
            return_id, assessment_at, assessment_id=assessment_id
        )
        self.event_repository.append(assessment)
        return assessment
