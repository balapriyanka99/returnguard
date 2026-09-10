"""Append-only, retry-safe persistence boundary for deterministic risk events."""

from __future__ import annotations

import json
from typing import Protocol

from google.cloud import bigquery

from returnguard.intelligence.config import IntelligenceConfig

from .models import RiskAssessment


class RiskEventRepository(Protocol):
    def append(self, assessment: RiskAssessment) -> None: ...


class BigQueryRiskEventRepository:
    """Insert an immutable event once; retries with the same event ID are no-ops."""

    def __init__(
        self,
        config: IntelligenceConfig | None = None,
        client: bigquery.Client | None = None,
    ) -> None:
        self.config = config or IntelligenceConfig.from_env()
        self.client = client or bigquery.Client(project=self.config.project_id)

    def append(self, assessment: RiskAssessment) -> None:
        table = f"{self.config.returnguard_dataset}.risk_events"
        sql = f"""
        MERGE `{table}` target
        USING (SELECT @risk_event_id AS risk_event_id) source
        ON target.risk_event_id = source.risk_event_id
        WHEN NOT MATCHED THEN INSERT (
          risk_event_id, return_id, assessment_id, assessment_at, engine_version,
          score, band, coverage, evaluable_domains, group_scores_json,
          product_mitigation, reasons_json, patterns, limitations, data_origin,
          created_at
        ) VALUES (
          @risk_event_id, @return_id, @assessment_id, @assessment_at, @engine_version,
          @score, @band, @coverage, @evaluable_domains, PARSE_JSON(@group_scores_json),
          @product_mitigation, PARSE_JSON(@reasons_json), @patterns, @limitations, @data_origin,
          CURRENT_TIMESTAMP()
        )
        """
        parameters = [
            bigquery.ScalarQueryParameter("risk_event_id", "STRING", assessment.risk_event_id),
            bigquery.ScalarQueryParameter("return_id", "STRING", assessment.return_id),
            bigquery.ScalarQueryParameter("assessment_id", "STRING", assessment.assessment_id),
            bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment.assessment_at),
            bigquery.ScalarQueryParameter("engine_version", "STRING", assessment.engine_version),
            bigquery.ScalarQueryParameter("score", "INT64", assessment.score),
            bigquery.ScalarQueryParameter("band", "STRING", assessment.band.value),
            bigquery.ScalarQueryParameter("coverage", "STRING", assessment.coverage.value),
            bigquery.ArrayQueryParameter("evaluable_domains", "STRING", assessment.evaluable_domains),
            bigquery.ScalarQueryParameter("group_scores_json", "STRING", json.dumps(assessment.group_scores)),
            bigquery.ScalarQueryParameter("product_mitigation", "INT64", assessment.product_mitigation),
            bigquery.ScalarQueryParameter(
                "reasons_json", "STRING",
                json.dumps([reason.model_dump(mode="json") for reason in assessment.reasons]),
            ),
            bigquery.ArrayQueryParameter("patterns", "STRING", [item.value for item in assessment.patterns]),
            bigquery.ArrayQueryParameter("limitations", "STRING", assessment.limitations),
            bigquery.ScalarQueryParameter("data_origin", "STRING", assessment.data_origin),
        ]
        self.client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result()
