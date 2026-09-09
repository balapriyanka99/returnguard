"""Parameterized BigQuery access for ReturnGuard intelligence."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from google.cloud import bigquery

from returnguard.observability import logged_operation

from .config import IntelligenceConfig


logger = logging.getLogger(__name__)


class IntelligenceRepository(Protocol):
    def get_return(self, return_id: str) -> dict[str, Any] | None: ...
    def get_customer_history(self, user_id: int, product_id: int, category: str | None, assessment_at: datetime, current_order_item_id: int) -> dict[str, Any]: ...
    def get_product_history(self, product_id: int, assessment_at: datetime, current_order_item_id: int) -> dict[str, Any]: ...
    def get_network_links(self, return_id: str, assessment_at: datetime) -> list[dict[str, Any]]: ...
    def get_evidence(self, return_id: str, assessment_at: datetime) -> list[dict[str, Any]]: ...
    def get_inspection(self, return_id: str, assessment_at: datetime) -> dict[str, Any] | None: ...
    def get_economics(self, return_id: str, assessment_at: datetime) -> dict[str, Any]: ...


class BigQueryIntelligenceRepository:
    def __init__(self, config: IntelligenceConfig | None = None, client: bigquery.Client | None = None) -> None:
        self.config = config or IntelligenceConfig.from_env()
        self.client = client or bigquery.Client(project=self.config.project_id)

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return dict(row.items())

    def _one(
        self,
        operation: str,
        sql: str,
        parameters: list[bigquery.ScalarQueryParameter],
        *,
        source_role: str | None = None,
    ) -> dict[str, Any] | None:
        with logged_operation(
            logger, operation_type="repository_query", operation_name=operation,
            source_role=source_role,
        ) as log_result:
            job = self.client.query(
                sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
            )
            row = next(iter(job.result()), None)
            log_result["row_count"] = 0 if row is None else 1
            return None if row is None else self._row(row)

    def _all(
        self,
        operation: str,
        sql: str,
        parameters: list[bigquery.ScalarQueryParameter],
        *,
        source_role: str | None = None,
    ) -> list[dict[str, Any]]:
        with logged_operation(
            logger, operation_type="repository_query", operation_name=operation,
            source_role=source_role,
        ) as log_result:
            job = self.client.query(
                sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
            )
            rows = [self._row(row) for row in job.result()]
            log_result["row_count"] = len(rows)
            return rows

    def get_return(self, return_id: str) -> dict[str, Any] | None:
        rg = self.config.returnguard_dataset
        sql = f"""
        SELECT return_id, scenario_id, generator_version, user_id, order_item_id,
               order_id, product_id, assessment_at, requested_at, reason,
               responsibility, status, source_type, anchor_sale_price,
               anchor_product_cost, customer_historical_return_count
        FROM `{rg}.return_requests`
        WHERE return_id = @return_id
        LIMIT 1
        """
        return self._one(
            "get_return", sql,
            [bigquery.ScalarQueryParameter("return_id", "STRING", return_id)],
            source_role="returnguard_active",
        )

    def get_product_category(self, product_id: int) -> str | None:
        row = self._one(
            "get_product_category",
            f"SELECT category FROM `{self.config.source_products}` WHERE id = @product_id LIMIT 1",
            [bigquery.ScalarQueryParameter("product_id", "INT64", product_id)],
            source_role="frozen_products_snapshot",
        )
        return None if row is None else row.get("category")

    def get_customer_history(self, user_id: int, product_id: int, category: str | None, assessment_at: datetime, current_order_item_id: int) -> dict[str, Any]:
        order_items = self.config.source_order_items
        products = self.config.source_products
        sql = f"""
        WITH eligible AS (
          SELECT oi.id, oi.order_id, oi.product_id, oi.sale_price, oi.created_at,
                 oi.returned_at, LOWER(CAST(oi.status AS STRING)) AS item_status,
                 p.category
          FROM `{order_items}` oi
          LEFT JOIN `{products}` p ON p.id = oi.product_id
          WHERE oi.user_id = @user_id
            AND oi.created_at <= @assessment_at
            AND oi.id != @current_order_item_id
            AND LOWER(CAST(oi.status AS STRING)) != 'cancelled'
        ), facts AS (
          SELECT id, order_id, product_id, sale_price, created_at, returned_at,
            item_status, category,
            item_status = 'returned' AND returned_at IS NOT NULL
              AND returned_at <= @assessment_at AS returned_as_of_assessment
          FROM eligible
        ), purchase_category AS (
          SELECT category, COUNT(*) AS item_count
          FROM facts WHERE category IS NOT NULL GROUP BY category
          ORDER BY item_count DESC, category LIMIT 1
        ), return_category AS (
          SELECT category, COUNT(*) AS return_count
          FROM facts WHERE returned_as_of_assessment AND category IS NOT NULL
          GROUP BY category ORDER BY return_count DESC, category LIMIT 1
        )
        SELECT
          COUNT(DISTINCT order_id) AS total_orders,
          COUNT(*) AS total_items,
          COALESCE(SUM(sale_price), 0) AS total_spend,
          SAFE_DIVIDE(SUM(sale_price), COUNT(DISTINCT order_id)) AS average_order_value,
          AVG(sale_price) AS average_item_value,
          MIN(created_at) AS first_observed_purchase_at,
          MAX(created_at) AS last_purchase_at,
          COUNT(DISTINCT product_id) AS distinct_products_purchased,
          COUNT(DISTINCT category) AS distinct_categories_purchased,
          COUNT(DISTINCT IF(created_at >= TIMESTAMP_SUB(@assessment_at, INTERVAL 30 DAY), order_id, NULL)) AS orders_30d,
          COUNTIF(created_at >= TIMESTAMP_SUB(@assessment_at, INTERVAL 30 DAY)) AS items_30d,
          COUNT(DISTINCT IF(created_at >= TIMESTAMP_SUB(@assessment_at, INTERVAL 90 DAY), order_id, NULL)) AS orders_90d,
          COUNTIF(created_at >= TIMESTAMP_SUB(@assessment_at, INTERVAL 90 DAY)) AS items_90d,
          COUNTIF(returned_as_of_assessment) AS live_return_count,
          COUNTIF(returned_as_of_assessment AND returned_at >= TIMESTAMP_SUB(@assessment_at, INTERVAL 30 DAY)) AS returns_30d,
          COUNTIF(returned_as_of_assessment AND returned_at >= TIMESTAMP_SUB(@assessment_at, INTERVAL 90 DAY)) AS returns_90d,
          MAX(IF(returned_as_of_assessment, returned_at, NULL)) AS last_return_at,
          COALESCE(SUM(IF(returned_as_of_assessment, sale_price, 0)), 0) AS returned_value,
          COUNT(DISTINCT IF(returned_as_of_assessment, product_id, NULL)) AS distinct_returned_products,
          COUNT(DISTINCT IF(returned_as_of_assessment, category, NULL)) AS distinct_returned_categories,
          COUNTIF(returned_as_of_assessment AND product_id = @product_id) AS same_product_return_count,
          COUNTIF(returned_as_of_assessment AND category = @category) AS same_category_return_count,
          AVG(IF(returned_as_of_assessment, TIMESTAMP_DIFF(returned_at, created_at, DAY), NULL)) AS purchase_to_return_timing_days,
          (SELECT category FROM purchase_category) AS top_purchase_category,
          (SELECT item_count FROM purchase_category) AS top_purchase_category_item_count,
          (SELECT category FROM return_category) AS top_returned_category,
          (SELECT return_count FROM return_category) AS top_returned_category_count
        FROM facts
        """
        params = [
            bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
            bigquery.ScalarQueryParameter("product_id", "INT64", product_id),
            bigquery.ScalarQueryParameter("category", "STRING", category),
            bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment_at),
            bigquery.ScalarQueryParameter("current_order_item_id", "INT64", current_order_item_id),
        ]
        return self._one(
            "get_customer_history", sql, params, source_role="frozen_source_history"
        ) or {}

    def get_product_history(self, product_id: int, assessment_at: datetime, current_order_item_id: int) -> dict[str, Any]:
        order_items = self.config.source_order_items
        products = self.config.source_products
        sql = f"""
        WITH current_product AS (
          SELECT category FROM `{products}` WHERE id = @product_id LIMIT 1
        ), eligible AS (
          SELECT oi.id, oi.product_id, oi.sale_price, oi.created_at, oi.returned_at,
                 LOWER(CAST(oi.status AS STRING)) AS item_status, p.category
          FROM `{order_items}` oi
          JOIN `{products}` p ON p.id = oi.product_id
          WHERE oi.created_at <= @assessment_at
            AND oi.id != @current_order_item_id
            AND LOWER(CAST(oi.status AS STRING)) != 'cancelled'
            AND (oi.product_id = @product_id OR p.category = (SELECT category FROM current_product))
        ), facts AS (
          SELECT id, product_id, sale_price, created_at, returned_at, item_status,
            category, item_status = 'returned' AND returned_at IS NOT NULL
              AND returned_at <= @assessment_at AS returned_as_of_assessment
          FROM eligible
        )
        SELECT
          (SELECT category FROM current_product) AS category,
          COUNTIF(product_id = @product_id) AS product_items_observed,
          COUNTIF(product_id = @product_id AND returned_as_of_assessment) AS product_returns_observed,
          COALESCE(SUM(IF(product_id = @product_id AND returned_as_of_assessment, sale_price, 0)), 0) AS product_returned_value,
          AVG(IF(product_id = @product_id, sale_price, NULL)) AS product_average_sale_price,
          MAX(IF(product_id = @product_id AND returned_as_of_assessment, returned_at, NULL)) AS product_last_return_at,
          COUNTIF(category = (SELECT category FROM current_product)) AS category_items_observed,
          COUNTIF(category = (SELECT category FROM current_product) AND returned_as_of_assessment) AS category_returns_observed,
          COALESCE(SUM(IF(category = (SELECT category FROM current_product) AND returned_as_of_assessment, sale_price, 0)), 0) AS category_returned_value,
          AVG(IF(category = (SELECT category FROM current_product), sale_price, NULL)) AS category_average_sale_price
        FROM facts
        """
        return self._one("get_product_history", sql, [
            bigquery.ScalarQueryParameter("product_id", "INT64", product_id),
            bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment_at),
            bigquery.ScalarQueryParameter("current_order_item_id", "INT64", current_order_item_id),
        ], source_role="frozen_source_history") or {}

    def get_network_links(self, return_id: str, assessment_at: datetime) -> list[dict[str, Any]]:
        rg = self.config.returnguard_dataset
        sql = f"""
        WITH current_identifiers AS (
          SELECT DISTINCT network_identifier FROM `{rg}.synthetic_network_links`
          WHERE return_id = @return_id AND first_observed_at <= @assessment_at
        )
        SELECT network_link_id, scenario_id, return_id, user_id, linked_user_id,
               network_identifier, relationship_type, first_observed_at,
               IF(last_observed_at <= @assessment_at, last_observed_at, NULL) AS last_observed_at,
               source_type, generator_version
        FROM `{rg}.synthetic_network_links`
        WHERE network_identifier IN (SELECT network_identifier FROM current_identifiers)
          AND first_observed_at <= @assessment_at
        ORDER BY first_observed_at, network_link_id
        """
        return self._all("get_network_links", sql, [
            bigquery.ScalarQueryParameter("return_id", "STRING", return_id),
            bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment_at),
        ], source_role="controlled_synthetic_network_links")

    def get_evidence(self, return_id: str, assessment_at: datetime) -> list[dict[str, Any]]:
        rg = self.config.returnguard_dataset
        sql = f"""
        SELECT evidence_id, return_id, stage, type, image_uri,
               reference_image_uri, observed_at, source_type, uploaded_by,
               claim_metadata, scenario_id, generator_version
        FROM `{rg}.return_evidence`
        WHERE return_id = @return_id
          AND observed_at <= @assessment_at
        ORDER BY observed_at, evidence_id
        """
        return self._all(
            "get_evidence", sql,
            [
                bigquery.ScalarQueryParameter("return_id", "STRING", return_id),
                bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment_at),
            ],
            source_role="returnguard_evidence",
        )

    def get_inspection(self, return_id: str, assessment_at: datetime) -> dict[str, Any] | None:
        rg = self.config.returnguard_dataset
        sql = f"""
        SELECT i.return_id, r.product_id, i.actual_weight_kg,
               i.expected_serial, i.returned_serial, i.item_present, i.condition,
               i.accessories_present, p.expected_accessories,
               i.inspection_location, i.inspected_at, i.expected_weight_kg,
               i.source_type,
               p.source_type AS product_attributes_source_type,
               p.generator_version AS product_attributes_generator_version,
               i.scenario_id, i.generator_version
        FROM `{rg}.return_inspections` i
        LEFT JOIN `{rg}.return_requests` r USING (return_id)
        LEFT JOIN `{rg}.product_attributes` p ON p.product_id = r.product_id
        WHERE i.return_id = @return_id
          AND i.inspected_at <= @assessment_at
        LIMIT 1
        """
        return self._one(
            "get_inspection", sql,
            [
                bigquery.ScalarQueryParameter("return_id", "STRING", return_id),
                bigquery.ScalarQueryParameter("assessment_at", "TIMESTAMP", assessment_at),
            ],
            source_role="returnguard_inspection",
        )

    def get_economics(self, return_id: str, assessment_at: datetime) -> dict[str, Any]:
        rg = self.config.returnguard_dataset
        sql = f"""
        SELECT r.anchor_sale_price AS current_item_value,
               r.anchor_product_cost AS product_cost,
               l.reverse_cost AS logistics_reverse_cost,
               c.reverse_logistics_cost, c.inspection_cost, c.recovery_value,
               c.source_type
        FROM `{rg}.return_requests` r
        LEFT JOIN `{rg}.return_logistics` l USING (return_id)
        LEFT JOIN `{rg}.return_operational_costs` c USING (return_id)
        WHERE r.return_id = @return_id LIMIT 1
        """
        return self._one(
            "get_economics", sql,
            [bigquery.ScalarQueryParameter("return_id", "STRING", return_id)],
            source_role="returnguard_economics",
        ) or {}
