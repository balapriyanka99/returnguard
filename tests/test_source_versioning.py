from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


DATA_GENERATION_DIR = Path(__file__).resolve().parents[1] / "scripts" / "data_generation"
sys.path.insert(0, str(DATA_GENERATION_DIR))

import create_source_snapshot as snapshot  # noqa: E402
import generate_returnguard_synthetic_enrichment as generator  # noqa: E402
import verify_fresh_source_semantics as verifier  # noqa: E402
from thelook_source import SOURCE_DATASET, parse_source_as_of, source_table  # noqa: E402


SOURCE_AS_OF_TEXT = "2026-09-06T12:00:00+00:00"
SOURCE_AS_OF = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
ASSESSMENT_AT = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)


class _FakeQueryResult:
    def result(self):
        return self

    def to_dataframe(self, **_kwargs):
        return pd.DataFrame()

    def __iter__(self):
        return iter(())


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return _FakeQueryResult()


def _source_as_of_value(job_config):
    matches = [
        parameter.value
        for parameter in job_config.query_parameters
        if parameter.name == "source_as_of"
    ]
    return matches


class SourceVersioningTests(unittest.TestCase):
    def test_source_as_of_cli_parsing_is_consistent(self):
        generated = generator.parse_args([
            "--project-id", "test-project", "--source-as-of", SOURCE_AS_OF_TEXT
        ])
        snapshotted = snapshot.parse_args([
            "--output-dir", "/tmp/returnguard-snapshot-test",
            "--source-as-of", SOURCE_AS_OF_TEXT,
        ])
        verified = verifier.parse_args(["--source-as-of", SOURCE_AS_OF_TEXT])

        self.assertEqual(generated.source_as_of, SOURCE_AS_OF)
        self.assertEqual(snapshotted.source_as_of, SOURCE_AS_OF)
        self.assertEqual(verified.source_as_of, SOURCE_AS_OF)

    def test_omitted_source_as_of_preserves_current_table_reference(self):
        self.assertEqual(
            source_table("order_items"),
            f"`{SOURCE_DATASET}.order_items`",
        )
        self.assertNotIn("FOR SYSTEM_TIME", source_table("products"))
        self.assertIsNone(
            generator.parse_args(["--project-id", "test-project"]).source_as_of
        )

    def test_generator_query_uses_parameterized_fixed_version_for_both_tables(self):
        client = _RecordingClient()
        generator.query_anchors(client, 10, ASSESSMENT_AT, SOURCE_AS_OF)

        sql, config = client.calls[0]
        self.assertIn(
            f"`{SOURCE_DATASET}.order_items` FOR SYSTEM_TIME AS OF @source_as_of",
            sql,
        )
        self.assertIn(
            f"`{SOURCE_DATASET}.products` FOR SYSTEM_TIME AS OF @source_as_of",
            sql,
        )
        self.assertEqual(_source_as_of_value(config), [SOURCE_AS_OF])

    def test_snapshot_propagates_one_version_to_every_source_table(self):
        client = _RecordingClient()
        case = verifier.Case(
            return_id="RTN-TEST-001",
            scenario_id="S01",
            order_item_id=1,
            order_id=2,
            user_id=3,
            product_id=4,
            assessment_at=ASSESSMENT_AT,
            anchor_sale_price=25.0,
            controlled_customer_return_count=None,
        )

        snapshot.query_order_items(client, [case], SOURCE_AS_OF)
        snapshot.query_products(client, [4], SOURCE_AS_OF)
        snapshot.query_users(client, [3], SOURCE_AS_OF)
        snapshot.query_orders(client, [2], SOURCE_AS_OF)
        snapshot.query_events(client, [3], ASSESSMENT_AT, SOURCE_AS_OF)

        combined_sql = "\n".join(sql for sql, _config in client.calls)
        for table in ("order_items", "products", "users", "orders", "events"):
            self.assertIn(
                f"`{SOURCE_DATASET}.{table}` FOR SYSTEM_TIME AS OF @source_as_of",
                combined_sql,
            )
        self.assertEqual(len(client.calls), 5)
        for _sql, config in client.calls:
            self.assertEqual(_source_as_of_value(config), [SOURCE_AS_OF])

    def test_verifier_query_uses_same_parameterized_source_version(self):
        client = _RecordingClient()
        case = verifier.Case(
            return_id="RTN-TEST-001",
            scenario_id="S01",
            order_item_id=1,
            order_id=2,
            user_id=3,
            product_id=4,
            assessment_at=ASSESSMENT_AT,
            anchor_sale_price=25.0,
            controlled_customer_return_count=None,
        )

        verifier.query_relevant_order_items(client, [case], SOURCE_AS_OF)

        sql, config = client.calls[0]
        self.assertIn("FOR SYSTEM_TIME AS OF @source_as_of", sql)
        self.assertEqual(_source_as_of_value(config), [SOURCE_AS_OF])

    def test_fixed_source_rebuild_cannot_target_current_active_directory(self):
        current = Path(generator.__file__).resolve().parents[2] / "data" / "generated_returnguard"
        with self.assertRaisesRegex(ValueError, "separate output directory"):
            generator.validate_fixed_source_output_target(current, SOURCE_AS_OF)

        generator.validate_fixed_source_output_target(
            current.parent / "generated_returnguard_rebuilt", SOURCE_AS_OF
        )
        generator.validate_fixed_source_output_target(current, None)

    def test_retired_s12_rows_are_excluded_from_active_tables(self):
        requests = pd.DataFrame([
            {"return_id": "RTN-S12-001", "scenario_id": "S12"},
            {"return_id": "RTN-S12-002", "scenario_id": "S12"},
            {"return_id": "RTN-S12-005", "scenario_id": "S12"},
            {"return_id": "RTN-S12V2-001", "scenario_id": "S12V2"},
        ])
        evidence = pd.DataFrame([
            {"evidence_id": "E1", "return_id": "RTN-S12-001"},
            {"evidence_id": "E2", "return_id": "RTN-S12-002"},
        ])

        active = generator.exclude_legacy_ineligible_rows({
            "return_requests": requests,
            "return_evidence": evidence,
        })

        self.assertEqual(
            active["return_requests"]["return_id"].tolist(),
            ["RTN-S12-002", "RTN-S12V2-001"],
        )
        self.assertEqual(
            active["return_evidence"]["return_id"].tolist(), ["RTN-S12-002"]
        )

    def test_snapshot_anchor_integrity_compares_full_tuple_and_price(self):
        case = verifier.Case(
            return_id="RTN-TEST-001",
            scenario_id="S01",
            order_item_id=1,
            order_id=2,
            user_id=3,
            product_id=4,
            assessment_at=ASSESSMENT_AT,
            anchor_sale_price=25.0,
            controlled_customer_return_count=None,
        )
        order_item = {
            "id": 1,
            "order_id": 2,
            "user_id": 3,
            "product_id": 4,
            "status": "Complete",
            "sale_price": 25.0,
            "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "returned_at": None,
        }

        result = snapshot.verify_integrity(
            [case], [order_item], [{"id": 4}], [{"id": 3}],
            [{"order_id": 2}], set(),
        )

        self.assertEqual(result["anchors_present"], 1)
        self.assertEqual(result["anchor_tuple_and_price_matches"], 1)
        self.assertEqual(result["duplicate_order_item_ids"], 0)
        self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
