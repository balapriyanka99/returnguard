"""Append-only, retry-safe BigQuery persistence for economics assessments."""

from __future__ import annotations

from typing import Any, Protocol

from google.cloud import bigquery

from returnguard.intelligence.config import IntelligenceConfig

from .models import EconomicsAssessment


class EconomicsAssessmentRepository(Protocol):
    def append(self, assessment: EconomicsAssessment) -> None: ...
    def get(self, economics_event_id: str) -> dict[str, Any] | None: ...


class BigQueryEconomicsAssessmentRepository:
    """Insert an immutable event once; same-ID retries are no-ops."""

    def __init__(
        self,
        config: IntelligenceConfig | None = None,
        client: bigquery.Client | None = None,
    ) -> None:
        self.config = config or IntelligenceConfig.from_env()
        self.client = client or bigquery.Client(project=self.config.project_id)

    @property
    def table(self) -> str:
        return f"{self.config.returnguard_dataset}.economics_assessments"

    def append(self, assessment: EconomicsAssessment) -> None:
        sql = f"""
        MERGE `{self.table}` target
        USING (SELECT @economics_event_id AS economics_event_id) source
        ON target.economics_event_id = source.economics_event_id
        WHEN NOT MATCHED THEN INSERT (
          economics_event_id, return_id, assessment_id, assessment_at,
          economics_version, current_item_value, product_cost,
          reverse_logistics_cost, inspection_cost, recovery_value,
          total_operational_cost, estimated_net_return_cost,
          estimated_loss_exposure, confidence, data_origin, created_at
        ) VALUES (
          @economics_event_id, @return_id, @assessment_id, @assessment_at,
          @economics_version, @current_item_value, @product_cost,
          @reverse_logistics_cost, @inspection_cost, @recovery_value,
          @total_operational_cost, @estimated_net_return_cost,
          @estimated_loss_exposure, @confidence, @data_origin,
          CURRENT_TIMESTAMP()
        )
        """
        parameters = [
            bigquery.ScalarQueryParameter("economics_event_id", "STRING", assessment.economics_event_id),
            bigquery.ScalarQueryParameter("return_id", "STRING", assessment.return_id),
            bigquery.ScalarQueryParameter("assessment_id", "STRING", assessment.assessment_id),
            bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment.assessment_at),
            bigquery.ScalarQueryParameter("economics_version", "STRING", assessment.economics_version),
            bigquery.ScalarQueryParameter("current_item_value", "NUMERIC", assessment.current_item_value),
            bigquery.ScalarQueryParameter("product_cost", "NUMERIC", assessment.product_cost),
            bigquery.ScalarQueryParameter("reverse_logistics_cost", "NUMERIC", assessment.reverse_logistics_cost),
            bigquery.ScalarQueryParameter("inspection_cost", "NUMERIC", assessment.inspection_cost),
            bigquery.ScalarQueryParameter("recovery_value", "NUMERIC", assessment.recovery_value),
            bigquery.ScalarQueryParameter("total_operational_cost", "NUMERIC", assessment.total_operational_cost),
            bigquery.ScalarQueryParameter("estimated_net_return_cost", "NUMERIC", assessment.estimated_net_return_cost),
            bigquery.ScalarQueryParameter("estimated_loss_exposure", "NUMERIC", assessment.estimated_loss_exposure),
            bigquery.ScalarQueryParameter("confidence", "STRING", assessment.confidence),
            bigquery.ScalarQueryParameter("data_origin", "STRING", assessment.data_origin),
        ]
        self.client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result()

    def get(self, economics_event_id: str) -> dict[str, Any] | None:
        sql = f"""
        SELECT * FROM `{self.table}`
        WHERE economics_event_id = @economics_event_id
        LIMIT 1
        """
        rows = self.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter(
                    "economics_event_id", "STRING", economics_event_id
                )
            ]),
        ).result()
        row = next(iter(rows), None)
        return None if row is None else dict(row.items())
