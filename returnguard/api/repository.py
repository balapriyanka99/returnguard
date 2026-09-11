"""Read-only projections of persisted assessment artifacts for the HTTP API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from google.cloud import bigquery

from returnguard.intelligence.config import IntelligenceConfig


class ApiReadRepository(Protocol):
    def get_return(self, return_id: str) -> dict[str, Any] | None: ...
    def latest_for_return(self, return_id: str) -> dict[str, Any]: ...
    def assessment_history(self, return_id: str) -> list[dict[str, Any]]: ...
    def source_customers(self, search: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...
    def source_orders(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]: ...
    def source_order_items(self, order_id: int) -> list[dict[str, Any]]: ...
    def create_return(self, *, return_id: str, order_item_id: int, customer_id: int | None,
                      order_id: int | None, reason: str, comment: str | None) -> dict[str, Any]: ...


class BigQueryApiReadRepository:
    def __init__(self, config: IntelligenceConfig | None = None, client: bigquery.Client | None = None) -> None:
        self.config = config or IntelligenceConfig.from_env()
        self.client = client or bigquery.Client(project=self.config.project_id)

    def _rows(self, sql: str, return_id: str) -> list[dict[str, Any]]:
        rows = self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("return_id", "STRING", return_id)
        ])).result()
        def plain(value: Any) -> Any:
            if hasattr(value, "items"):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, list):
                return [plain(item) for item in value]
            return value
        return [plain(row) for row in rows]

    def latest_for_return(self, return_id: str) -> dict[str, Any]:
        dataset = self.config.returnguard_dataset
        sql = f"""
        SELECT
          (SELECT AS STRUCT * FROM `{dataset}.risk_events`
           WHERE return_id = @return_id ORDER BY assessment_at DESC, created_at DESC LIMIT 1) AS risk,
          (SELECT AS STRUCT * FROM `{dataset}.economics_assessments`
           WHERE return_id = @return_id ORDER BY assessment_at DESC, created_at DESC LIMIT 1) AS economics,
          (SELECT AS STRUCT * FROM `{dataset}.policy_assessments`
           WHERE return_id = @return_id ORDER BY assessment_at DESC, created_at DESC LIMIT 1) AS policy,
          (SELECT AS STRUCT * FROM `{dataset}.return_decisions`
           WHERE return_id = @return_id ORDER BY assessment_at DESC, created_at DESC LIMIT 1) AS decision
        """
        rows = self._rows(sql, return_id)
        return rows[0] if rows else {}

    def get_return(self, return_id: str) -> dict[str, Any] | None:
        dataset = self.config.returnguard_dataset
        sql = f"SELECT return_id, order_id, order_item_id, user_id, product_id, status, reason, requested_at, assessment_at, source_type, anchor_sale_price FROM `{dataset}.return_requests` WHERE return_id = @return_id LIMIT 1"
        rows = self._rows(sql, return_id)
        return rows[0] if rows else None

    def assessment_history(self, return_id: str) -> list[dict[str, Any]]:
        dataset = self.config.returnguard_dataset
        sql = f"""
        SELECT assessment_id, assessment_at, score, band
        FROM `{dataset}.risk_events`
        WHERE return_id = @return_id
        ORDER BY assessment_at, created_at
        """
        return self._rows(sql, return_id)

    def source_customers(self, search: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if not search or len(search.strip()) < 2:
            return []
        # The source snapshot contains no names/emails; expose only safe IDs and
        # source attributes needed to select a valid customer.
        search_clause = "AND CAST(id AS STRING) LIKE @search" if search else ""
        sql = f"""
        SELECT id AS user_id, created_at, age, gender, city, state, country,
               postal_code, traffic_source
        FROM `{self.config.source_users}`
        WHERE TRUE {search_clause}
        ORDER BY id
        LIMIT @limit
        """
        params = [bigquery.ScalarQueryParameter("limit", "INT64", min(max(limit, 1), 100))]
        if search:
            params.append(bigquery.ScalarQueryParameter("search", "STRING", f"%{search}%"))
        rows = self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        return [dict(row.items()) for row in rows]

    def create_return(self, *, return_id: str, order_item_id: int, customer_id: int | None,
                      order_id: int | None, reason: str, comment: str | None) -> dict[str, Any]:
        """Validate a frozen source relationship, then append one runtime request."""
        lookup = f"""
        SELECT oi.id AS order_item_id, oi.order_id, oi.user_id, oi.product_id,
               oi.sale_price, p.cost AS product_cost
        FROM `{self.config.source_order_items}` oi
        LEFT JOIN `{self.config.source_products}` p ON p.id = oi.product_id
        WHERE oi.id = @order_item_id
        LIMIT 1
        """
        rows = self.client.query(lookup, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("order_item_id", "INT64", order_item_id)
        ])).result()
        row = next(iter(rows), None)
        if row is None:
            raise ValueError("Selected order item is not available in the source snapshot")
        values = dict(row.items())
        if customer_id is not None and int(values["user_id"]) != customer_id:
            raise ValueError("Selected order item does not belong to the selected customer")
        if order_id is not None and int(values["order_id"]) != order_id:
            raise ValueError("Selected order item does not belong to the selected order")
        target = f"{self.config.returnguard_dataset}.return_requests"
        insert = f"""
        INSERT INTO `{target}` (
          return_id, scenario_id, generator_version, user_id, order_item_id,
          order_id, product_id, assessment_at, requested_at, reason,
          customer_comment, responsibility, status, source_type, anchor_sale_price,
          anchor_product_cost, customer_historical_return_count
        ) VALUES (
          @return_id, NULL, NULL, @user_id, @order_item_id, @order_id,
          @product_id, NULL, CURRENT_TIMESTAMP(), @reason, @customer_comment, NULL, 'REQUESTED',
          'source_backed', @sale_price, @product_cost, NULL
        )
        """
        params = [
            bigquery.ScalarQueryParameter("return_id", "STRING", return_id),
            bigquery.ScalarQueryParameter("user_id", "INT64", int(values["user_id"])),
            bigquery.ScalarQueryParameter("order_item_id", "INT64", order_item_id),
            bigquery.ScalarQueryParameter("order_id", "INT64", int(values["order_id"])),
            bigquery.ScalarQueryParameter("product_id", "INT64", int(values["product_id"])),
            bigquery.ScalarQueryParameter("reason", "STRING", reason),
            bigquery.ScalarQueryParameter("customer_comment", "STRING", comment),
            bigquery.ScalarQueryParameter("sale_price", "FLOAT64", values.get("sale_price")),
            bigquery.ScalarQueryParameter("product_cost", "FLOAT64", values.get("product_cost")),
        ]
        self.client.query(insert, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        return {"return_id": return_id, "status": "REQUESTED", "source_type": "source_backed",
                "requested_at": datetime.now(timezone.utc), "assessment_at": None,
                "user_id": int(values["user_id"]), "order_id": int(values["order_id"]),
                "order_item_id": order_item_id, "product_id": int(values["product_id"])}

    def source_orders(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        sql = f"""
        SELECT order_id, user_id, status, created_at, shipped_at, delivered_at,
               returned_at, num_of_item
        FROM `{self.config.source_orders}`
        WHERE user_id = @user_id
        ORDER BY created_at DESC, order_id DESC
        LIMIT @limit
        """
        rows = self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "INT64", user_id),
            bigquery.ScalarQueryParameter("limit", "INT64", min(max(limit, 1), 200)),
        ])).result()
        return [dict(row.items()) for row in rows]

    def source_order_items(self, order_id: int) -> list[dict[str, Any]]:
        sql = f"""
        SELECT oi.id AS order_item_id, oi.order_id, oi.product_id,
               oi.status AS item_status, oi.sale_price,
               p.name AS product_name, p.category, p.sku
        FROM `{self.config.source_order_items}` oi
        LEFT JOIN `{self.config.source_products}` p ON p.id = oi.product_id
        WHERE oi.order_id = @order_id
        ORDER BY oi.id
        """
        rows = self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("order_id", "INT64", order_id)
        ])).result()
        return [dict(row.items()) for row in rows]
