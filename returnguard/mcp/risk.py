"""Dependency boundary for the not-yet-implemented deterministic Risk Engine."""

from __future__ import annotations

from typing import Protocol

from .contracts import RiskAssessmentRequest, RiskAssessmentResult


class RiskEngine(Protocol):
    def assess_risk(self, request: RiskAssessmentRequest) -> RiskAssessmentResult: ...


class RiskEngineUnavailableError(RuntimeError):
    """Raised when assess_risk is called before an engine is injected."""
