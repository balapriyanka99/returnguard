"""Append-only, retry-safe BigQuery persistence for policy assessments."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from google.cloud import bigquery

from returnguard.intelligence.config import IntelligenceConfig

from .models import PolicyEvaluation


def policy_event_id(evaluation: PolicyEvaluation) -> str:
    material = "|".join((
        evaluation.return_id,
        evaluation.assessment_id or "",
        evaluation.assessment_at.isoformat(),
        evaluation.policy_version,
    ))
    return "POLICY-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class PolicyAssessmentRepository(Protocol):
    def append(self, evaluation: PolicyEvaluation) -> None: ...
    def get(self, event_id: str) -> dict[str, Any] | None: ...


class BigQueryPolicyAssessmentRepository:
    """Insert an immutable policy event once; same-ID retries are no-ops."""

    def __init__(
        self,
        config: IntelligenceConfig | None = None,
        client: bigquery.Client | None = None,
    ) -> None:
        self.config = config or IntelligenceConfig.from_env()
        self.client = client or bigquery.Client(project=self.config.project_id)

    @property
    def table(self) -> str:
        return f"{self.config.returnguard_dataset}.policy_assessments"

    def append(self, evaluation: PolicyEvaluation) -> None:
        event_id = policy_event_id(evaluation)
        economics_json = json.dumps(evaluation.economics.model_dump(mode="json"))
        pricing_json = (
            None
            if evaluation.pricing is None
            else json.dumps(evaluation.pricing.model_dump(mode="json"))
        )
        sql = f"""
        MERGE `{self.table}` target
        USING (SELECT @policy_event_id AS policy_event_id) source
        ON target.policy_event_id = source.policy_event_id
        WHEN NOT MATCHED THEN INSERT (
          policy_event_id, return_id, assessment_id, assessment_at,
          policy_version, matched_rule, action, normalized_reason,
          return_fee, fee_reason, economics_json, pricing_json,
          rationale, created_at
        ) VALUES (
          @policy_event_id, @return_id, @assessment_id, @assessment_at,
          @policy_version, @matched_rule, @action, @normalized_reason,
          @return_fee, @fee_reason, PARSE_JSON(@economics_json),
          IF(@pricing_json IS NULL, NULL, PARSE_JSON(@pricing_json)),
          @rationale, CURRENT_TIMESTAMP()
        )
        """
        parameters = [
            bigquery.ScalarQueryParameter("policy_event_id", "STRING", event_id),
            bigquery.ScalarQueryParameter("return_id", "STRING", evaluation.return_id),
            bigquery.ScalarQueryParameter("assessment_id", "STRING", evaluation.assessment_id),
            bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", evaluation.assessment_at),
            bigquery.ScalarQueryParameter("policy_version", "STRING", evaluation.policy_version),
            bigquery.ScalarQueryParameter("matched_rule", "STRING", evaluation.matched_rule),
            bigquery.ScalarQueryParameter("action", "STRING", evaluation.action.value),
            bigquery.ScalarQueryParameter("normalized_reason", "STRING", evaluation.normalized_reason.value),
            bigquery.ScalarQueryParameter("return_fee", "NUMERIC", evaluation.return_fee),
            bigquery.ScalarQueryParameter("fee_reason", "STRING", evaluation.fee_reason),
            bigquery.ScalarQueryParameter("economics_json", "STRING", economics_json),
            bigquery.ScalarQueryParameter("pricing_json", "STRING", pricing_json),
            bigquery.ArrayQueryParameter("rationale", "STRING", evaluation.rationale),
        ]
        self.client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result()

    def get(self, event_id: str) -> dict[str, Any] | None:
        sql = f"""
        SELECT * FROM `{self.table}`
        WHERE policy_event_id = @policy_event_id
        LIMIT 1
        """
        rows = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("policy_event_id", "STRING", event_id)
            ]),
        ).result()
        row = next(iter(rows), None)
        return None if row is None else dict(row.items())
