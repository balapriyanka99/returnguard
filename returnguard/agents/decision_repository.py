"""Append-only persistence for finalized merchant-safe decision synthesis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Protocol

from google.cloud import bigquery
from pydantic import BaseModel

from returnguard.intelligence.config import IntelligenceConfig
from returnguard.observability import sanitize_structured_value

from .contracts import DecisionSynthesisResult


DECISION_VERSION = "decision-synthesis-v1"


def decision_event_id(
    result: DecisionSynthesisResult,
    decision_version: str = DECISION_VERSION,
) -> str:
    material = "|".join((
        result.return_id,
        result.assessment_id or "",
        result.assessment_at.isoformat(),
        decision_version,
    ))
    return "DECISION-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _safe_dump(value: Any) -> Any:
    return sanitize_structured_value(value)


class DecisionAssessment(BaseModel):
    """Flattened immutable row plus JSON-safe nested result projections."""

    decision_event_id: str
    return_id: str
    assessment_id: str | None = None
    assessment_at: datetime
    decision_version: str
    recommended_action: str
    matched_policy_rule: str
    risk_score: int | None = None
    risk_band: str
    risk_coverage: str
    decision_summary: str
    strongest_evidence: list[str]
    mitigating_context: list[str]
    network_context_json: dict[str, Any] | None = None
    economics_summary_json: dict[str, Any]
    pricing_json: dict[str, Any] | None = None
    policy_reasoning: list[str]
    limitations: list[str]
    specialists_used: list[str]
    data_origin: str

    @classmethod
    def from_result(
        cls,
        result: DecisionSynthesisResult,
        *,
        decision_version: str = DECISION_VERSION,
    ) -> "DecisionAssessment":
        value = _safe_dump(result.model_dump(mode="json"))
        network = value.get("network_context")
        pricing = value.get("pricing")
        origins = value.get("data_origin", [])
        return cls(
            decision_event_id=decision_event_id(result, decision_version),
            return_id=value["return_id"],
            assessment_id=value.get("assessment_id"),
            assessment_at=result.assessment_at,
            decision_version=decision_version,
            recommended_action=value["recommended_action"],
            matched_policy_rule=value["matched_policy_rule"],
            **{"risk_score": value.get("risk_score")},
            risk_band=value["risk_band"],
            risk_coverage=value["risk_coverage"],
            decision_summary=value["decision_summary"],
            strongest_evidence=list(value.get("strongest_evidence", [])),
            mitigating_context=list(value.get("mitigating_context", [])),
            network_context_json=network,
            economics_summary_json=dict(value["economics_summary"]),
            pricing_json=pricing,
            policy_reasoning=list(value.get("policy_reasoning", [])),
            limitations=list(value.get("limitations", [])),
            specialists_used=list(value.get("specialists_used", [])),
            data_origin=json.dumps(origins, separators=(",", ":")),
        )


class DecisionAssessmentRepository(Protocol):
    def append(self, assessment: DecisionAssessment) -> None: ...
    def get(self, event_id: str) -> dict[str, Any] | None: ...


class BigQueryDecisionAssessmentRepository:
    """Insert one immutable decision row; retries with the same ID are no-ops."""

    def __init__(
        self,
        config: IntelligenceConfig | None = None,
        client: bigquery.Client | None = None,
    ) -> None:
        self.config = config or IntelligenceConfig.from_env()
        self.client = client or bigquery.Client(project=self.config.project_id)

    @property
    def table(self) -> str:
        return f"{self.config.returnguard_dataset}.return_decisions"

    def append(self, assessment: DecisionAssessment) -> None:
        sql = f"""
        MERGE `{self.table}` target
        USING (SELECT @decision_event_id AS decision_event_id) source
        ON target.decision_event_id = source.decision_event_id
        WHEN NOT MATCHED THEN INSERT (
          decision_event_id, return_id, assessment_id, assessment_at,
          decision_version, recommended_action, matched_policy_rule,
          risk_score, risk_band, risk_coverage, decision_summary,
          strongest_evidence, mitigating_context, network_context_json,
          economics_summary_json, pricing_json, policy_reasoning,
          limitations, specialists_used, data_origin, created_at
        ) VALUES (
          @decision_event_id, @return_id, @assessment_id, @assessment_at,
          @decision_version, @recommended_action, @matched_policy_rule,
          @risk_score, @risk_band, @risk_coverage, @decision_summary,
          @strongest_evidence, @mitigating_context,
          IF(@network_context_json IS NULL, NULL, PARSE_JSON(@network_context_json)),
          PARSE_JSON(@economics_summary_json),
          IF(@pricing_json IS NULL, NULL, PARSE_JSON(@pricing_json)),
          @policy_reasoning, @limitations, @specialists_used, @data_origin,
          CURRENT_TIMESTAMP()
        )
        """
        parameters = [
            bigquery.ScalarQueryParameter("decision_event_id", "STRING", assessment.decision_event_id),
            bigquery.ScalarQueryParameter("return_id", "STRING", assessment.return_id),
            bigquery.ScalarQueryParameter("assessment_id", "STRING", assessment.assessment_id),
            bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment.assessment_at),
            bigquery.ScalarQueryParameter("decision_version", "STRING", assessment.decision_version),
            bigquery.ScalarQueryParameter("recommended_action", "STRING", assessment.recommended_action),
            bigquery.ScalarQueryParameter("matched_policy_rule", "STRING", assessment.matched_policy_rule),
            bigquery.ScalarQueryParameter("risk_score", "INT64", assessment.risk_score),
            bigquery.ScalarQueryParameter("risk_band", "STRING", assessment.risk_band),
            bigquery.ScalarQueryParameter("risk_coverage", "STRING", assessment.risk_coverage),
            bigquery.ScalarQueryParameter("decision_summary", "STRING", assessment.decision_summary),
            bigquery.ArrayQueryParameter("strongest_evidence", "STRING", assessment.strongest_evidence),
            bigquery.ArrayQueryParameter("mitigating_context", "STRING", assessment.mitigating_context),
            bigquery.ScalarQueryParameter(
                "network_context_json", "STRING",
                None if assessment.network_context_json is None else json.dumps(assessment.network_context_json),
            ),
            bigquery.ScalarQueryParameter(
                "economics_summary_json", "STRING", json.dumps(assessment.economics_summary_json)
            ),
            bigquery.ScalarQueryParameter(
                "pricing_json", "STRING",
                None if assessment.pricing_json is None else json.dumps(assessment.pricing_json),
            ),
            bigquery.ArrayQueryParameter("policy_reasoning", "STRING", assessment.policy_reasoning),
            bigquery.ArrayQueryParameter("limitations", "STRING", assessment.limitations),
            bigquery.ArrayQueryParameter("specialists_used", "STRING", assessment.specialists_used),
            bigquery.ScalarQueryParameter("data_origin", "STRING", assessment.data_origin),
        ]
        getattr(self.client, "query")(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result()

    def get(self, event_id: str) -> dict[str, Any] | None:
        sql = f"""
        SELECT * FROM `{self.table}`
        WHERE decision_event_id = @decision_event_id
        LIMIT 1
        """
        rows = getattr(self.client, "query")(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("decision_event_id", "STRING", event_id)
            ]),
        ).result()
        row = next(iter(rows), None)
        return None if row is None else dict(row.items())
