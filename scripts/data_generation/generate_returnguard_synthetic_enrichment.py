#!/usr/bin/env python3
"""
ReturnGuard deterministic synthetic operational-enrichment generator (V2).

Purpose
-------
Build the ReturnGuard-owned operational layer around REAL The Look anchors.

This script does NOT:
- replace The Look customers/products/orders with fake universes
- mutate the public source tables
- invent historical return reasons
- generate Gemini findings (Gemini runs later on actual supplied images)
- use fraud ground truth as a risk feature
- generate final risk decisions

It DOES:
- query real customer/order_item/product/network anchors from BigQuery
- define explicit scenario contracts across S01-S14 (single-signal) and M01-M08 (multi-signal)
- select anchors with semantic eligibility checks and detailed audit trail
- generate only the missing operational/enrichment records
- produce structured synthetic_network_links for network intelligence
- enforce 5-level semantic validation (structural, relational, temporal, semantic, provenance)
- support safe, non-destructive incremental BigQuery MERGE upserts
- preserve existing historical evaluation records and provenance

Usage
-----
1) Generate local CSV/JSON files preserving existing foundation:
    python scripts/data_generation/generate_returnguard_synthetic_enrichment.py \
        --project-id YOUR_GCP_PROJECT \
        --output-dir ./data/generated_returnguard

2) Generate and incrementally load into BigQuery:
    python scripts/data_generation/generate_returnguard_synthetic_enrichment.py \
        --project-id YOUR_GCP_PROJECT \
        --output-dir ./data/generated_returnguard \
        --load-bigquery

3) Validate existing data only:
    python scripts/data_generation/generate_returnguard_synthetic_enrichment.py \
        --project-id YOUR_GCP_PROJECT \
        --validate-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import string
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd
from google.cloud import bigquery


GENERATOR_VERSION_V1 = "rg-synth-v1.0.2"
GENERATOR_VERSION_V2 = "rg-synth-v2.0.0"
GENERATOR_VERSION = GENERATOR_VERSION_V2
INSPECTION_SERIAL_CONTRACT_VERSION = "rg-synth-v2.0.1"
SOURCE_DATASET = "bigquery-public-data.thelook_ecommerce"

# Base single-signal scenarios (64 total cases)
BASE_SCENARIO_COUNTS: Dict[str, int] = {
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

# Multi-signal scenarios (16 total cases)
MULTI_SCENARIO_COUNTS: Dict[str, int] = {
    "M01": 2,  # Customer + Network
    "M02": 2,  # Customer + Product
    "M03": 2,  # Product + Evidence
    "M04": 2,  # Customer + Network + Evidence
    "M05": 2,  # Customer + Product + Economics
    "M06": 2,  # Customer + Product + Network + Evidence
    "M07": 2,  # Customer + Network + Economics + Evidence
    "M08": 2,  # Customer + Product + Network + Evidence + Economics
}

# These are additive V2 demonstrations.  They intentionally do not replace frozen
# V1 foundation IDs (in particular the original S12 records).
V2_ADDITIONAL_SCENARIO_COUNTS: Dict[str, int] = {
    "S12V2": 2,
}

# Frozen V1 provenance retained outside the active demo dataset.  These source
# products no longer meet the live S12 return-history contract, so their stable
# IDs must never be materialized into an active output on ordinary V2 runs.
RETIRED_ACTIVE_RETURN_IDS: Tuple[str, ...] = (
    "RTN-S12-001",
    "RTN-S12-005",
)
LEGACY_INELIGIBLE_RETURN_IDS: Set[str] = set(RETIRED_ACTIVE_RETURN_IDS)

SCENARIO_COUNTS: Dict[str, int] = {
    **BASE_SCENARIO_COUNTS,
    **MULTI_SCENARIO_COUNTS,
    **V2_ADDITIONAL_SCENARIO_COUNTS,
}

SCENARIO_DESCRIPTIONS: Dict[str, str] = {
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
    "S12V2": "Product-quality issue (V2 verified active demonstration)",
    "S13": "High-cost legitimate return",
    "S14": "Mixed / ambiguous evidence",
    "M01": "Multi-signal: Customer return frequency + Network link",
    "M02": "Multi-signal: Customer return frequency + Elevated product return rate",
    "M03": "Multi-signal: Product return defect + Photo evidence of damage",
    "M04": "Multi-signal: Customer wardrobing + Network link + False damage claim",
    "M05": "Multi-signal: High-value customer + Product return risk + High return costs",
    "M06": "Multi-signal: Network cluster + Defective product + Conflicting evidence",
    "M07": "Multi-signal: High-value network abuse + Empty box physical finding",
    "M08": "Multi-signal: High-value product + Network cluster + Serial swap + High exposure",
}

REASONS: Dict[str, Tuple[str, str]] = {
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
    "S12V2": ("defective", "merchant_fault"),
    "S13": ("changed_mind", "customer_discretionary"),
    "S14": ("damaged", "unknown"),
    "M01": ("wrong_size", "customer_discretionary"),
    "M02": ("defective", "merchant_fault"),
    "M03": ("damaged_on_arrival", "merchant_fault"),
    "M04": ("damaged", "unknown"),
    "M05": ("changed_mind", "customer_discretionary"),
    "M06": ("defective", "unknown"),
    "M07": ("missing_item", "unknown"),
    "M08": ("not_as_described", "unknown"),
}


@dataclass(frozen=True)
class ScenarioContract:
    scenario_id: str
    scenario_name: str
    scenario_type: str  # "SINGLE_SIGNAL" or "MULTI_SIGNAL"
    primary_capability: str
    secondary_capabilities: List[str]
    source_signals: List[str]
    controlled_signals: List[str]
    anchor_requirements: Dict[str, Any]
    required_records: List[str]
    forbidden_controlled_signals: List[str]
    validation_rules: List[str]


def build_scenario_contracts() -> Dict[str, ScenarioContract]:
    contracts = {
        "S01": ScenarioContract(
            scenario_id="S01",
            scenario_name="Honest normal return",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Customer Behavior / Baseline",
            secondary_capabilities=[],
            source_signals=["customer_id", "order_id", "product_id", "sale_price"],
            controlled_signals=[],
            anchor_requirements={"min_price": 0.0},
            required_records=["return_request", "logistics", "operational_costs"],
            forbidden_controlled_signals=["network_links", "serial_swap", "empty_box", "missing_accessories", "false_damage", "wardrobing"],
            validation_rules=["case_is_legitimate", "no_injected_fraud_signals"],
        ),
        "S02": ScenarioContract(
            scenario_id="S02",
            scenario_name="Legitimate heavy returner",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Customer Behavior",
            secondary_capabilities=[],
            source_signals=["customer_id", "order_id", "product_id"],
            controlled_signals=["customer_historical_return_count"],
            anchor_requirements={"min_historical_returns": 3},
            required_records=["return_request", "logistics", "operational_costs"],
            forbidden_controlled_signals=["network_links", "serial_swap", "empty_box", "missing_accessories", "false_damage", "wardrobing"],
            validation_rules=["customer_historical_return_count >= 3", "case_is_legitimate"],
        ),
        "S03": ScenarioContract(
            scenario_id="S03",
            scenario_name="Merchant-fault damaged return",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Evidence & Inspection",
            secondary_capabilities=["Product Intelligence"],
            source_signals=["customer_id", "product_id"],
            controlled_signals=["customer_photo_evidence", "inspection_damaged_condition"],
            anchor_requirements={"min_price": 0.0},
            required_records=["return_request", "customer_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "serial_swap", "empty_box", "wardrobing"],
            validation_rules=["inspection.condition == 'damaged'", "evidence.stage == 'CUSTOMER'"],
        ),
        "S04": ScenarioContract(
            scenario_id="S04",
            scenario_name="Wrong item shipped",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Evidence & Inspection",
            secondary_capabilities=[],
            source_signals=["order_item_id", "product_id"],
            controlled_signals=["customer_photo_evidence"],
            anchor_requirements={"min_price": 0.0},
            required_records=["return_request", "customer_evidence"],
            forbidden_controlled_signals=["network_links", "serial_swap", "empty_box", "wardrobing"],
            validation_rules=["reason == 'wrong_item_shipped'", "responsibility == 'merchant_fault'"],
        ),
        "S05": ScenarioContract(
            scenario_id="S05",
            scenario_name="High-value legitimate return",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Economics",
            secondary_capabilities=["Customer Behavior"],
            source_signals=["sale_price", "product_cost"],
            controlled_signals=[],
            anchor_requirements={"min_price": 250.0},
            required_records=["return_request", "logistics", "operational_costs"],
            forbidden_controlled_signals=["network_links", "serial_swap", "empty_box", "false_damage", "wardrobing"],
            validation_rules=["anchor_sale_price >= 250.0", "case_is_legitimate"],
        ),
        "S06": ScenarioContract(
            scenario_id="S06",
            scenario_name="Empty box / missing item",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Inspection & Physical Finding",
            secondary_capabilities=[],
            source_signals=["order_item_id", "product_id"],
            controlled_signals=["warehouse_photo_evidence", "inspection_item_present_false"],
            anchor_requirements={"min_price": 50.0},
            required_records=["return_request", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "serial_swap", "missing_accessories", "wardrobing"],
            validation_rules=["inspection.item_present == False", "inspection.condition == 'not_present'"],
        ),
        "S07": ScenarioContract(
            scenario_id="S07",
            scenario_name="Serial/product swap",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Inspection & Serial Verification",
            secondary_capabilities=[],
            source_signals=["product_id"],
            controlled_signals=["returned_serial_mismatch"],
            anchor_requirements={"min_price": 50.0},
            required_records=["return_request", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "empty_box", "missing_accessories", "wardrobing"],
            validation_rules=["inspection.returned_serial != expected_serial"],
        ),
        "S08": ScenarioContract(
            scenario_id="S08",
            scenario_name="Missing accessories",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Inspection & Completeness",
            secondary_capabilities=[],
            source_signals=["product_id", "category"],
            controlled_signals=["inspection_accessories_subset"],
            anchor_requirements={"min_price": 50.0, "requires_accessories": True},
            required_records=["return_request", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap", "wardrobing"],
            validation_rules=["len(accessories_present) < len(expected_accessories)"],
        ),
        "S09": ScenarioContract(
            scenario_id="S09",
            scenario_name="False damage claim",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Evidence & Inspection Inconsistency",
            secondary_capabilities=[],
            source_signals=["product_id"],
            controlled_signals=["customer_damage_claim", "warehouse_clean_inspection"],
            anchor_requirements={"min_price": 50.0},
            required_records=["return_request", "customer_evidence", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap"],
            validation_rules=["reason == 'damaged'", "inspection.condition == 'minor_or_no_damage'"],
        ),
        "S10": ScenarioContract(
            scenario_id="S10",
            scenario_name="Wardrobing / short-duration return",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Customer Behavior & Usage Context",
            secondary_capabilities=[],
            source_signals=["category", "product_id"],
            controlled_signals=["usage_context_short_duration"],
            anchor_requirements={"min_price": 0.0},
            required_records=["return_request", "warehouse_evidence"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap", "false_damage"],
            validation_rules=["usage_context.usage_days <= 3", "usage_context.repeat_pattern == 'short_duration_repeat_return'"],
        ),
        "S11": ScenarioContract(
            scenario_id="S11",
            scenario_name="Coordinated abuse / network",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Network Intelligence",
            secondary_capabilities=[],
            source_signals=["user_id", "attributable_events"],
            controlled_signals=["synthetic_network_links"],
            anchor_requirements={"network_pool_required": True},
            required_records=["return_request", "synthetic_network_links", "warehouse_evidence"],
            forbidden_controlled_signals=["empty_box", "serial_swap", "missing_accessories", "false_damage"],
            validation_rules=["network_links_count >= 1", "user_id != linked_user_id"],
        ),
        "S12": ScenarioContract(
            scenario_id="S12",
            scenario_name="Product-quality issue",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Product Intelligence",
            secondary_capabilities=["Evidence & Inspection"],
            source_signals=["product_id", "product_return_rate"],
            controlled_signals=["defective_condition_inspection"],
            anchor_requirements={"product_with_return_history": True},
            required_records=["return_request", "customer_evidence", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap", "wardrobing"],
            validation_rules=["reason == 'defective'", "responsibility == 'merchant_fault'", "inspection.condition == 'defective'"],
        ),
        "S12V2": ScenarioContract(
            scenario_id="S12V2",
            scenario_name="Product-quality issue (V2 verified active demonstration)",
            scenario_type="V2_ACTIVE_DEMONSTRATION",
            primary_capability="Product Intelligence",
            secondary_capabilities=["Evidence & Inspection"],
            source_signals=["product_id", "product_return_rate"],
            controlled_signals=["defective_condition_inspection"],
            anchor_requirements={"product_with_return_history": True},
            required_records=["return_request", "customer_evidence", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap", "wardrobing"],
            validation_rules=["reason == 'defective'", "responsibility == 'merchant_fault'", "inspection.condition == 'defective'", "product_return_history > 0"],
        ),
        "S13": ScenarioContract(
            scenario_id="S13",
            scenario_name="High-cost legitimate return",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Economics",
            secondary_capabilities=["Logistics"],
            source_signals=["sale_price", "reverse_cost"],
            controlled_signals=[],
            anchor_requirements={"min_price": 100.0},
            required_records=["return_request", "logistics", "operational_costs"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap", "false_damage"],
            validation_rules=["reverse_logistics_cost > 0.0", "case_is_legitimate"],
        ),
        "S14": ScenarioContract(
            scenario_id="S14",
            scenario_name="Mixed / ambiguous evidence",
            scenario_type="SINGLE_SIGNAL",
            primary_capability="Multi-signal Ambiguity / Manual Review",
            secondary_capabilities=["Evidence & Inspection"],
            source_signals=["product_id"],
            controlled_signals=["ambiguous_claim_comment"],
            anchor_requirements={"min_price": 0.0},
            required_records=["return_request", "warehouse_evidence"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap"],
            validation_rules=["'manual review required' in customer_comment.lower()"],
        ),
        # Multi-signal scenarios (M01-M08)
        "M01": ScenarioContract(
            scenario_id="M01",
            scenario_name="Multi-signal: Customer return frequency + Network link",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Customer Behavior",
            secondary_capabilities=["Network Intelligence"],
            source_signals=["user_id", "order_id", "product_id"],
            controlled_signals=["customer_historical_return_count", "synthetic_network_links"],
            anchor_requirements={"network_pool_required": True},
            required_records=["return_request", "synthetic_network_links", "warehouse_evidence"],
            forbidden_controlled_signals=["empty_box", "serial_swap"],
            validation_rules=["customer_historical_return_count >= 3", "network_links_count >= 1"],
        ),
        "M02": ScenarioContract(
            scenario_id="M02",
            scenario_name="Multi-signal: Customer return frequency + Product defect rate",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Customer Behavior",
            secondary_capabilities=["Product Intelligence"],
            source_signals=["user_id", "product_id", "product_return_rate"],
            controlled_signals=["customer_historical_return_count"],
            anchor_requirements={"product_with_return_history": True},
            required_records=["return_request", "customer_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap"],
            validation_rules=["customer_historical_return_count >= 3", "reason == 'defective'"],
        ),
        "M03": ScenarioContract(
            scenario_id="M03",
            scenario_name="Multi-signal: Product return defect + Photo evidence of damage",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Product Intelligence",
            secondary_capabilities=["Evidence & Inspection"],
            source_signals=["product_id", "product_return_rate"],
            controlled_signals=["customer_photo_evidence", "inspection_damaged_condition"],
            anchor_requirements={"product_with_return_history": True},
            required_records=["return_request", "customer_evidence", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap"],
            validation_rules=["inspection.condition == 'damaged'", "evidence.stage == 'CUSTOMER'"],
        ),
        "M04": ScenarioContract(
            scenario_id="M04",
            scenario_name="Multi-signal: Customer wardrobing + Network link + False damage claim",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Network Intelligence",
            secondary_capabilities=["Customer Behavior", "Evidence & Inspection"],
            source_signals=["user_id", "product_id"],
            controlled_signals=["usage_context_short_duration", "synthetic_network_links", "warehouse_clean_inspection"],
            anchor_requirements={"network_pool_required": True},
            required_records=["return_request", "synthetic_network_links", "customer_evidence", "inspection"],
            forbidden_controlled_signals=["empty_box", "serial_swap"],
            validation_rules=["network_links_count >= 1", "usage_context.usage_days <= 3", "inspection.condition == 'minor_or_no_damage'"],
        ),
        "M05": ScenarioContract(
            scenario_id="M05",
            scenario_name="Multi-signal: High-value customer + Product return risk + High return costs",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Economics",
            secondary_capabilities=["Customer Behavior", "Product Intelligence"],
            source_signals=["sale_price", "product_id"],
            controlled_signals=["customer_historical_return_count"],
            anchor_requirements={"min_price": 250.0, "product_with_return_history": True},
            required_records=["return_request", "logistics", "operational_costs"],
            forbidden_controlled_signals=["network_links", "empty_box", "serial_swap"],
            validation_rules=["anchor_sale_price >= 250.0", "customer_historical_return_count >= 2", "reverse_logistics_cost >= 50.0"],
        ),
        "M06": ScenarioContract(
            scenario_id="M06",
            scenario_name="Multi-signal: Network cluster + Defective product + Conflicting evidence",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Network Intelligence",
            secondary_capabilities=["Product Intelligence", "Evidence & Inspection"],
            source_signals=["product_id", "product_return_rate"],
            controlled_signals=["synthetic_network_links", "conflicting_claim_comment"],
            anchor_requirements={"network_pool_required": True, "product_with_return_history": True},
            required_records=["return_request", "synthetic_network_links", "customer_evidence", "inspection"],
            forbidden_controlled_signals=["empty_box", "serial_swap"],
            validation_rules=["network_links_count >= 1", "reason == 'defective'"],
        ),
        "M07": ScenarioContract(
            scenario_id="M07",
            scenario_name="Multi-signal: High-value network abuse + Empty box physical finding",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Network Intelligence",
            secondary_capabilities=["Economics", "Inspection & Physical Finding"],
            source_signals=["sale_price", "user_id"],
            controlled_signals=["synthetic_network_links", "inspection_item_present_false"],
            anchor_requirements={"min_price": 250.0, "network_pool_required": True},
            required_records=["return_request", "synthetic_network_links", "warehouse_evidence", "inspection"],
            forbidden_controlled_signals=["serial_swap"],
            validation_rules=["network_links_count >= 1", "inspection.item_present == False", "anchor_sale_price >= 250.0"],
        ),
        "M08": ScenarioContract(
            scenario_id="M08",
            scenario_name="Multi-signal: High-value product + Network cluster + Serial swap + High exposure",
            scenario_type="MULTI_SIGNAL",
            primary_capability="Combined Risk Engine",
            secondary_capabilities=["Customer Behavior", "Product Intelligence", "Network Intelligence", "Evidence & Inspection", "Economics"],
            source_signals=["sale_price", "product_id", "user_id"],
            controlled_signals=["synthetic_network_links", "returned_serial_mismatch"],
            anchor_requirements={"min_price": 250.0, "network_pool_required": True, "product_with_return_history": True},
            required_records=["return_request", "synthetic_network_links", "warehouse_evidence", "inspection", "operational_costs"],
            forbidden_controlled_signals=["empty_box"],
            validation_rules=["network_links_count >= 1", "inspection.returned_serial != expected_serial", "anchor_sale_price >= 250.0"],
        ),
    }
    return contracts


SCENARIO_CONTRACTS: Dict[str, ScenarioContract] = build_scenario_contracts()


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


@dataclass
class AnchorAudit:
    scenario_id: str
    index: int
    candidates_evaluated: int
    rejected_reasons: List[str]
    selected_order_item_id: Optional[int]
    selected_user_id: Optional[int]
    selected_product_id: Optional[int]
    status: str  # "ACCEPTED" or "REJECTED"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate ReturnGuard controlled synthetic enrichment (V2).")
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
                   help="Incrementally load ReturnGuard BigQuery tables via safe MERGE after generation.")
    p.add_argument("--limit-anchors", type=int, default=2500,
                   help="Maximum number of source anchor rows fetched for local selection.")
    p.add_argument("--preserve-existing", action="store_true", default=True,
                   help="Preserve existing S01-S14 baseline cases and append multi-signal scenarios incrementally.")
    p.add_argument("--validate-only", action="store_true",
                   help="Run 5-level validation on existing files in output-dir without re-generating.")
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


def serial_for(product_id: int, scenario_id: str, index: int, version: str = GENERATOR_VERSION) -> str:
    """
    Deterministic synthetic serial. It is intentionally ReturnGuard-owned.
    """
    digest = stable_hash(f"{version}|{product_id}|{scenario_id}|{index}")[:10].upper()
    return f"RG-{product_id}-{digest}"


def scenario_return_id(scenario_id: str, index: int) -> str:
    return f"RTN-{scenario_id}-{index:03d}"


def choose(rng: random.Random, values: Sequence[Any]) -> Any:
    return values[rng.randrange(len(values))]


def expected_weight_kg(anchor: Anchor, rng: random.Random) -> float:
    """
    Synthetic operational attribute only.
    """
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
    return list(dict.fromkeys(common))


# ----------------------------------------------------------------------
# BigQuery Source Queries
# ----------------------------------------------------------------------

def query_anchors(client: bigquery.Client, limit: int, assessment_at: datetime) -> pd.DataFrame:
    """
    Fetch real The Look anchors created on or before assessment_at.
    Excludes already returned items so generated cases represent operational returns.
    """
    cutoff = iso(assessment_at)
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
      AND LOWER(CAST(oi.status AS STRING)) != 'cancelled'
      AND oi.user_id IS NOT NULL
      AND oi.product_id IS NOT NULL
      AND oi.created_at <= TIMESTAMP('{cutoff}')
    ORDER BY oi.id
    LIMIT {int(limit)}
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def query_customer_return_counts(client: bigquery.Client, assessment_at: datetime) -> pd.DataFrame:
    """
    Counts customer historical returns on or before assessment_at, excluding cancelled orders.
    """
    cutoff = iso(assessment_at)
    query = f"""
    SELECT
      user_id,
      COUNTIF(LOWER(CAST(status AS STRING)) = 'returned') AS returned_count,
      COUNTIF(LOWER(CAST(status AS STRING)) != 'cancelled') AS item_count
    FROM `{SOURCE_DATASET}.order_items`
    WHERE user_id IS NOT NULL
      AND created_at <= TIMESTAMP('{cutoff}')
    GROUP BY user_id
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def query_product_return_metrics(client: bigquery.Client, assessment_at: datetime) -> pd.DataFrame:
    """
    Aggregates product return metrics on or before assessment_at, excluding cancelled orders.
    """
    cutoff = iso(assessment_at)
    query = f"""
    SELECT
      product_id,
      COUNTIF(LOWER(status) != 'cancelled') as observed_items,
      COUNTIF(LOWER(status) = 'returned') as returned_items,
      ROUND(COUNTIF(LOWER(status) = 'returned') / NULLIF(COUNTIF(LOWER(status) != 'cancelled'), 0), 4) as return_rate
    FROM `{SOURCE_DATASET}.order_items`
    WHERE product_id IS NOT NULL
      AND created_at <= TIMESTAMP('{cutoff}')
    GROUP BY product_id
    HAVING observed_items >= 2
    ORDER BY return_rate DESC, returned_items DESC
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def product_metric_excluding_anchor(
    product_metrics: Dict[int, Dict[str, Any]],
    anchor: Anchor,
) -> Optional[Dict[str, Any]]:
    """Return the candidate-specific, leave-one-out product metric.

    ``query_anchors`` only returns non-cancelled, non-returned anchors created
    at or before assessment_at.  Consequently each eligible candidate appears
    once in the global non-cancelled denominator and never in its returned
    numerator.  Removing that row here is equivalent to applying
    ``order_item_id != candidate.order_item_id`` before aggregation, without
    imposing an invalid single-anchor predicate on the global source query.
    """
    metric = product_metrics.get(anchor.product_id)
    if not metric:
        return None

    observed_items = max(int(metric["observed_items"]) - 1, 0)
    returned_items = int(metric["returned_items"])
    return {
        "observed_items": observed_items,
        "returned_items": returned_items,
        "return_rate": (round(returned_items / observed_items, 4) if observed_items else None),
    }


def query_identifiable_network(client: bigquery.Client, assessment_at: datetime) -> pd.DataFrame:
    """
    Attributable non-anonymous events on or before assessment_at.
    """
    cutoff = iso(assessment_at)
    query = f"""
    SELECT
      user_id,
      ip_address
    FROM `{SOURCE_DATASET}.events`
    WHERE user_id IS NOT NULL
      AND ip_address IS NOT NULL
      AND created_at <= TIMESTAMP('{cutoff}')
    GROUP BY user_id, ip_address
    """
    return client.query(query).result().to_dataframe(create_bqstorage_client=False)


def make_anchor_objects(
    anchors_df: pd.DataFrame,
    return_counts_df: pd.DataFrame,
) -> Tuple[List[Anchor], Dict[int, int]]:
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
    return anchors, rc


# ----------------------------------------------------------------------
# Anchor Selection with Semantic Eligibility & Rejection Handling
# ----------------------------------------------------------------------

def select_anchor_with_audit(
    scenario_id: str,
    index: int,
    anchors: List[Anchor],
    return_counts: Dict[int, int],
    product_metrics: Dict[int, Dict[str, Any]],
    rng: random.Random,
    used_order_items: Set[int],
    network_pool: List[Tuple[int, str]],
) -> Tuple[Anchor, AnchorAudit]:
    contract = SCENARIO_CONTRACTS[scenario_id]
    available = [a for a in anchors if a.order_item_id not in used_order_items]
    if not available:
        raise RuntimeError(f"Ran out of unique real order_item anchors for {scenario_id}-{index:03d}.")

    rejected_reasons: List[str] = []
    candidates_evaluated = 0

    shuffled = list(available)
    rng.shuffle(shuffled)

    selected: Optional[Anchor] = None
    for candidate in shuffled:
        candidates_evaluated += 1
        rejection = None

        min_price = contract.anchor_requirements.get("min_price", 0.0)
        if candidate.sale_price < min_price:
            rejection = f"Price ${candidate.sale_price:.2f} < required min ${min_price:.2f}"

        if not rejection and contract.anchor_requirements.get("product_with_return_history"):
            p_metric = product_metric_excluding_anchor(product_metrics, candidate)
            if not p_metric or p_metric.get("returned_items", 0) < 1:
                rejection = f"Product {candidate.product_id} lacks non-zero return history"

        if not rejection and contract.anchor_requirements.get("requires_accessories"):
            acc = accessory_set(candidate, rng)
            if len(acc) < 2:
                rejection = f"Product category '{candidate.category}' does not yield expected accessories"

        if not rejection and contract.anchor_requirements.get("network_pool_required"):
            eligible_peers = [p for p in network_pool if p[0] != candidate.user_id]
            if not eligible_peers:
                rejection = "No eligible distinct peer in network pool"

        if rejection:
            rejected_reasons.append(f"Anchor {candidate.order_item_id}: {rejection}")
            continue

        selected = candidate
        break

    if selected is None:
        selected = available[0]
        rejected_reasons.append("Fallback candidate selected due to strict constraint exhaustion")

    audit = AnchorAudit(
        scenario_id=scenario_id,
        index=index,
        candidates_evaluated=candidates_evaluated,
        rejected_reasons=rejected_reasons[:5],
        selected_order_item_id=selected.order_item_id,
        selected_user_id=selected.user_id,
        selected_product_id=selected.product_id,
        status="ACCEPTED",
    )
    return selected, audit


def build_product_attributes(
    anchors_by_product: Dict[int, Anchor],
    scenario_map: Dict[str, List[Dict[str, Any]]],
    rng: random.Random,
    version: str = GENERATOR_VERSION,
) -> List[Dict[str, Any]]:
    rows = []
    physical_products = set()
    for s_id, cases in scenario_map.items():
        contract = SCENARIO_CONTRACTS.get(s_id)
        if contract and (
            "inspection" in contract.required_records
            or contract.anchor_requirements.get("min_price", 0) >= 250
            or s_id in {"S05", "S06", "S07", "S08", "S09", "S12", "S13", "M02", "M03", "M04", "M05", "M06", "M07", "M08"}
        ):
            for case in cases:
                physical_products.add(case["product_id"])

    for product_id in sorted(physical_products):
        if product_id not in anchors_by_product:
            continue
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
            "generator_version": version,
        })
    return rows


# ----------------------------------------------------------------------
# Case Builder
# ----------------------------------------------------------------------

def make_case(
    scenario_id: str,
    index: int,
    anchor: Anchor,
    assessment_at: datetime,
    rng: random.Random,
    return_counts: Dict[int, int],
    network_pool: List[Tuple[int, str]],
    version: str = GENERATOR_VERSION,
) -> Dict[str, Any]:
    rid = scenario_return_id(scenario_id, index)
    reason, responsibility = REASONS[scenario_id]

    requested = assessment_at - timedelta(days=rng.randint(7, 12), hours=rng.randint(0, 20))
    pickup = requested + timedelta(hours=rng.randint(12, 48))
    received = pickup + timedelta(days=rng.randint(1, 4))

    if scenario_id in {"S02", "M01", "M02"}:
        hist_returns = max(return_counts.get(anchor.user_id, 0), rng.randint(3, 5))
    elif scenario_id == "M05":
        hist_returns = max(return_counts.get(anchor.user_id, 0), rng.randint(2, 4))
    else:
        hist_returns = return_counts.get(anchor.user_id, 0)

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
        "generator_version": version,
        "seed": None,
        "anchor_sale_price": anchor.sale_price,
        "anchor_product_cost": anchor.product_cost,
        "customer_historical_return_count": hist_returns,
        "usage_context": None,
        "network_context": None,
    }

    evidence: List[Dict[str, Any]] = []
    inspection: Optional[Dict[str, Any]] = None
    network_links: List[Dict[str, Any]] = []

    reverse_cost_min = 50.0 if scenario_id == "M05" else 35.0
    logistics = {
        "return_id": rid,
        "pickup_at": iso(pickup),
        "received_at": iso(received),
        "reverse_cost": round(rng.uniform(reverse_cost_min, 120), 2),
        "source_type": "synthetic_demo",
        "generator_version": version,
        "scenario_id": scenario_id,
    }

    expected_weight = expected_weight_kg(anchor, rng)
    expected_accessories = accessory_set(anchor, rng)
    expected_serial = serial_for(anchor.product_id, scenario_id, index, version)

    if scenario_id in {"S03", "S04", "S09", "S12", "S12V2", "M02", "M03", "M04", "M06"}:
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
            "generator_version": version,
            "reference_image_uri": f"gs://returnguard-evidence/reference/product_{anchor.product_id}.jpg",
        })

    if scenario_id in {"S06", "S07", "S08", "S09", "S10", "S11", "S12", "S12V2", "S14",
                       "M01", "M03", "M04", "M06", "M07", "M08"}:
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
            "generator_version": version,
            "reference_image_uri": None,
        })

    if scenario_id in {"S06", "M07"}:
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
            "generator_version": version,
        }

    elif scenario_id in {"S07", "M08"}:
        inspection = {
            "return_id": rid,
            "actual_weight_kg": round(expected_weight * rng.uniform(0.92, 1.08), 3),
            "expected_serial": expected_serial,
            "returned_serial": f"{expected_serial}-SWAP",
            "item_present": True,
            "condition": "salable",
            "accessories_present": json.dumps(expected_accessories),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": expected_weight,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": INSPECTION_SERIAL_CONTRACT_VERSION,
        }

    elif scenario_id == "S08":
        missing = expected_accessories[: max(1, len(expected_accessories) // 2)]
        inspection = {
            "return_id": rid,
            "actual_weight_kg": round(expected_weight * rng.uniform(0.90, 1.02), 3),
            "returned_serial": expected_serial,
            "item_present": True,
            "condition": "salable",
            "accessories_present": json.dumps(missing),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": expected_weight,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": version,
        }

    elif scenario_id in {"S09", "M04"}:
        inspection = {
            "return_id": rid,
            "actual_weight_kg": round(expected_weight * rng.uniform(0.96, 1.04), 3),
            "returned_serial": expected_serial,
            "item_present": True,
            "condition": "minor_or_no_damage",
            "accessories_present": json.dumps(expected_accessories),
            "inspection_location": "WH-01",
            "inspected_at": iso(received + timedelta(hours=4)),
            "expected_weight_kg": expected_weight,
            "source_type": "synthetic_demo",
            "scenario_id": scenario_id,
            "generator_version": version,
        }

    elif scenario_id in {"S03", "M03"}:
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
            "generator_version": version,
        }

    elif scenario_id in {"S12", "S12V2", "M02", "M06"}:
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
            "generator_version": version,
        }

    if scenario_id in {"S10", "M04"}:
        base["customer_comment"] = "Worn briefly; returning after a short use period."
        base["usage_context"] = json.dumps({
            "synthetic": True,
            "usage_days": rng.randint(1, 3),
            "repeat_pattern": "short_duration_repeat_return",
        })

    if scenario_id in {"S11", "M01", "M04", "M06", "M07", "M08"}:
        peer = None
        eligible_peers = [p for p in network_pool if p[0] != anchor.user_id]
        if eligible_peers:
            peer = choose(rng, eligible_peers)

        linked_user_id = peer[0] if peer else anchor.user_id + 9999
        shared_ip = peer[1] if peer else ip_hash(f"synthetic-net-{index}")
        link_id = f"NLK-{rid}-01"

        network_links.append({
            "network_link_id": link_id,
            "scenario_id": scenario_id,
            "return_id": rid,
            "user_id": anchor.user_id,
            "linked_user_id": linked_user_id,
            "network_identifier": shared_ip,
            "relationship_type": "shared_ip_cluster",
            "first_observed_at": iso(requested - timedelta(days=20)),
            "last_observed_at": iso(requested - timedelta(hours=6)),
            "source_type": "synthetic_demo",
            "generator_version": version,
        })

        base["network_context"] = json.dumps({
            "synthetic_correlated_behavior": True,
            "anchor_user_id": anchor.user_id,
            "related_user_id": linked_user_id,
            "shared_ip_hash": shared_ip,
            "note": "Shared IP alone is contextual evidence; corroboration is required.",
        })

    if scenario_id in {"S14", "M06"}:
        base["customer_comment"] = "Claim and physical evidence are not fully consistent; manual review required."

    insp_cost = 0.0 if inspection is None else (24.0 if anchor.sale_price >= 150 else 12.5)
    recov_val = round(max(anchor.product_cost * 0.65, 0.0), 2)
    operational_costs = {
        "return_id": rid,
        "reverse_logistics_cost": logistics["reverse_cost"],
        "inspection_cost": insp_cost,
        "recovery_value": recov_val,
        "source_type": "synthetic_demo",
        "scenario_id": scenario_id,
        "generator_version": version,
    }

    is_fraud = scenario_id not in {"S01", "S02", "S03", "S04", "S05", "S12", "S12V2", "S13", "M02", "M03"}
    fraud_type_map = {
        "S06": "empty_box",
        "S07": "serial_swap",
        "S08": "missing_accessories",
        "S09": "false_damage",
        "S10": "wardrobing",
        "S11": "coordinated_abuse",
        "S14": "ambiguous",
        "M01": "coordinated_abuse",
        "M04": "multi_signal_abuse",
        "M05": "elevated_risk_economics",
        "M06": "coordinated_defect_abuse",
        "M07": "high_value_empty_box",
        "M08": "high_value_serial_swap",
    }

    fraud_label = {
        "return_id": rid,
        "fraud_type": fraud_type_map.get(scenario_id, "none"),
        "ground_truth": is_fraud,
        "source": "controlled_demo_evaluation_only",
        "scenario_id": scenario_id,
        "generator_version": version,
    }

    return {
        "return_request": base,
        "evidence": evidence,
        "inspection": inspection,
        "logistics": logistics,
        "operational_costs": operational_costs,
        "fraud_label": fraud_label,
        "network_links": network_links,
    }


def flatten_cases(
    cases: List[Dict[str, Any]],
    version: str = GENERATOR_VERSION,
) -> Dict[str, pd.DataFrame]:
    requests = []
    evidence = []
    inspections = []
    logistics = []
    costs = []
    labels = []
    scenarios = []
    network_links = []

    for c in cases:
        r = dict(c["return_request"])
        requests.append(r)

        evidence.extend(c["evidence"])
        if c["inspection"] is not None:
            inspections.append(c["inspection"])
        logistics.append(c["logistics"])
        costs.append(c["operational_costs"])
        labels.append(c["fraud_label"])
        network_links.extend(c.get("network_links", []))

        scenarios.append({
            "scenario_id": r["scenario_id"],
            "scenario_type": r["scenario_description"],
            "return_id": r["return_id"],
            "seed": r["seed"],
            "generator_version": r.get("generator_version", version),
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
        "synthetic_network_links": pd.DataFrame(network_links),
    }


def extract_existing_network_links(
    requests_df: pd.DataFrame,
    version: str = GENERATOR_VERSION_V1,
) -> List[Dict[str, Any]]:
    links = []
    s11_rows = requests_df[requests_df["scenario_id"] == "S11"]
    for _, row in s11_rows.iterrows():
        ctx_raw = row.get("network_context")
        if not ctx_raw or pd.isna(ctx_raw):
            continue
        try:
            ctx = json.loads(ctx_raw)
            rid = row["return_id"]
            user_id = safe_int(row["user_id"])
            linked_user_id = safe_int(ctx.get("related_user_id", user_id + 1000))
            shared_ip = ctx.get("shared_ip_hash", "")
            req_at = pd.to_datetime(row["requested_at"])
            links.append({
                "network_link_id": f"NLK-{rid}-01",
                "scenario_id": "S11",
                "return_id": rid,
                "user_id": user_id,
                "linked_user_id": linked_user_id,
                "network_identifier": shared_ip,
                "relationship_type": "shared_ip_cluster",
                "first_observed_at": iso(req_at - timedelta(days=20)),
                "last_observed_at": iso(req_at - timedelta(hours=6)),
                "source_type": "synthetic_demo",
                "generator_version": row.get("generator_version", version),
            })
        except Exception:
            continue
    return links


# ----------------------------------------------------------------------
# BigQuery Schema & Primary Key Definitions
# ----------------------------------------------------------------------

BQ_SCHEMAS: Dict[str, List[Tuple[str, str]]] = {
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
        ("expected_serial", "STRING"),
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
    "synthetic_network_links": [
        ("network_link_id", "STRING"),
        ("scenario_id", "STRING"),
        ("return_id", "STRING"),
        ("user_id", "INT64"),
        ("linked_user_id", "INT64"),
        ("network_identifier", "STRING"),
        ("relationship_type", "STRING"),
        ("first_observed_at", "TIMESTAMP"),
        ("last_observed_at", "TIMESTAMP"),
        ("source_type", "STRING"),
        ("generator_version", "STRING"),
    ],
}

PRIMARY_KEYS: Dict[str, List[str]] = {
    "product_attributes": ["product_id"],
    "return_requests": ["return_id"],
    "return_evidence": ["evidence_id"],
    "return_inspections": ["return_id"],
    "return_logistics": ["return_id"],
    "return_operational_costs": ["return_id"],
    "synthetic_scenarios": ["return_id"],
    "fraud_labels": ["return_id"],
    "synthetic_network_links": ["network_link_id"],
}


def upsert_local_rows(existing: pd.DataFrame, incoming: pd.DataFrame, table_name: str) -> pd.DataFrame:
    """Local counterpart of the BigQuery MERGE: one authoritative row per key."""
    if existing.empty:
        return incoming.copy()
    if incoming.empty:
        return existing.copy()
    keys = PRIMARY_KEYS[table_name]
    combined = pd.concat([existing, incoming], ignore_index=True)
    return combined.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)


def exclude_legacy_ineligible_rows(tables: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Return the active dataset, excluding explicitly archived legacy cases."""
    active: Dict[str, pd.DataFrame] = {}
    for table_name, df in tables.items():
        if df.empty or "return_id" not in df.columns:
            active[table_name] = df.copy()
        else:
            active[table_name] = df[~df["return_id"].isin(LEGACY_INELIGIBLE_RETURN_IDS)].copy()
    return active


def apply_v2_contract_migrations(tables: Dict[str, pd.DataFrame]) -> None:
    """Apply deterministic, version-scoped corrections without touching V1 rows."""
    logistics = tables.get("return_logistics", pd.DataFrame())
    costs = tables.get("return_operational_costs", pd.DataFrame())
    if logistics.empty or costs.empty:
        return

    logistics_mask = (
        (logistics["scenario_id"] == "M05")
        & (logistics["generator_version"] == GENERATOR_VERSION_V2)
    )
    costs_mask = (
        (costs["scenario_id"] == "M05")
        & (costs["generator_version"] == GENERATOR_VERSION_V2)
    )
    logistics.loc[logistics_mask, "reverse_cost"] = logistics.loc[logistics_mask, "reverse_cost"].clip(lower=50.0)
    costs.loc[costs_mask, "reverse_logistics_cost"] = costs.loc[costs_mask, "reverse_logistics_cost"].clip(lower=50.0)


def return_grained_table_names() -> List[str]:
    """Derive cleanup scope from schemas; product-grained tables are excluded."""
    return [
        table_name
        for table_name, schema in BQ_SCHEMAS.items()
        if any(column_name == "return_id" for column_name, _ in schema)
    ]


def cleanup_retired_active_returns(client: bigquery.Client, dataset_id: str) -> List[str]:
    """Delete only explicitly retired return IDs from return-grained tables.

    This is intentionally separate from ``upsert_table``.  Missing tables and
    repeated execution are harmless, and product_attributes is not in the
    schema-derived scope because it has no return_id column.
    """
    retired = list(RETIRED_ACTIVE_RETURN_IDS)
    cleaned_tables: List[str] = []
    for table_name in return_grained_table_names():
        table_ref = f"{client.project}.{dataset_id}.{table_name}"
        try:
            client.get_table(table_ref)
        except Exception:
            continue

        query = f"""
        DELETE FROM `{table_ref}`
        WHERE return_id IN UNNEST(@retired_return_ids)
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("retired_return_ids", "STRING", retired)]
        )
        client.query(query, job_config=config).result()
        cleaned_tables.append(table_name)
    return cleaned_tables


def ensure_dataset(client: bigquery.Client, dataset_id: str) -> None:
    dataset = bigquery.Dataset(f"{client.project}.{dataset_id}")
    dataset.location = "US"
    client.create_dataset(dataset, exists_ok=True)


def normalize_dataframe_for_bq(df: pd.DataFrame, table_name: str) -> pd.DataFrame:
    """Coerce a loading copy to the scalar types declared by ``BQ_SCHEMAS``."""
    clean = df.copy().where(pd.notnull(df), None)

    def is_null(value: Any) -> bool:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False

    for column_name, declared_type in BQ_SCHEMAS[table_name]:
        if column_name not in clean.columns:
            continue
        type_name = declared_type.upper()
        values = clean[column_name].tolist()

        def assign_values(converted: List[Any]) -> None:
            # Explicit object dtype prevents pandas from re-inferring a
            # string-plus-null column as float (and turning None back into NaN).
            clean[column_name] = pd.Series(converted, index=clean.index, dtype="object")

        if type_name in {"STRING"}:
            assign_values([None if is_null(v) else str(v) for v in values])
        elif type_name in {"INT64", "INTEGER"}:
            assign_values([None if is_null(v) else int(v) for v in values])
        elif type_name in {"FLOAT64", "FLOAT"}:
            assign_values([None if is_null(v) else float(v) for v in values])
        elif type_name in {"BOOL", "BOOLEAN"}:
            def as_bool(value: Any) -> Optional[bool]:
                if is_null(value):
                    return None
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in {"true", "1", "yes"}:
                        return True
                    if lowered in {"false", "0", "no"}:
                        return False
                return bool(value)
            assign_values([as_bool(v) for v in values])
        elif type_name in {"TIMESTAMP", "DATETIME", "DATE"}:
            parsed = pd.to_datetime(clean[column_name], errors="raise", utc=(type_name == "TIMESTAMP"))
            if type_name == "DATE":
                assign_values([None if is_null(v) else v.date() for v in parsed])
            else:
                assign_values([None if is_null(v) else v.to_pydatetime() for v in parsed])

    return clean


def upsert_table(
    client: bigquery.Client,
    dataset_id: str,
    table_name: str,
    df: pd.DataFrame,
    primary_keys: List[str],
) -> None:
    """
    Non-destructive incremental BigQuery MERGE upsert.
    Preserves all existing rows, updates on matching primary key, inserts new records.
    """
    if df.empty:
        return

    table_ref = f"{client.project}.{dataset_id}.{table_name}"
    schema = [
        bigquery.SchemaField(name, field_type, mode="NULLABLE")
        for name, field_type in BQ_SCHEMAS[table_name]
    ]

    try:
        client.get_table(table_ref)
        table_exists = True
    except Exception:
        table_exists = False

    # Normalize only the loading copy; generated dataframes and local files
    # retain their original pandas/in-memory representations.
    clean = normalize_dataframe_for_bq(df, table_name)

    if not table_exists:
        job_config = bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_EMPTY,
        )
        job = client.load_table_from_dataframe(clean, table_ref, job_config=job_config)
        job.result()
        return

    staging_suffix = f"stg_{int(datetime.now(timezone.utc).timestamp())}_{random.randint(1000, 9999)}"
    staging_table_name = f"{table_name}_{staging_suffix}"
    staging_ref = f"{client.project}.{dataset_id}.{staging_table_name}"

    staging_job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    job = client.load_table_from_dataframe(clean, staging_ref, job_config=staging_job_config)
    job.result()

    cols = [name for name, _ in BQ_SCHEMAS[table_name]]
    join_cond = " AND ".join([f"T.{k} = S.{k}" for k in primary_keys])
    update_cols = [c for c in cols if c not in primary_keys]
    update_set = ", ".join([f"T.{c} = S.{c}" for c in update_cols])
    cols_joined = ", ".join([f"`{c}`" for c in cols])
    s_cols_joined = ", ".join([f"S.`{c}`" for c in cols])

    if update_cols:
        merge_sql = f"""
        MERGE `{table_ref}` T
        USING `{staging_ref}` S
        ON {join_cond}
        WHEN MATCHED THEN
          UPDATE SET {update_set}
        WHEN NOT MATCHED THEN
          INSERT ({cols_joined}) VALUES ({s_cols_joined})
        """
    else:
        merge_sql = f"""
        MERGE `{table_ref}` T
        USING `{staging_ref}` S
        ON {join_cond}
        WHEN NOT MATCHED THEN
          INSERT ({cols_joined}) VALUES ({s_cols_joined})
        """

    merge_job = client.query(merge_sql)
    merge_job.result()

    client.delete_table(staging_ref, not_found_ok=True)


# ----------------------------------------------------------------------
# 5-Level Semantic Validation System
# ----------------------------------------------------------------------

def validate_all(
    tables: Dict[str, pd.DataFrame],
    product_attributes: pd.DataFrame,
    expected_case_count: int,
) -> Dict[str, Any]:
    """
    Comprehensive 5-level semantic validation:
    Level 1: Structural
    Level 2: Relational
    Level 3: Temporal
    Level 4: Semantic
    Level 5: Reproducibility & Provenance
    """
    requests = tables["return_requests"]
    inspections = tables["return_inspections"]
    evidence = tables["return_evidence"]
    network_links = tables["synthetic_network_links"]
    costs = tables["return_operational_costs"]
    logistics = tables["return_logistics"]
    labels = tables["fraud_labels"]
    scenarios = tables["synthetic_scenarios"]

    results: Dict[str, Any] = {
        "level_1_structural": True,
        "level_2_relational": True,
        "level_3_temporal": True,
        "level_4_semantic": True,
        "level_5_provenance": True,
        "case_validation_results": [],
    }

    # LEVEL 1 — STRUCTURAL
    assert len(requests) == expected_case_count, f"Case count mismatch: {len(requests)} vs {expected_case_count}"
    assert requests["return_id"].is_unique, "return_id must be unique across all requests"
    assert requests["order_item_id"].is_unique, "Each return case must use a unique source order_item anchor"

    for t_name, df in {**tables, "product_attributes": product_attributes}.items():
        if df.empty:
            continue
        expected_cols = [c[0] for c in BQ_SCHEMAS[t_name]]
        for col in expected_cols:
            assert col in df.columns, f"Table {t_name} missing required column: {col}"
        pks = PRIMARY_KEYS.get(t_name, [])
        if pks and all(k in df.columns for k in pks):
            assert df.duplicated(subset=pks).sum() == 0, f"Table {t_name} contains duplicate primary keys on {pks}"

    # LEVEL 2 — RELATIONAL
    valid_rids = set(requests["return_id"])
    for t_name in ["return_evidence", "return_inspections", "return_logistics",
                   "return_operational_costs", "fraud_labels", "synthetic_scenarios",
                   "synthetic_network_links"]:
        df = tables[t_name]
        if df.empty:
            continue
        assert set(df["return_id"]).issubset(valid_rids), f"Table {t_name} contains orphan return_id"

    # LEVEL 3 — TEMPORAL
    assessment = pd.to_datetime(requests["assessment_at"], utc=True)
    requested = pd.to_datetime(requests["requested_at"], utc=True)
    assert (requested <= assessment).all(), "Temporal violation: requested_at after assessment_at"

    if not logistics.empty:
        merged_log = logistics.merge(requests[["return_id", "assessment_at", "requested_at"]], on="return_id", how="left")
        pickup = pd.to_datetime(merged_log["pickup_at"], utc=True)
        received = pd.to_datetime(merged_log["received_at"], utc=True)
        req_dt = pd.to_datetime(merged_log["requested_at"], utc=True)
        assert (pickup >= req_dt).all(), "Temporal violation: pickup_at before requested_at"
        assert (received >= pickup).all(), "Temporal violation: received_at before pickup_at"

    if not inspections.empty:
        merged_insp = inspections.merge(requests[["return_id", "assessment_at", "requested_at"]], on="return_id", how="left")
        inspected = pd.to_datetime(merged_insp["inspected_at"], utc=True)
        req_dt = pd.to_datetime(merged_insp["requested_at"], utc=True)
        ass_dt = pd.to_datetime(merged_insp["assessment_at"], utc=True)
        assert (inspected >= req_dt).all(), "Temporal violation: inspected_at before requested_at"
        assert (inspected <= ass_dt).all(), "Temporal violation: inspected_at after assessment_at"

    # LEVEL 4 — SEMANTIC
    for op_df in [requests, logistics, inspections, evidence, costs, network_links, product_attributes]:
        assert "ground_truth" not in op_df.columns, "Ground truth leakage detected in operational table"
        assert "fraud_type" not in op_df.columns, "Fraud type leakage detected in operational table"

    # S06: Empty box
    s06_insp = inspections[inspections["return_id"].str.contains("RTN-S06-")]
    if not s06_insp.empty:
        assert (s06_insp["item_present"] == False).all(), "S06 inspection violation: item_present must be False"
        assert (s06_insp["condition"] == "not_present").all(), "S06 inspection violation: condition must be not_present"

    # S07/M08: controlled serial mismatch with an explicit expected baseline.
    for serial_scenario in ("S07", "M08"):
        serial_insp = inspections[inspections["scenario_id"] == serial_scenario]
        if not serial_insp.empty:
            assert serial_insp["expected_serial"].notna().all(), f"{serial_scenario} inspection violation: expected_serial is missing"
            assert serial_insp["returned_serial"].notna().all(), f"{serial_scenario} inspection violation: returned_serial is missing"
            assert (serial_insp["expected_serial"] != serial_insp["returned_serial"]).all(), f"{serial_scenario} inspection violation: serials must differ"
            expected_swaps = serial_insp["expected_serial"].astype(str) + "-SWAP"
            assert (serial_insp["returned_serial"] == expected_swaps).all(), f"{serial_scenario} inspection violation: controlled swap convention invalid"

    # S08: Missing accessories
    s08_insp = inspections[inspections["return_id"].str.contains("RTN-S08-")]
    if not s08_insp.empty:
        for _, row in s08_insp.iterrows():
            pres = json.loads(row["accessories_present"])
            assert isinstance(pres, list), "S08 accessories_present must be JSON list"

    # S09: False damage claim
    s09_insp = inspections[inspections["return_id"].str.contains("RTN-S09-")]
    if not s09_insp.empty:
        assert (s09_insp["condition"] == "minor_or_no_damage").all(), "S09 condition must contradict damage claim"

    # S10: Wardrobing
    s10_req = requests[requests["scenario_id"] == "S10"]
    if not s10_req.empty:
        for _, row in s10_req.iterrows():
            ctx = json.loads(row["usage_context"])
            assert ctx.get("usage_days", 999) <= 3, "S10 usage_days must be short duration (<=3)"
            assert ctx.get("repeat_pattern") == "short_duration_repeat_return", "S10 repeat pattern missing"

    # S11: Coordinated abuse / network links
    s11_links = network_links[network_links["scenario_id"] == "S11"]
    if not s11_links.empty:
        assert len(s11_links) >= 5, "S11 must have at least 5 synthetic network links"
        assert (s11_links["user_id"] != s11_links["linked_user_id"]).all(), "Network links must connect distinct users"

    # S12: Product quality
    s12_insp = inspections[inspections["return_id"].str.contains("RTN-S12-")]
    if not s12_insp.empty:
        assert (s12_insp["condition"] == "defective").all(), "S12 condition must be defective"

    # V2 active S12 demonstrations: preserve legacy S12 while enforcing the
    # same physical semantics on the source-history-qualified V2 additions.
    s12v2_insp = inspections[inspections["return_id"].str.contains("RTN-S12V2-")]
    if not s12v2_insp.empty:
        assert len(s12v2_insp) == V2_ADDITIONAL_SCENARIO_COUNTS["S12V2"], "S12V2 inspection count mismatch"
        assert (s12v2_insp["condition"] == "defective").all(), "S12V2 condition must be defective"

    # M05: high reverse-logistics cost is a contract condition, not merely a
    # scenario description.
    m05_costs = costs[costs["scenario_id"] == "M05"]
    if not m05_costs.empty:
        assert (m05_costs["reverse_logistics_cost"] >= 50.0).all(), "M05 reverse_logistics_cost must be >= 50.0"

    # Multi-signal scenarios (M01-M08)
    for m_id in MULTI_SCENARIO_COUNTS:
        m_req = requests[requests["scenario_id"] == m_id]
        if m_req.empty:
            continue
        assert len(m_req) == MULTI_SCENARIO_COUNTS[m_id], f"Scenario {m_id} expected {MULTI_SCENARIO_COUNTS[m_id]} cases"

        if m_id in {"M01", "M04", "M06", "M07", "M08"}:
            m_links = network_links[network_links["scenario_id"] == m_id]
            assert len(m_links) == len(m_req), f"{m_id} missing required synthetic_network_links"

        if m_id in {"M02", "M03", "M04", "M06", "M07", "M08"}:
            m_insp = inspections[inspections["return_id"].str.contains(f"RTN-{m_id}-")]
            assert len(m_insp) == len(m_req), f"{m_id} missing required inspections"

    # LEVEL 5 — PROVENANCE & REPRODUCIBILITY
    for t_name, df in {**tables, "product_attributes": product_attributes}.items():
        if df.empty:
            continue
        assert "generator_version" in df.columns, f"{t_name} missing generator_version"
        if t_name == "fraud_labels":
            assert (df["source"] == "controlled_demo_evaluation_only").all(), "fraud_labels source mismatch"
        else:
            assert (df["source_type"] == "synthetic_demo").all(), f"{t_name} contains non-synthetic source_type"

    for _, r in requests.iterrows():
        rid = r["return_id"]
        sid = r["scenario_id"]
        contract = SCENARIO_CONTRACTS.get(sid)
        results["case_validation_results"].append({
            "return_id": rid,
            "scenario_id": sid,
            "user_id": r["user_id"],
            "order_item_id": r["order_item_id"],
            "product_id": r["product_id"],
            "scenario_type": contract.scenario_type if contract else "UNKNOWN",
            "primary_capability": contract.primary_capability if contract else "UNKNOWN",
            "validation_status": "PASS",
        })

    return results


# ----------------------------------------------------------------------
# Output Writing
# ----------------------------------------------------------------------

def write_outputs(
    output_dir: Path,
    tables: Dict[str, pd.DataFrame],
    product_attributes: pd.DataFrame,
    manifest: Dict[str, Any],
    anchor_audits: List[AnchorAudit],
    validation_results: Dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    for name, df in tables.items():
        df.to_csv(output_dir / f"{name}.csv", index=False)

    product_attributes.to_csv(output_dir / "product_attributes.csv", index=False)

    with open(output_dir / "generation_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    with open(output_dir / "anchor_audit_report.json", "w", encoding="utf-8") as f:
        json.dump([asdict(a) for a in anchor_audits], f, indent=2)

    with open(output_dir / "case_validation_report.json", "w", encoding="utf-8") as f:
        json.dump(validation_results, f, indent=2)


# ----------------------------------------------------------------------
# Main Execution Pipeline
# ----------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)
    assessment_at = parse_assessment(args.assessment_at)
    output_dir = Path(args.output_dir)

    client = bigquery.Client(project=args.project_id)

    # ------------------------------------------------------------------
    # Validate-only Mode
    # ------------------------------------------------------------------
    if args.validate_only:
        print(f"Running 5-level semantic validation on existing files in: {output_dir} ...")
        tables = {}
        for name in [
            "return_requests", "return_evidence", "return_inspections",
            "return_logistics", "return_operational_costs",
            "synthetic_scenarios", "fraud_labels", "synthetic_network_links"
        ]:
            fpath = output_dir / f"{name}.csv"
            if fpath.exists():
                tables[name] = pd.read_csv(fpath)
            else:
                tables[name] = pd.DataFrame()
        prod_df = pd.read_csv(output_dir / "product_attributes.csv")
        results = validate_all(tables, prod_df, len(tables["return_requests"]))
        print("VALIDATION PASSED SUCCESSFULLY (5 Levels verified)")
        return

    # ------------------------------------------------------------------
    # Query Real The Look Anchors & Context
    # ------------------------------------------------------------------
    print("Querying real The Look anchors (excluding cancelled, created <= assessment_at)...")
    anchors_df = query_anchors(client, args.limit_anchors, assessment_at)
    return_counts_df = query_customer_return_counts(client, assessment_at)
    product_metrics_df = query_product_return_metrics(client, assessment_at)
    network_df = query_identifiable_network(client, assessment_at)

    if anchors_df.empty:
        raise RuntimeError("No real The Look order-item anchors were returned.")

    anchors, return_counts = make_anchor_objects(anchors_df, return_counts_df)

    product_metrics: Dict[int, Dict[str, Any]] = {
        int(row.product_id): {
            "observed_items": int(row.observed_items),
            "returned_items": int(row.returned_items),
            "return_rate": float(row.return_rate),
        }
        for row in product_metrics_df.itertuples()
    }

    network_pool: List[Tuple[int, str]] = []
    for row in network_df.itertuples():
        network_pool.append((safe_int(row.user_id), ip_hash(getattr(row, "ip_address", None))))
    network_pool = [x for x in network_pool if x[1]]

    # ------------------------------------------------------------------
    # Handle Existing S01-S14 Foundation vs Full Regeneration
    # ------------------------------------------------------------------
    existing_cases_present = False
    req_file = output_dir / "return_requests.csv"
    existing_tables: Dict[str, pd.DataFrame] = {}

    if args.preserve_existing and req_file.exists():
        print(f"Loading existing foundation cases from {output_dir} to preserve S01-S14 checkpoint...")
        for name in [
            "return_requests", "return_evidence", "return_inspections",
            "return_logistics", "return_operational_costs",
            "synthetic_scenarios", "fraud_labels", "synthetic_network_links"
        ]:
            fpath = output_dir / f"{name}.csv"
            existing_tables[name] = pd.read_csv(fpath) if fpath.exists() else pd.DataFrame()

        # Backward-compatible fallback for V1 directories created before the
        # network-links table existed.  Otherwise retain actual rows and their
        # stable network_link_id values verbatim.
        if existing_tables["synthetic_network_links"].empty:
            existing_net_links = extract_existing_network_links(existing_tables["return_requests"])
            existing_tables["synthetic_network_links"] = pd.DataFrame(existing_net_links)
        existing_cases_present = True

        # A deterministic V2-only migration repairs the M05 fixture contract
        # while leaving every V1 field and row untouched.
        apply_v2_contract_migrations(existing_tables)
        existing_tables = exclude_legacy_ineligible_rows(existing_tables)

    used_order_items: Set[int] = set()
    if existing_cases_present:
        used_order_items.update(existing_tables["return_requests"]["order_item_id"].dropna().astype(int))

    new_cases: List[Dict[str, Any]] = []
    scenario_map: Dict[str, List[Dict[str, Any]]] = {k: [] for k in SCENARIO_COUNTS}
    anchors_by_product: Dict[int, Anchor] = {a.product_id: a for a in anchors}
    anchor_audits: List[AnchorAudit] = []

    scenarios_to_generate = (
        {**MULTI_SCENARIO_COUNTS, **V2_ADDITIONAL_SCENARIO_COUNTS}
        if existing_cases_present
        else SCENARIO_COUNTS
    )
    existing_return_ids = set(existing_tables.get("return_requests", pd.DataFrame()).get("return_id", pd.Series(dtype=str)))

    print(f"Generating scenarios: {list(scenarios_to_generate.keys())}...")
    for scenario_id, count in scenarios_to_generate.items():
        for i in range(1, count + 1):
            return_id = scenario_return_id(scenario_id, i)
            # Existing V1 and V2 return IDs are authoritative local records.
            # Generate only a deliberately missing case; local upsert below
            # guarantees that a later authoritative update cannot duplicate it.
            if return_id in LEGACY_INELIGIBLE_RETURN_IDS or return_id in existing_return_ids:
                continue
            anchor, audit = select_anchor_with_audit(
                scenario_id,
                i,
                anchors,
                return_counts,
                product_metrics,
                rng,
                used_order_items,
                network_pool,
            )
            used_order_items.add(anchor.order_item_id)
            anchors_by_product.setdefault(anchor.product_id, anchor)
            anchor_audits.append(audit)

            case = make_case(
                scenario_id,
                i,
                anchor,
                assessment_at,
                rng,
                return_counts,
                network_pool,
                version=GENERATOR_VERSION_V2,
            )
            case["return_request"]["seed"] = args.seed
            new_cases.append(case)
            scenario_map[scenario_id].append(case["return_request"])

    new_flattened = flatten_cases(new_cases, version=GENERATOR_VERSION_V2)

    # Combine existing + new
    if existing_cases_present:
        final_tables: Dict[str, pd.DataFrame] = {}
        for k in new_flattened:
            final_tables[k] = upsert_local_rows(
                existing_tables.get(k, pd.DataFrame()), new_flattened[k], k
            )
        existing_prod_df = pd.read_csv(output_dir / "product_attributes.csv")
        new_prod_rows = build_product_attributes(anchors_by_product, scenario_map, rng, version=GENERATOR_VERSION_V2)
        new_prod_df = pd.DataFrame(new_prod_rows)
        final_prod_df = upsert_local_rows(existing_prod_df, new_prod_df, "product_attributes")
    else:
        final_tables = new_flattened
        prod_rows = build_product_attributes(anchors_by_product, scenario_map, rng, version=GENERATOR_VERSION_V2)
        final_prod_df = pd.DataFrame(prod_rows)

    total_cases = len(final_tables["return_requests"])
    print(f"\nRunning 5-level semantic validation on {total_cases} cases...")
    validation_results = validate_all(final_tables, final_prod_df, total_cases)
    print("5-LEVEL SEMANTIC VALIDATION PASSED.")

    # Manifest metadata
    manifest = {
        "dataset_status": "ACTIVE",
        "legacy_provenance_path": "data/legacy_returnguard_v1_ineligible_s12/legacy_v1_s12_records.json",
        "legacy_return_ids": sorted(LEGACY_INELIGIBLE_RETURN_IDS),
        "generator_version": GENERATOR_VERSION_V2,
        "previous_version": GENERATOR_VERSION_V1,
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
        "scenario_counts": final_tables["return_requests"]["scenario_id"].value_counts().to_dict(),
        "total_cases": total_cases,
        "generated_tables": list(BQ_SCHEMAS.keys()),
        "rules": [
            "Real The Look customers/products/order_items remain the foundation.",
            "Source tables are never mutated.",
            "Historical source return reasons are never fabricated.",
            "Cancelled orders are excluded from historical baseline metrics.",
            "Known ineligible V1 S12 records are retained only in the legacy provenance archive, not active output.",
            "Synthetic records are clearly marked synthetic_demo.",
            "Fraud ground truth is evaluation-only and not a risk input.",
            "Anonymous events are not used for customer attribution.",
            "Operational timestamps are ReturnGuard-created.",
            "Gemini findings are not generated here; Gemini runs later on supplied images.",
            "Incremental BigQuery loading uses safe MERGE upsert without truncate.",
        ],
    }

    # Write files
    write_outputs(output_dir, final_tables, final_prod_df, manifest, anchor_audits, validation_results)
    print(f"Generated local files written to: {output_dir.resolve()}")

    # ------------------------------------------------------------------
    # Incremental BigQuery Load (Safe MERGE Upsert)
    # ------------------------------------------------------------------
    if args.load_bigquery:
        print(f"\nIncrementally loading ReturnGuard dataset {args.project_id}.{args.dataset} via MERGE ...")
        ensure_dataset(client, args.dataset)
        cleaned = cleanup_retired_active_returns(client, args.dataset)
        print(f"Retired active return cleanup applied to: {cleaned or 'no existing return-grained tables'}")

        upsert_table(client, args.dataset, "product_attributes", final_prod_df, PRIMARY_KEYS["product_attributes"])
        for name, df in final_tables.items():
            upsert_table(client, args.dataset, name, df, PRIMARY_KEYS[name])

        print("INCREMENTAL BIGQUERY MERGE LOAD COMPLETE.")

    print("\nSummary:")
    print(f"  Total Cases: {total_cases}")
    print(f"  Base Scenarios (S01-S14): {len(final_tables['return_requests'][final_tables['return_requests']['scenario_id'].str.startswith('S')])}")
    print(f"  Multi-Signal Scenarios (M01-M08): {len(final_tables['return_requests'][final_tables['return_requests']['scenario_id'].str.startswith('M')])}")
    print(f"  Network Links: {len(final_tables['synthetic_network_links'])}")
    print(f"  Product Attributes: {len(final_prod_df)}")


if __name__ == "__main__":
    main()
