from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone

from returnguard.intelligence.config import IntelligenceConfig
from returnguard.intelligence.repository import BigQueryIntelligenceRepository
from returnguard.observability import ExecutionContext, bind_execution_context


class EmptyJob:
    def result(self):
        return []


class RecordingClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, job_config):
        self.calls.append((sql, job_config.query_parameters))
        return EmptyJob()


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class RepositorySqlContractTests(unittest.TestCase):
    def setUp(self):
        self.client = RecordingClient()
        config = IntelligenceConfig(project_id="p", dataset_id="d", source_project_id="s", source_dataset_id="source")
        self.repo = BigQueryIntelligenceRepository(config=config, client=self.client)
        self.at = datetime(2026, 9, 2, tzinfo=timezone.utc)

    def test_product_query_is_parameterized_point_in_time_leave_one_out(self):
        self.repo.get_product_history(123, self.at, 456)
        sql, parameters = self.client.calls[-1]
        self.assertIn("oi.created_at <= @assessment_at", sql)
        self.assertIn("oi.id != @current_order_item_id", sql)
        self.assertIn("!= 'cancelled'", sql)
        self.assertIn("returned_at <= @assessment_at", sql)
        self.assertNotIn("123", sql)
        self.assertEqual({p.name for p in parameters}, {"product_id", "assessment_at", "current_order_item_id"})

    def test_customer_query_excludes_current_anchor_and_future_returns(self):
        self.repo.get_customer_history(1, 2, "Jeans", self.at, 3)
        sql, parameters = self.client.calls[-1]
        self.assertIn("oi.created_at <= @assessment_at", sql)
        self.assertIn("oi.id != @current_order_item_id", sql)
        self.assertIn("returned_at <= @assessment_at", sql)
        self.assertIn("!= 'cancelled'", sql)
        self.assertEqual(next(p.value for p in parameters if p.name == "current_order_item_id"), 3)

    def test_default_customer_source_routes_to_frozen_tables(self):
        client = RecordingClient()
        repo = BigQueryIntelligenceRepository(config=IntelligenceConfig(), client=client)
        repo.get_customer_history(1, 2, "Jeans", self.at, 3)
        sql, _ = client.calls[-1]
        self.assertIn("`return-guard-506407.returnguard.source_order_items_snapshot`", sql)
        self.assertIn("`return-guard-506407.returnguard.source_products_snapshot`", sql)
        self.assertNotIn("bigquery-public-data", sql)

    def test_default_product_source_routes_to_frozen_tables(self):
        client = RecordingClient()
        repo = BigQueryIntelligenceRepository(config=IntelligenceConfig(), client=client)
        repo.get_product_history(2, self.at, 3)
        sql, _ = client.calls[-1]
        self.assertIn("`return-guard-506407.returnguard.source_order_items_snapshot`", sql)
        self.assertIn("`return-guard-506407.returnguard.source_products_snapshot`", sql)
        self.assertNotIn("bigquery-public-data", sql)

    def test_return_id_queries_use_query_parameters(self):
        malicious = "x' OR TRUE --"
        self.repo.get_return(malicious)
        sql, parameters = self.client.calls[-1]
        self.assertNotIn(malicious, sql)
        self.assertEqual(parameters[0].value, malicious)

    def test_network_context_hides_future_observations(self):
        self.repo.get_network_links("RTN-M01-001", self.at)
        sql, _ = self.client.calls[-1]
        self.assertIn("`p.d.synthetic_network_links`", sql)
        self.assertIn("first_observed_at <= @assessment_at", sql)
        self.assertIn("IF(last_observed_at <= @assessment_at, last_observed_at, NULL)", sql)

    def test_inspection_retrieves_controlled_expected_accessories(self):
        self.repo.get_inspection("RTN-S08-001")
        sql, parameters = self.client.calls[-1]
        self.assertIn("i.expected_serial", sql)
        self.assertIn("p.expected_accessories", sql)
        self.assertIn("product_attributes` p ON p.product_id = r.product_id", sql)
        self.assertEqual(parameters[0].value, "RTN-S08-001")

    def test_repository_logging_excludes_sql_and_parameter_payloads(self):
        handler = RecordingHandler()
        repository_logger = logging.getLogger("returnguard.intelligence.repository")
        repository_logger.addHandler(handler)
        repository_logger.setLevel(logging.INFO)
        private_parameter = "private-return-identifier"
        try:
            with bind_execution_context(ExecutionContext("trace-repo", "assessment-repo")):
                self.repo.get_return(private_parameter)
            completed = next(record for record in handler.records if record.event == "completed")
            self.assertEqual(completed.operation_name, "get_return")
            self.assertEqual(completed.trace_id, "trace-repo")
            self.assertEqual(completed.assessment_id, "assessment-repo")
            self.assertIn("event=returnguard_operation", completed.getMessage())
            self.assertIn("layer=repository", completed.getMessage())
            self.assertIn("operation=get_return", completed.getMessage())
            self.assertIn("status=completed", completed.getMessage())
            logged = " ".join(str(record.__dict__) for record in handler.records)
            self.assertNotIn("SELECT return_id", logged)
            self.assertNotIn(private_parameter, logged)
            self.assertNotIn("query_parameters", logged)
        finally:
            repository_logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
