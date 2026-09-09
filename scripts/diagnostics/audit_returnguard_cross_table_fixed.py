#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery

EXPECTED_TABLES = [
    "return_requests",
    "fraud_labels",
    "return_logistics",
    "return_operational_costs",
    "synthetic_scenarios",
    "product_attributes",
    "return_evidence",
    "return_inspections",
    "synthetic_network_links",
    "source_order_items_snapshot",
    "source_products_snapshot",
    "source_users_snapshot",
    "source_orders_snapshot",
    "source_events_snapshot",
]

PRIMARY_KEYS = {
    "return_requests": ("return_id",),
    "fraud_labels": ("return_id",),
    "return_logistics": ("return_id",),
    "return_operational_costs": ("return_id",),
    "synthetic_scenarios": ("return_id",),
    "product_attributes": ("product_id",),
    "return_evidence": ("evidence_id",),
    "return_inspections": ("return_id",),
    "synthetic_network_links": ("network_link_id",),
    "source_order_items_snapshot": ("id",),
    "source_products_snapshot": ("id",),
    "source_users_snapshot": ("id",),
    "source_orders_snapshot": ("order_id",),
    "source_events_snapshot": ("id",),
}

RETURN_CHILDREN = [
    "fraud_labels",
    "return_logistics",
    "return_operational_costs",
    "synthetic_scenarios",
    "return_evidence",
    "return_inspections",
    "synthetic_network_links",
]


@dataclass
class Finding:
    check_id: str
    severity: str
    title: str
    count: int
    details: list[dict[str, Any]]
    note: str | None = None


class Auditor:
    def __init__(self, client, project, dataset, repo_root: Path):
        self.client = client
        self.project = project
        self.dataset = dataset
        self.repo_root = repo_root
        self.findings: list[Finding] = []
        self.table_counts: dict[str, int] = {}

    def fq(self, table):
        return f"`{self.project}.{self.dataset}.{table}`"

    def query(self, sql):
        return [dict(r.items()) for r in self.client.query(sql).result()]

    def add(self, cid, sev, title, rows, note=None, max_details=25):
        if isinstance(rows, int):
            count, details = rows, []
        else:
            rows = list(rows)
            count, details = len(rows), rows[:max_details]
        self.findings.append(Finding(cid, sev, title, count, details, note))

    def table_inventory(self):
        missing = []
        for t in EXPECTED_TABLES:
            try:
                meta = self.client.get_table(f"{self.project}.{self.dataset}.{t}")
                self.table_counts[t] = int(meta.num_rows)
            except Exception as e:
                missing.append({"table": t, "error": type(e).__name__})
        self.add("01A", "ERROR", "Missing expected tables", missing)
        self.add(
            "01B",
            "WARNING",
            "Expected tables that are empty",
            [{"table": t, "row_count": c} for t, c in self.table_counts.items() if c == 0],
        )

    def primary_key_duplicates(self):
        for t, cols in PRIMARY_KEYS.items():
            if t not in self.table_counts:
                continue
            c = ", ".join(cols)
            rows = self.query(f"""
            SELECT {c}, COUNT(*) AS duplicate_count
            FROM {self.fq(t)}
            GROUP BY {c}
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC
            """)
            self.add(f"02-{t}", "ERROR", f"Duplicate primary keys in {t}", rows)

    def orphans(self):
        for t in RETURN_CHILDREN:
            rows = self.query(f"""
            SELECT c.return_id
            FROM {self.fq(t)} c
            LEFT JOIN {self.fq("return_requests")} r USING(return_id)
            WHERE r.return_id IS NULL
            """)
            self.add(f"03-{t}", "ERROR", f"Orphan return_id values in {t}", rows)

    def anchors(self):
        rows = self.query(f"""
        SELECT
          r.return_id,
          r.order_item_id,
          r.order_id AS controlled_order_id,
          s.order_id AS source_order_id,
          r.user_id AS controlled_user_id,
          s.user_id AS source_user_id,
          r.product_id AS controlled_product_id,
          s.product_id AS source_product_id,
          r.anchor_sale_price,
          s.sale_price
        FROM {self.fq("return_requests")} r
        LEFT JOIN {self.fq("source_order_items_snapshot")} s
          ON s.id = r.order_item_id
        WHERE s.id IS NULL
           OR r.order_id != s.order_id
           OR r.user_id != s.user_id
           OR r.product_id != s.product_id
           OR ABS(CAST(r.anchor_sale_price AS FLOAT64)-CAST(s.sale_price AS FLOAT64)) > 0.000001
        ORDER BY r.return_id
        """)
        self.add("04A", "ERROR", "Controlled anchor does not match frozen source order item", rows)

        summary = self.query(f"""
        SELECT
          COUNT(*) AS total_cases,
          COUNTIF(s.id IS NOT NULL) AS anchors_found,
          COUNTIF(
            s.id IS NOT NULL
            AND r.order_id = s.order_id
            AND r.user_id = s.user_id
            AND r.product_id = s.product_id
            AND ABS(CAST(r.anchor_sale_price AS FLOAT64)-CAST(s.sale_price AS FLOAT64)) <= 0.000001
          ) AS exact_matches
        FROM {self.fq("return_requests")} r
        LEFT JOIN {self.fq("source_order_items_snapshot")} s
          ON s.id = r.order_item_id
        """)
        self.add("04B", "INFO", "Anchor match summary", summary)

    def source_entities(self):
        self.add("05A", "ERROR", "Case user missing from source_users_snapshot", self.query(f"""
        SELECT r.return_id, r.user_id
        FROM {self.fq("return_requests")} r
        LEFT JOIN {self.fq("source_users_snapshot")} u ON u.id = r.user_id
        WHERE u.id IS NULL
        """))

        self.add("05B", "ERROR", "Case product missing from source_products_snapshot", self.query(f"""
        SELECT r.return_id, r.product_id
        FROM {self.fq("return_requests")} r
        LEFT JOIN {self.fq("source_products_snapshot")} p ON p.id = r.product_id
        WHERE p.id IS NULL
        """))

        self.add("05C", "ERROR", "Case order missing or owned by wrong user", self.query(f"""
        SELECT r.return_id, r.order_id, r.user_id AS case_user_id, o.user_id AS order_user_id
        FROM {self.fq("return_requests")} r
        LEFT JOIN {self.fq("source_orders_snapshot")} o ON o.order_id = r.order_id
        WHERE o.order_id IS NULL OR o.user_id != r.user_id
        """))

    def scenario_consistency(self):
        self.add("06A", "ERROR", "return_requests vs synthetic_scenarios mismatch", self.query(f"""
        SELECT r.return_id, r.scenario_id AS request_scenario_id, s.scenario_id AS scenario_table_scenario_id
        FROM {self.fq("return_requests")} r
        LEFT JOIN {self.fq("synthetic_scenarios")} s USING(return_id)
        WHERE s.return_id IS NULL OR r.scenario_id != s.scenario_id
        """))

        for t in ["fraud_labels","return_logistics","return_operational_costs","return_evidence","return_inspections","synthetic_network_links"]:
            schema = {f.name for f in self.client.get_table(f"{self.project}.{self.dataset}.{t}").schema}
            if "scenario_id" not in schema:
                continue
            self.add(f"06-{t}", "ERROR", f"scenario_id mismatch in {t}", self.query(f"""
            SELECT c.return_id, r.scenario_id AS request_scenario_id, c.scenario_id AS child_scenario_id
            FROM {self.fq(t)} c
            JOIN {self.fq("return_requests")} r USING(return_id)
            WHERE c.scenario_id IS NOT NULL AND c.scenario_id != r.scenario_id
            """))

    def inspection_product(self):
        self.add("07A", "ERROR", "Inspected return missing product_attributes", self.query(f"""
        SELECT r.return_id, r.product_id
        FROM {self.fq("return_requests")} r
        JOIN {self.fq("return_inspections")} i USING(return_id)
        LEFT JOIN {self.fq("product_attributes")} p USING(product_id)
        WHERE p.product_id IS NULL
        """))

        self.add("07B", "ERROR", "Inspection expected weight disagrees with product baseline", self.query(f"""
        SELECT
          r.return_id,
          r.product_id,
          i.expected_weight_kg AS inspection_expected_weight_kg,
          p.expected_weight_kg AS product_expected_weight_kg,
          ABS(i.expected_weight_kg-p.expected_weight_kg) AS absolute_difference_kg
        FROM {self.fq("return_requests")} r
        JOIN {self.fq("return_inspections")} i USING(return_id)
        JOIN {self.fq("product_attributes")} p USING(product_id)
        WHERE i.expected_weight_kg IS NOT NULL
          AND p.expected_weight_kg IS NOT NULL
          AND ABS(i.expected_weight_kg-p.expected_weight_kg) > 0.000001
        ORDER BY absolute_difference_kg DESC
        """), note="Generator currently samples expected_weight_kg independently in product_attributes and make_case.")

        self.add("07C", "ERROR", "Non-positive expected/actual inspection weights", self.query(f"""
        SELECT r.return_id, r.product_id, i.expected_weight_kg, i.actual_weight_kg
        FROM {self.fq("return_requests")} r
        JOIN {self.fq("return_inspections")} i USING(return_id)
        WHERE (i.expected_weight_kg IS NOT NULL AND i.expected_weight_kg <= 0)
           OR (i.actual_weight_kg IS NOT NULL AND i.actual_weight_kg <= 0)
        """))

        self.add("07D", "ERROR", "Serial-verification scenario lacks expected serial", self.query(f"""
        SELECT
          r.return_id,
          r.scenario_id,
          r.product_id,
          i.expected_serial,
          i.returned_serial
        FROM {self.fq("return_requests")} r
        JOIN {self.fq("return_inspections")} i USING(return_id)
        WHERE r.scenario_id IN ("S07", "M08")
          AND (
            i.expected_serial IS NULL
            OR i.returned_serial IS NULL
          )
        """), note="Only explicit serial-verification scenarios are required to carry expected/returned serials.")

        rows = self.query(f"""
        SELECT r.return_id, r.product_id, p.expected_accessories, i.accessories_present
        FROM {self.fq("return_requests")} r
        JOIN {self.fq("return_inspections")} i USING(return_id)
        JOIN {self.fq("product_attributes")} p USING(product_id)
        WHERE p.expected_accessories IS NOT NULL AND i.accessories_present IS NOT NULL
        """)
        malformed, extras = [], []
        for row in rows:
            try:
                expected = self._arr(row["expected_accessories"])
                present = self._arr(row["accessories_present"])
                unexpected = sorted(set(present) - set(expected))
                if unexpected:
                    extras.append({
                        "return_id": row["return_id"],
                        "product_id": row["product_id"],
                        "unexpected": unexpected,
                        "expected": expected,
                        "present": present,
                    })
            except Exception as e:
                malformed.append({"return_id": row["return_id"], "error": str(e)})
        self.add("07E", "ERROR", "Malformed accessory arrays", malformed)
        self.add("07F", "WARNING", "Inspection contains accessories outside expected set", extras,
                 note="Missing accessories are allowed; only unexpected extra values are flagged.")

    @staticmethod
    def _arr(v):
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        x = json.loads(str(v))
        if not isinstance(x, list):
            raise ValueError("not a JSON array")
        return [str(i) for i in x]

    def economics(self):
        self.add("08A", "INFO", "Case economics differ from product baseline", self.query(f"""
        SELECT
          r.return_id, r.product_id,
          c.inspection_cost AS case_inspection_cost,
          p.inspection_cost AS product_inspection_cost,
          c.recovery_value AS case_recovery_value,
          p.recovery_value AS product_recovery_value
        FROM {self.fq("return_requests")} r
        JOIN {self.fq("return_operational_costs")} c USING(return_id)
        LEFT JOIN {self.fq("product_attributes")} p USING(product_id)
        WHERE p.product_id IS NOT NULL
          AND (
            (c.inspection_cost IS NOT NULL AND p.inspection_cost IS NOT NULL
             AND ABS(c.inspection_cost-p.inspection_cost) > 0.000001)
            OR
            (c.recovery_value IS NOT NULL AND p.recovery_value IS NOT NULL
             AND ABS(c.recovery_value-p.recovery_value) > 0.01)
          )
        """), note=(
            "Informational only: product_attributes stores a product-level baseline/default, "
            "while return_operational_costs stores actual case-incurred values."
        ))

        self.add("08B", "ERROR", "Negative operational values", self.query(f"""
        SELECT return_id, reverse_logistics_cost, inspection_cost, recovery_value
        FROM {self.fq("return_operational_costs")}
        WHERE (reverse_logistics_cost IS NOT NULL AND reverse_logistics_cost < 0)
           OR (inspection_cost IS NOT NULL AND inspection_cost < 0)
           OR (recovery_value IS NOT NULL AND recovery_value < 0)
        """))

    def evidence(self):
        self.add("09A", "ERROR", "Evidence row missing image_uri", self.query(f"""
        SELECT evidence_id, return_id, type, stage, image_uri
        FROM {self.fq("return_evidence")}
        WHERE image_uri IS NULL OR TRIM(image_uri) = ''
        """))

        self.add("09B", "ERROR", "Evidence reference image disagrees with product baseline", self.query(f"""
        SELECT e.evidence_id, e.return_id, r.product_id,
               e.reference_image_uri, p.reference_image_uri AS canonical_reference_image_uri
        FROM {self.fq("return_evidence")} e
        JOIN {self.fq("return_requests")} r USING(return_id)
        LEFT JOIN {self.fq("product_attributes")} p USING(product_id)
        WHERE e.reference_image_uri IS NOT NULL
          AND p.reference_image_uri IS NOT NULL
          AND e.reference_image_uri != p.reference_image_uri
        """))

    def network(self):
        self.add("10A", "ERROR", "Network anchor user does not match case user", self.query(f"""
        SELECT n.network_link_id, n.return_id, n.user_id AS link_user_id, r.user_id AS case_user_id
        FROM {self.fq("synthetic_network_links")} n
        JOIN {self.fq("return_requests")} r USING(return_id)
        WHERE n.user_id != r.user_id
        """))

        self.add("10B", "ERROR", "Network timestamps violate point-in-time semantics", self.query(f"""
        SELECT n.network_link_id, n.return_id, n.first_observed_at, n.last_observed_at, r.assessment_at
        FROM {self.fq("synthetic_network_links")} n
        JOIN {self.fq("return_requests")} r USING(return_id)
        WHERE n.first_observed_at > r.assessment_at
           OR n.last_observed_at > r.assessment_at
           OR n.last_observed_at < n.first_observed_at
        """))

        self.add("10C", "ERROR", "Network self-link detected", self.query(f"""
        SELECT network_link_id, return_id, user_id, linked_user_id
        FROM {self.fq("synthetic_network_links")}
        WHERE user_id = linked_user_id
        """))

    def temporal(self):
        self.add("11A", "ERROR", "requested_at after assessment_at", self.query(f"""
        SELECT return_id, requested_at, assessment_at
        FROM {self.fq("return_requests")}
        WHERE requested_at > assessment_at
        """))

        self.add("11B", "ERROR", "Evidence observed after assessment_at", self.query(f"""
        SELECT e.evidence_id, e.return_id, e.observed_at, r.assessment_at
        FROM {self.fq("return_evidence")} e
        JOIN {self.fq("return_requests")} r USING(return_id)
        WHERE e.observed_at > r.assessment_at
        """))

        self.add("11C", "ERROR", "Inspection after assessment_at", self.query(f"""
        SELECT i.return_id, i.inspected_at, r.assessment_at
        FROM {self.fq("return_inspections")} i
        JOIN {self.fq("return_requests")} r USING(return_id)
        WHERE i.inspected_at > r.assessment_at
        """))

        self.add("11D", "ERROR", "Logistics lifecycle timestamps inconsistent", self.query(f"""
        SELECT l.return_id, l.pickup_at, l.received_at, r.requested_at, r.assessment_at
        FROM {self.fq("return_logistics")} l
        JOIN {self.fq("return_requests")} r USING(return_id)
        WHERE (l.pickup_at IS NOT NULL AND l.pickup_at < r.requested_at)
           OR (l.received_at IS NOT NULL AND l.pickup_at IS NOT NULL AND l.received_at < l.pickup_at)
           OR (l.pickup_at IS NOT NULL AND l.pickup_at > r.assessment_at)
           OR (l.received_at IS NOT NULL AND l.received_at > r.assessment_at)
        """))

    def history(self):
        self.add("12A", "ERROR", "Same source order item reused by multiple controlled returns", self.query(f"""
        SELECT order_item_id, COUNT(*) AS case_count, ARRAY_AGG(return_id ORDER BY return_id) AS return_ids
        FROM {self.fq("return_requests")}
        GROUP BY order_item_id
        HAVING COUNT(*) > 1
        """))

    def fraud_label_safety(self):
        self.add("13A", "ERROR", "fraud_labels contains unknown return_id", self.query(f"""
        SELECT f.return_id
        FROM {self.fq("fraud_labels")} f
        LEFT JOIN {self.fq("return_requests")} r USING(return_id)
        WHERE r.return_id IS NULL
        """))

        suspicious = []
        root = self.repo_root / "returnguard"
        if root.exists():
            for p in root.rglob("*.py"):
                txt = p.read_text(encoding="utf-8")
                for n, line in enumerate(txt.splitlines(), 1):
                    stripped = line.strip()
                    if "fraud_labels" not in stripped:
                        continue

                    allowed = (
                        "never use fraud_labels" in stripped.lower()
                        or stripped in {'"fraud_labels",', "'fraud_labels',"}
                    )
                    if allowed:
                        continue

                    suspicious.append({
                        "file": str(p.relative_to(self.repo_root)),
                        "line": n,
                        "text": stripped[:300],
                    })

        self.add(
            "13B",
            "ERROR",
            "Runtime Python code appears to access evaluation-only fraud_labels",
            suspicious,
            note=(
                "Safety mentions and blocked-field declarations are allowed. "
                "Any other runtime reference is flagged."
            ),
        )

    def uri_sanity(self):
        self.add("14A", "ERROR", "Evidence URI is not gs://", self.query(f"""
        SELECT evidence_id, return_id, image_uri, reference_image_uri
        FROM {self.fq("return_evidence")}
        WHERE (image_uri IS NOT NULL AND NOT STARTS_WITH(image_uri,'gs://'))
           OR (reference_image_uri IS NOT NULL AND NOT STARTS_WITH(reference_image_uri,'gs://'))
        """))

        self.add("14B", "ERROR", "Product reference image URI is not gs://", self.query(f"""
        SELECT product_id, reference_image_uri
        FROM {self.fq("product_attributes")}
        WHERE reference_image_uri IS NOT NULL
          AND NOT STARTS_WITH(reference_image_uri,'gs://')
        """))

    def run(self):
        self.table_inventory()
        if any(t not in self.table_counts for t in EXPECTED_TABLES):
            return
        self.primary_key_duplicates()
        self.orphans()
        self.anchors()
        self.source_entities()
        self.scenario_consistency()
        self.inspection_product()
        self.economics()
        self.evidence()
        self.network()
        self.temporal()
        self.history()
        self.fraud_label_safety()
        self.uri_sanity()

    def summary(self):
        failed = [f.check_id for f in self.findings if f.severity == "ERROR" and f.count > 0]
        warnings = [f.check_id for f in self.findings if f.severity == "WARNING" and f.count > 0]
        return {
            "project": self.project,
            "dataset": self.dataset,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "table_counts": self.table_counts,
            "failed_error_checks": failed,
            "warning_checks": warnings,
            "error_finding_count": sum(f.count for f in self.findings if f.severity == "ERROR"),
            "warning_finding_count": sum(f.count for f in self.findings if f.severity == "WARNING"),
            "status": "PASS" if not failed else "FAIL",
        }


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--project-id", default="return-guard-506407")
    p.add_argument("--dataset", default="returnguard")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--report-path", default="data/audits/cross_table_audit_returnguard.json")
    p.add_argument("--strict-warnings", action="store_true")
    return p.parse_args()


def main():
    a = parse_args()
    audit = Auditor(
        bigquery.Client(project=a.project_id),
        a.project_id,
        a.dataset,
        Path(a.repo_root).resolve(),
    )
    audit.run()

    print("\n" + "=" * 96)
    print("RETURNGUARD CROSS-TABLE AUDIT — READ ONLY")
    print("=" * 96)

    for f in audit.findings:
        marker = "PASS" if f.count == 0 and f.severity != "INFO" else f.severity
        print(f"[{marker:7}] {f.check_id:18} {f.title} | findings={f.count}")
        if f.note:
            print(f"          note: {f.note}")
        for d in f.details[:5]:
            print("          " + json.dumps(d, default=str, ensure_ascii=False))

    summary = audit.summary()
    print("\nSUMMARY")
    print(json.dumps(summary, indent=2, default=str))

    path = Path(a.report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "summary": summary,
        "findings": [asdict(f) for f in audit.findings],
    }, indent=2, default=str, ensure_ascii=False), encoding="utf-8")

    print(f"\nJSON report: {path}")

    if summary["failed_error_checks"]:
        raise SystemExit(1)
    if a.strict_warnings and summary["warning_checks"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
