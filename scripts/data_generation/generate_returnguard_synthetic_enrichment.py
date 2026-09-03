#!/usr/bin/env python3
"""
ReturnGuard deterministic synthetic operational-enrichment generator.

Purpose
-------
Build the ReturnGuard-owned operational layer around REAL The Look anchors.

This script does NOT:
- replace The Look customers/products/orders with fake universes
- mutate the public source tables
- invent historical return reasons
- generate Gemini findings
- use fraud ground truth as a risk feature
- generate risk scores or final decisions

It DOES:
- query real customer/order_item/product/network anchors from BigQuery
- create ~65 controlled return scenarios across S01-S14
- generate only the missing operational/enrichment records
- preserve provenance and reproducibility
- optionally create/load ReturnGuard BigQuery tables

The generated data is a DEMO/CONTROLLED operational layer. It must be kept
separate from source-provided The Look records.

Usage
-----
1) Generate local CSV/JSON files:
    python generate_returnguard_synthetic_enrichment.py \
        --project-id YOUR_GCP_PROJECT \
        --output-dir ./data/generated \
        --assessment-at 2026-09-02T23:59:59

2) Generate and load into BigQuery:
    python generate_returnguard_synthetic_enrichment.py \
        --project-id YOUR_GCP_PROJECT \
        --output-dir ./data/generated \
        --load-bigquery

Dependencies
------------
    pip install google-cloud-bigquery pandas pyarrow
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from google.cloud import bigquery


GENERATOR_VERSION = "rg-synth-v1.0.2"
SOURCE_DATASET = "bigquery-public-data.thelook_ecommerce"

SCENARIO_COUNTS = {
    "S01": 5,  # Honest normal
    "S02": 5,  # Legitimate heavy returner
    "S03": 5,  # Merchant-fault damaged
    "S04": 4,  # Wrong item
    "S05": 4,  # High-value legitimate
    "S06": 5,  # Empty box
    "S07": 5,  # Serial/product swap
    "S08": 4,  # Missing accessories
    "S09": 4,  # False damage claim
    "S10": 5,  # Wardrobing
    "S11": 5,  # Coordinated abuse
    "S12": 5,  # Product quality
    "S13": 4,  # High-cost legitimate
    "S14": 4,  # Mixed/ambiguous
}

SCENARIO_DESCRIPTIONS = {
    "S01": "Honest normal return",
    "S02": "Legitimate heavy returner",
    "S03": "Merchant-fault damaged return",
    "S04": "Wrong item shipped",
    "S05": "High-value legitimate return",
    "S06": "Empty box / missing item",
    "S07": "Serial/product swap",
    "S08": "Missing accessories/components",
    "S09": "False damage claim / conflicting visual evidence",
    "S10": "Wardrobing / repeated short-duration usage pattern",
    "S11": "Coordinated abuse / correlated cases",
    "S12": "Product-quality issue",
    "S13": "High-cost legitimate return",
    "S14": "Mixed / ambiguous evidence",
}

REASONS = {
    "S01": ("changed_mind", "customer_discretionary"),
    "S02": ("wrong_size", "customer_discretionary"),
    "S03": ("damaged_on_arrival", "merchant_fault"),
    "S04": ("wrong_item_shipped", "merchant_fault"),
    "S05": ("changed_mind", "customer_discretionary"),
    "S06": ("missing_item", "unknown"),
    "S07": ("not_as_described", "unknown"),
    "S08": ("missing_accessories", "unknown"),
    "S09": ("damaged", "unknown"),
    "S10": ("changed_mind", "customer_discretionary"),
    "S11": ("changed_mind", "unknown"),
    "S12": ("defective", "merchant_fault"),
    "S13": ("changed_mind", "customer_discretionary"),
    "S14": ("damaged", "unknown"),
}

STAGES = ("CUSTOMER", "PICKUP", "WAREHOUSE")


@dataclass(frozen=True)
class Anchor:
    user_id: int
    order_item_id: int
    order_id: int
    product_id: int
    sale_price: float
    product_name: str
    brand: str
    category: str
    department: str
    product_cost: float
    ip_hash: Optional[str] = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate ReturnGuard controlled synthetic enrichment.")
    p.add_argument("--project-id", required=True, help="Google Cloud project used to query/load ReturnGuard.")
    p.add_argument("--output-dir", default="./data/generated_returnguard",
                   help="Directory for generated CSV/JSON outputs.")
    p.add_argument("--dataset", default="returnguard",
                   help="ReturnGuard BigQuery dataset name.")
    p.add_argument("--seed", type=int, default=20260903,
                   help="Deterministic generation seed.")
    p.add_argument("--assessment-at", default="2026-09-02T23:59:59",
                   help="Reference timestamp for current-state features, ISO format.")
    p.add_argument("--load-bigquery", action="store_true",
                   help="Create/load ReturnGuard BigQuery tables after generation.")
    p.add_argument("--limit-anchors", type=int, default=1200,
                   help="Maximum number of source anchor rows fetched for local selection.")
    return p.parse_args()


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_assessment(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def stable_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def ip_hash(ip: Optional[str]) -> Optional[str]:
    if not ip:
        return None
    return stable_hash(ip)[:32]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any) -> int:
    return int(value)


def serial_for(product_id: int, scenario_id: str, index: int) -> str:
    """
    Deterministic synthetic serial. It is intentionally ReturnGuard-owned.
    """
    digest = stable_hash(f"{GENERATOR_VERSION}|{product_id}|{scenario_id}|{index}")[:10].upper()
    return f"RG-{product_id}-{digest}"


def scenario_return_id(scenario_id: str, index: int) -> str:
    return f"RTN-{scenario_id}-{index:03d}"


def choose(rng: random.Random, values: Sequence[Any]) -> Any:
    return values[rng.randrange(len(values))]


def expected_weight_kg(anchor: Anchor, rng: random.Random) -> float:
    """
    Synthetic operational attribute only. The source does not provide weight.
    Values are generated deterministically and are not claimed to be source facts.
    """
    # Broad ecommerce ranges; product-specific variation keeps repeated products consistent.
    if anchor.sale_price >= 500:
        base = rng.uniform(0.7, 3.0)
    elif anchor.sale_price >= 150:
        base = rng.uniform(0.3, 2.0)
    else:
        base = rng.uniform(0.15, 1.2)
    return round(base, 3)


def accessory_set(anchor: Anchor, rng: random.Random) -> List[str]:
    common = ["product", "packaging"]
    if anchor.sale_price >= 100:
        common += ["manual", "charger_or_cable"]
    if anchor.category and any(x in anchor.category.lower() for x in ("shoes", "clothing", "pants", "tops")):
        common += ["tags"]
    # Stable uniqueness.
    return list(dict.fromkeys(common))


def query_anchors(client: bigquery.Client, limit: int) -> pd.DataFrame:
    """
    Fetch real The Look anchors.

    We deliberately select NON-RETURNED order items so a generated ReturnGuard
    return represents a new operational case rather than fabricating a historical
    source return.
    """
    query = f"""
    SELECT
      oi.id AS order_item_id,
      oi.order_id,
      oi.user_id,
      oi.product_id,
      CAST(oi.sale_price AS FLOAT64) AS sale_price,
      p.name AS product_name,
      p.brand,
      p.category,
      p.department,
      CAST(p.cost AS FLOAT64) AS product_cost
    FROM `{SOURCE_DATASET}.order_items` oi
    JOIN `{SOURCE_DATASET}.products` p
      ON p.id = oi.product_id
    WHERE LOWER(CAST(oi.status AS STRING)) != 'returned'
      AND oi.user_id IS NOT NULL
      AND oi.product_id IS NOT NULL
    ORDER BY oi.id
    LIMIT {int(limit)}
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def query_customer_return_counts(client: bigquery.Client) -> pd.DataFrame:
    query = f"""
    SELECT
      user_id,
      COUNTIF(LOWER(CAST(status AS STRING)) = 'returned') AS returned_count,
      COUNT(*) AS item_count
    FROM `{SOURCE_DATASET}.order_items`
    WHERE user_id IS NOT NULL
    GROUP BY user_id
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def query_high_value_users(client: bigquery.Client) -> pd.DataFrame:
    query = f"""
    SELECT
      oi.user_id,
      SUM(CAST(oi.sale_price AS FLOAT64)) AS observed_spend
    FROM `{SOURCE_DATASET}.order_items` oi
    WHERE oi.user_id IS NOT NULL
    GROUP BY oi.user_id
    ORDER BY observed_spend DESC
    LIMIT 1000
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def query_identifiable_network(client: bigquery.Client) -> pd.DataFrame:
    """
    Only attributable events are used. Anonymous events are intentionally excluded.
    """
    query = f"""
    SELECT
      user_id,
      ip_address
    FROM `{SOURCE_DATASET}.events`
    WHERE user_id IS NOT NULL
      AND ip_address IS NOT NULL
    GROUP BY user_id, ip_address
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def make_anchor_objects(
    anchors_df: pd.DataFrame,
    return_counts_df: pd.DataFrame,
    rng: random.Random,
) -> Tuple[List[Anchor], Dict[int, int], List[Tuple[int, str]]]:
    rc = {
        int(row.user_id): int(row.returned_count)
        for row in return_counts_df.itertuples()
    }

    anchors: List[Anchor] = []
    for row in anchors_df.itertuples():
        anchors.append(
            Anchor(
                user_id=safe_int(row.user_id),
                order_item_id=safe_int(row.order_item_id),
                order_id=safe_int(row.order_id),
                product_id=safe_int(row.product_id),
                sale_price=safe_float(row.sale_price),
                product_name=str(row.product_name or ""),
                brand=str(row.brand or ""),
                category=str(row.category or ""),
                department=str(row.department or ""),
                product_cost=safe_float(row.product_cost),
            )
        )

    heavy = sorted(
        [(u, c) for u, c in rc.items() if c >= 3],
        key=lambda x: (-x[1], x[0]),
    )
    return anchors, rc, heavy


def select_anchor(
    scenario_id: str,
    anchors: List[Anchor],
    return_counts: Dict[int, int],
    rng: random.Random,
    used_order_items: set[int],
) -> Anchor:
    """
    Scenario-aware real anchor selection.
    """
    available = [a for a in anchors if a.order_item_id not in used_order_items]
    if not available:
        raise RuntimeError("Ran out of unique real order_item anchors.")

    if scenario_id == "S02":
        candidates = [a for a in available if return_counts.get(a.user_id, 0) >= 3]
        if candidates:
            return choose(rng, candidates)

    if scenario_id == "S05":
        candidates = [a for a in available if a.sale_price >= 250]
        if candidates:
            return choose(rng, candidates)

    if scenario_id == "S12":
        candidates = [a for a in available if a.product_id in {
            x.product_id for x in available if x.sale_price > 0
        }]
        if candidates:
            return choose(rng, candidates)

    if scenario_id in {"S06", "S07", "S08", "S09"}:
        # Reuse product families for evidence-driven physical cases, but keep order items unique.
        candidates = [a for a in available if a.sale_price >= 50]
        if candidates:
            return choose(rng, candidates)

    return choose(rng, available)


def build_product_attributes(
    anchors_by_product: Dict[int, Anchor],
    scenario_map: Dict[str, List[Dict[str, Any]]],
    rng: random.Random,
) -> List[Dict[str, Any]]:
    rows = []
    physical_products = set()
    for cases in scenario_map.values():
        for case in cases:
            if case["scenario_id"] in {"S06", "S07", "S08", "S09"}:
                physical_products.add(case["product_id"])
            elif case["scenario_id"] in {"S05", "S13"}:
                physical_products.add(case["product_id"])

    for product_id in sorted(physical_products):
        anchor = anchors_by_product[product_id]
        serial_required = anchor.sale_price >= 150 or anchor.category.lower() in {
            "computers", "phones", "electronics"
        }
        attrs = accessory_set(anchor, rng)
        rows.append({
            "product_id": product_id,
            "expected_weight_kg": expected_weight_kg(anchor, rng),
            "reference_image_uri": f"gs://returnguard-evidence/reference/product_{product_id}.jpg",
            "expected_accessories": json.dumps(attrs),
            "serial_required": bool(serial_required),
            "serial_pattern": f"RG-{product_id}-{{TOKEN}}",
            "condition_baseline": "new_or_salable",
            "recovery_value": round(max(anchor.product_cost * 0.65, 0.0), 2),
            "inspection_cost": 12.50 if anchor.sale_price < 150 else 24.00,
            "source_type": "synthetic_demo",
            "source_table": None,
            "source_record_id": str(product_id),
            "generator_version": GENERATOR_VERSION,
        })
    return rows


def make_case(
    scenario_id: str,
    index: int,
    anchor: Anchor,
    assessment_at: datetime,
    rng: random.Random,
    return_counts: Dict[int, int],
    network_pool: List[Tuple[int, str]],
) -> Dict[str, Any]:
    rid = scenario_return_id(scenario_id, index)
    reason, responsibility = REASONS[scenario_id]

    # Operational lifecycle timestamps are deliberately created by ReturnGuard.
    requested = assessment_at - timedelta(days=rng.randint(7, 12), hours=rng.randint(0, 20))
    pickup = requested + timedelta(hours=rng.randint(12, 48))
    received = pickup + timedelta(days=rng.randint(1, 4))

    base = {
        "return_id": rid,
        "scenario_id": scenario_id,
        "scenario_description": SCENARIO_DESCRIPTIONS[scenario_id],
        "order_item_id": anchor.order_item_id,
        "order_id": anchor.order_id,
        "user_id": anchor.user_id,
        "product_id": anchor.product_id,
        "requested_at": iso(requested),
        "reason": reason,
        "customer_comment": "",
        "responsibility": responsibility,
        "status": "REQUESTED",
        "assessment_at": iso(assessment_at),
        "source_type": "synthetic_demo",
        "source_table": f"{SOURCE_DATASET}.order_items",
        "source_record_id": str(anchor.order_item_id),
        "generator_version": GENERATOR_VERSION,
        "seed": None,
        "anchor_sale_price": anchor.sale_price,
        "anchor_product_cost": anchor.product_cost,
        "customer_historical_return_count": return_counts.get(anchor.user_id, 0),
    }

    evidence: List[Dict[str, Any]] = []
    inspection: Optional[Dict[str, Any]] = None
    logistics = {
        "return_id": rid,
        "pickup_at": iso(pickup),
        "received_at": iso(received),
        "reverse_cost": round(rng.uniform(35, 120), 2),
        "source_type": "synthetic_demo",
        "generator_version": GENERATOR_VERSION,
        "scenario_id": scenario_id,
    }

    expected_weight = None
    expected_serial = None
    expected_accessories = None

    if scenario_id in {"S06", "S07", "S08", "S09"}:
        expected_weight = expected_weight_kg(anchor, rng)
        expected_accessories = accessory_set(anchor, rng)
        expected_serial = serial_for(anchor.product_id, scenario_id, index)

    if scenario_id in {"S03", "S04", "S09", "S12"}:
        evidence.append({
            "evidence_id": f"E-{rid}-CUST-01",
            "return_id": rid,
            "stage": "CUSTOMER",
            "type": "CUSTOMER_PHOTO",
            "image_uri": f"gs://returnguard-evidence/customer/{rid}_customer_01.jpg",
            "claim_metadata": json.dumps({"reason": reason}),
            "uploaded_by": "customer",
            "observed_at": iso(requested + timedelta(hours=2)),
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        })

    if scenario_id in {"S06", "S07", "S08", "S09", "S10", "S11", "S12", "S14"}:
        evidence.append({
            "evidence_id": f"E-{rid}-WH-01",
            "return_id": rid,
            "stage": "WAREHOUSE",
            "type": "WAREHOUSE_PHOTO",
            "image_uri": f"gs://returnguard-evidence/warehouse/{rid}_warehouse_01.jpg",
            "claim_metadata": json.dumps({}),
            "uploaded_by": "warehouse",
            "observed_at": iso(received + timedelta(hours=2)),
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        })

    if scenario_id == "S06":
        actual = round(max(expected_weight * rng.uniform(0.05, 0.22), 0.05), 3)
        inspection = {
            "return_id": rid,
            "actual_weight_kg": actual,
            "returned_serial": None,
            "item_present": False,
            "condition": "not_present",
            "accessories_present": json.dumps([]),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": expected_weight,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        }

    elif scenario_id == "S07":
        inspection = {
            "return_id": rid,
            "actual_weight_kg": round(expected_weight * rng.uniform(0.92, 1.08), 3),
            "returned_serial": serial_for(anchor.product_id, scenario_id, index) + "-SWAP",
            "item_present": True,
            "condition": "salable",
            "accessories_present": json.dumps(expected_accessories),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": expected_weight,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        }

    elif scenario_id == "S08":
        missing = expected_accessories[: max(1, len(expected_accessories) // 2)]
        inspection = {
            "return_id": rid,
            "actual_weight_kg": round(expected_weight * rng.uniform(0.90, 1.02), 3),
            "returned_serial": expected_serial if expected_serial else None,
            "item_present": True,
            "condition": "salable",
            "accessories_present": json.dumps(missing),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": expected_weight,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        }

    elif scenario_id == "S09":
        inspection = {
            "return_id": rid,
            "actual_weight_kg": round(expected_weight * rng.uniform(0.96, 1.04), 3),
            "returned_serial": expected_serial if expected_serial else None,
            "item_present": True,
            "condition": "minor_or_no_damage",
            "accessories_present": json.dumps(expected_accessories),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": expected_weight,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        }

    elif scenario_id == "S03":
        # Merchant fault: physical state supports damage claim; no fraud ground truth is used here.
        inspection = {
            "return_id": rid,
            "actual_weight_kg": None,
            "returned_serial": None,
            "item_present": True,
            "condition": "damaged",
            "accessories_present": json.dumps([]),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": None,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        }

    elif scenario_id == "S12":
        inspection = {
            "return_id": rid,
            "actual_weight_kg": None,
            "returned_serial": None,
            "item_present": True,
            "condition": "defective",
            "accessories_present": json.dumps([]),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": None,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        }

    if scenario_id == "S10":
        # Controlled operational lifecycle pattern; not claimed to exist in The Look.
        base["customer_comment"] = "Worn briefly; returning after a short use period."
        base["usage_context"] = json.dumps({
            "synthetic": True,
            "usage_days": rng.randint(1, 3),
            "repeat_pattern": "short_duration_repeat_return",
        })

    elif scenario_id == "S11":
        # Network linkage uses an optional real attributable IP relationship where possible,
        # while correlated behavior itself is controlled synthetic enrichment.
        candidate = choose(rng, network_pool) if network_pool else None
        base["network_context"] = json.dumps({
            "synthetic_correlated_behavior": True,
            "anchor_user_id": anchor.user_id,
            "related_user_id": candidate[0] if candidate else None,
            "shared_ip_hash": candidate[1] if candidate else None,
            "note": "Shared IP alone is weak evidence; corroboration is required.",
        })

    elif scenario_id == "S14":
        base["customer_comment"] = "Claim and physical evidence are not fully consistent; manual review required."

    # Evidence records are generated as metadata only. Gemini findings come later from actual supplied images.
    for e in evidence:
        e["reference_image_uri"] = (
            f"gs://returnguard-evidence/reference/product_{anchor.product_id}.jpg"
            if scenario_id in {"S03", "S04", "S09", "S12"} else None
        )

    case = {
        "return_request": base,
        "evidence": evidence,
        "inspection": inspection,
        "logistics": logistics,
        "operational_costs": {
            "return_id": rid,
            "reverse_logistics_cost": logistics["reverse_cost"],
            "inspection_cost": 0.0 if inspection is None else (24.0 if anchor.sale_price >= 150 else 12.5),
            "recovery_value": round(max(anchor.product_cost * 0.65, 0.0), 2),
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        },
        "fraud_label": {
            "return_id": rid,
            "fraud_type": {
                "S06": "empty_box",
                "S07": "serial_swap",
                "S08": "missing_accessories",
                "S09": "false_damage",
                "S10": "wardrobing",
                "S11": "coordinated_abuse",
                "S14": "ambiguous",
            }.get(scenario_id, "none"),
            "ground_truth": scenario_id not in {"S01", "S02", "S03", "S04", "S05", "S12", "S13"},
            "source": "controlled_demo_evaluation_only",
            "scenario_id": scenario_id,
            "generator_version": GENERATOR_VERSION,
        },
    }

    return case


def flatten_cases(
    cases: List[Dict[str, Any]],
) -> Dict[str, pd.DataFrame]:
    requests = []
    evidence = []
    inspections = []
    logistics = []
    costs = []
    labels = []
    scenarios = []

    for c in cases:
        r = dict(c["return_request"])
        r["usage_context"] = r.get("usage_context")
        r["network_context"] = r.get("network_context")
        requests.append(r)

        evidence.extend(c["evidence"])
        if c["inspection"] is not None:
            inspections.append(c["inspection"])
        logistics.append(c["logistics"])
        costs.append(c["operational_costs"])
        labels.append(c["fraud_label"])

        scenarios.append({
            "scenario_id": r["scenario_id"],
            "scenario_type": r["scenario_description"],
            "return_id": r["return_id"],
            "seed": r["seed"],
            "generator_version": GENERATOR_VERSION,
            "description": r["scenario_description"],
            "source_type": "synthetic_demo",
        })

    return {
        "return_requests": pd.DataFrame(requests),
        "return_evidence": pd.DataFrame(evidence),
        "return_inspections": pd.DataFrame(inspections),
        "return_logistics": pd.DataFrame(logistics),
        "return_operational_costs": pd.DataFrame(costs),
        "fraud_labels": pd.DataFrame(labels),
        "synthetic_scenarios": pd.DataFrame(scenarios),
    }


def write_outputs(
    output_dir: Path,
    tables: Dict[str, pd.DataFrame],
    product_attributes: pd.DataFrame,
    manifest: Dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    for name, df in tables.items():
        df.to_csv(output_dir / f"{name}.csv", index=False)

    product_attributes.to_csv(output_dir / "product_attributes.csv", index=False)

    with open(output_dir / "generation_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


BQ_SCHEMAS = {
    "product_attributes": [
        ("product_id", "INT64"),
        ("expected_weight_kg", "FLOAT64"),
        ("reference_image_uri", "STRING"),
        ("expected_accessories", "STRING"),
        ("serial_required", "BOOL"),
        ("serial_pattern", "STRING"),
        ("condition_baseline", "STRING"),
        ("recovery_value", "FLOAT64"),
        ("inspection_cost", "FLOAT64"),
        ("source_type", "STRING"),
        ("source_table", "STRING"),
        ("source_record_id", "STRING"),
        ("generator_version", "STRING"),
    ],
    "return_requests": [
        ("return_id", "STRING"),
        ("scenario_id", "STRING"),
        ("scenario_description", "STRING"),
        ("order_item_id", "INT64"),
        ("order_id", "INT64"),
        ("user_id", "INT64"),
        ("product_id", "INT64"),
        ("requested_at", "TIMESTAMP"),
        ("reason", "STRING"),
        ("customer_comment", "STRING"),
        ("responsibility", "STRING"),
        ("status", "STRING"),
        ("assessment_at", "TIMESTAMP"),
        ("source_type", "STRING"),
        ("source_table", "STRING"),
        ("source_record_id", "STRING"),
        ("generator_version", "STRING"),
        ("seed", "INT64"),
        ("anchor_sale_price", "FLOAT64"),
        ("anchor_product_cost", "FLOAT64"),
        ("customer_historical_return_count", "INT64"),
        ("usage_context", "STRING"),
        ("network_context", "STRING"),
    ],
    "return_evidence": [
        ("evidence_id", "STRING"),
        ("return_id", "STRING"),
        ("stage", "STRING"),
        ("type", "STRING"),
        ("image_uri", "STRING"),
        ("claim_metadata", "STRING"),
        ("uploaded_by", "STRING"),
        ("observed_at", "TIMESTAMP"),
        ("source_type", "STRING"),
        ("scenario_id", "STRING"),
        ("generator_version", "STRING"),
        ("reference_image_uri", "STRING"),
    ],
    "return_inspections": [
        ("return_id", "STRING"),
        ("actual_weight_kg", "FLOAT64"),
        ("returned_serial", "STRING"),
        ("item_present", "BOOL"),
        ("condition", "STRING"),
        ("accessories_present", "STRING"),
        ("inspection_location", "STRING"),
        ("inspected_at", "TIMESTAMP"),
        ("expected_weight_kg", "FLOAT64"),
        ("source_type", "STRING"),
        ("scenario_id", "STRING"),
        ("generator_version", "STRING"),
    ],
    "return_logistics": [
        ("return_id", "STRING"),
        ("pickup_at", "TIMESTAMP"),
        ("received_at", "TIMESTAMP"),
        ("reverse_cost", "FLOAT64"),
        ("source_type", "STRING"),
        ("generator_version", "STRING"),
        ("scenario_id", "STRING"),
    ],
    "return_operational_costs": [
        ("return_id", "STRING"),
        ("reverse_logistics_cost", "FLOAT64"),
        ("inspection_cost", "FLOAT64"),
        ("recovery_value", "FLOAT64"),
        ("source_type", "STRING"),
        ("scenario_id", "STRING"),
        ("generator_version", "STRING"),
    ],
    "synthetic_scenarios": [
        ("scenario_id", "STRING"),
        ("scenario_type", "STRING"),
        ("return_id", "STRING"),
        ("seed", "INT64"),
        ("generator_version", "STRING"),
        ("description", "STRING"),
        ("source_type", "STRING"),
    ],
    "fraud_labels": [
        ("return_id", "STRING"),
        ("fraud_type", "STRING"),
        ("ground_truth", "BOOL"),
        ("source", "STRING"),
        ("scenario_id", "STRING"),
        ("generator_version", "STRING"),
    ],
}


def ensure_dataset(client: bigquery.Client, dataset_id: str) -> None:
    dataset = bigquery.Dataset(f"{client.project}.{dataset_id}")
    dataset.location = "US"
    client.create_dataset(dataset, exists_ok=True)


def load_table(
    client: bigquery.Client,
    dataset_id: str,
    table_name: str,
    df: pd.DataFrame,
) -> None:
    table_ref = f"{client.project}.{dataset_id}.{table_name}"
    schema = [
        bigquery.SchemaField(name, field_type, mode="NULLABLE")
        for name, field_type in BQ_SCHEMAS[table_name]
    ]
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    # Convert empty/NaN values to None so BigQuery handles them cleanly.
    clean = df.copy()
    clean = clean.where(pd.notnull(clean), None)
    job = client.load_table_from_dataframe(clean, table_ref, job_config=job_config)
    job.result()


def validate(
    tables: Dict[str, pd.DataFrame],
    product_attributes: pd.DataFrame,
    expected_case_count: int,
) -> None:
    requests = tables["return_requests"]
    assert len(requests) == expected_case_count, (len(requests), expected_case_count)
    assert requests["return_id"].is_unique, "return_id must be unique"
    assert requests["order_item_id"].is_unique, "Each generated case must use a unique source order_item anchor."

    allowed = set(SCENARIO_COUNTS)
    assert set(requests["scenario_id"]).issubset(allowed)

    observed_counts = requests["scenario_id"].value_counts().to_dict()
    assert observed_counts == SCENARIO_COUNTS, f"Scenario distribution mismatch: {observed_counts}"

    # Every synthetic case must point to a real source table/record.
    assert requests["source_table"].notna().all()
    assert requests["source_record_id"].notna().all()

    # Ground truth must remain outside request/risk inputs.
    assert "ground_truth" not in requests.columns
    assert "fraud_type" not in requests.columns

    # Provenance.
    # Most synthetic operational tables use source_type='synthetic_demo'.
    # fraud_labels is intentionally different: it is an evaluation-only
    # answer key and uses source='controlled_demo_evaluation_only' instead.
    for df_name, df in {**tables, "product_attributes": product_attributes}.items():
        if len(df) == 0:
            continue
        assert "generator_version" in df.columns, f"{df_name} missing generator_version"

        if df_name == "fraud_labels":
            assert "source" in df.columns, "fraud_labels missing source"
            assert (
                df["source"] == "controlled_demo_evaluation_only"
            ).all(), "fraud_labels source must be evaluation-only"
            assert "scenario_id" in df.columns, "fraud_labels missing scenario_id"
        else:
            assert "source_type" in df.columns, f"{df_name} missing source_type"
            assert (
                df["source_type"] == "synthetic_demo"
            ).all(), f"{df_name} contains non-synthetic source_type"

    # Future leakage: generated operational observations must not be after assessment_at.
    assessment = pd.to_datetime(requests["assessment_at"], utc=True)
    requested = pd.to_datetime(requests["requested_at"], utc=True)
    assert (requested <= assessment).all()

    # Inspection should happen after request, when present.
    if len(tables["return_inspections"]):
        joined = tables["return_inspections"].merge(
            requests[["return_id", "requested_at", "assessment_at"]],
            on="return_id",
            how="left",
        )
        inspected = pd.to_datetime(joined["inspected_at"], utc=True)
        req = pd.to_datetime(joined["requested_at"], utc=True)
        ass = pd.to_datetime(joined["assessment_at"], utc=True)
        assert (inspected >= req).all()
        assert (inspected <= ass).all()

    # Physical scenario sanity checks.
    insp = tables["return_inspections"]
    if len(insp):
        empty = insp[insp["return_id"].str.contains("RTN-S06-")]
        if len(empty):
            assert (empty["item_present"] == False).all()
        swap = insp[insp["return_id"].str.contains("RTN-S07-")]
        if len(swap):
            assert swap["returned_serial"].notna().all()

    print("VALIDATION PASSED")
    print(f"  Cases: {len(requests)}")
    print(f"  Scenario counts: {observed_counts}")
    print(f"  Product attributes: {len(product_attributes)}")
    print(f"  Evidence rows: {len(tables['return_evidence'])}")
    print(f"  Inspection rows: {len(tables['return_inspections'])}")


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)
    assessment_at = parse_assessment(args.assessment_at)

    client = bigquery.Client(project=args.project_id)

    print("Querying real The Look anchors...")
    anchors_df = query_anchors(client, args.limit_anchors)
    return_counts_df = query_customer_return_counts(client)
    network_df = query_identifiable_network(client)

    if anchors_df.empty:
        raise RuntimeError("No real The Look order-item anchors were returned.")

    anchors, return_counts, _ = make_anchor_objects(anchors_df, return_counts_df, rng)

    network_pool = []
    for row in network_df.itertuples():
        network_pool.append((safe_int(row.user_id), ip_hash(getattr(row, "ip_address", None))))
    network_pool = [x for x in network_pool if x[1]]

    cases: List[Dict[str, Any]] = []
    used_order_items: set[int] = set()
    scenario_map: Dict[str, List[Dict[str, Any]]] = {k: [] for k in SCENARIO_COUNTS}
    anchors_by_product: Dict[int, Anchor] = {}

    for scenario_id, count in SCENARIO_COUNTS.items():
        for i in range(1, count + 1):
            anchor = select_anchor(
                scenario_id,
                anchors,
                return_counts,
                rng,
                used_order_items,
            )
            used_order_items.add(anchor.order_item_id)
            anchors_by_product.setdefault(anchor.product_id, anchor)

            case = make_case(
                scenario_id,
                i,
                anchor,
                assessment_at,
                rng,
                return_counts,
                network_pool,
            )
            case["return_request"]["seed"] = args.seed
            cases.append(case)
            scenario_map[scenario_id].append(case["return_request"])

    tables = flatten_cases(cases)
    product_rows = build_product_attributes(anchors_by_product, scenario_map, rng)
    product_df = pd.DataFrame(product_rows)

    validate(tables, product_df, sum(SCENARIO_COUNTS.values()))

    manifest = {
        "generator_version": GENERATOR_VERSION,
        "seed": args.seed,
        "assessment_at": iso(assessment_at),
        "source_dataset": SOURCE_DATASET,
        "source_tables": [
            f"{SOURCE_DATASET}.users",
            f"{SOURCE_DATASET}.orders",
            f"{SOURCE_DATASET}.order_items",
            f"{SOURCE_DATASET}.products",
            f"{SOURCE_DATASET}.events",
        ],
        "scenario_counts": SCENARIO_COUNTS,
        "total_cases": sum(SCENARIO_COUNTS.values()),
        "generated_tables": [
            "product_attributes",
            "return_requests",
            "return_evidence",
            "return_inspections",
            "return_logistics",
            "return_operational_costs",
            "synthetic_scenarios",
            "fraud_labels",
        ],
        "rules": [
            "Real The Look customers/products/order_items remain the foundation.",
            "Source tables are never mutated.",
            "Historical source return reasons are never fabricated.",
            "Synthetic records are clearly marked synthetic_demo.",
            "Fraud ground truth is evaluation-only and not a risk input.",
            "Anonymous events are not used for customer attribution.",
            "Operational timestamps are ReturnGuard-created.",
            "Gemini findings are not generated here; Gemini runs later on supplied images.",
        ],
    }

    output_dir = Path(args.output_dir)
    write_outputs(output_dir, tables, product_df, manifest)

    if args.load_bigquery:
        print(f"Loading ReturnGuard dataset {args.project_id}.{args.dataset} ...")
        ensure_dataset(client, args.dataset)

        load_table(client, args.dataset, "product_attributes", product_df)
        for name, df in tables.items():
            load_table(client, args.dataset, name, df)

        print("BIGQUERY LOAD COMPLETE")

    print(f"\nGenerated ReturnGuard enrichment at: {output_dir.resolve()}")
    print("No source The Look table was modified.")


if __name__ == "__main__":
    main()
