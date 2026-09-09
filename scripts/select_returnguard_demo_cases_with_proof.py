#!/usr/bin/env python3
"""
ReturnGuard representative case selector with selection proof.

READ ONLY.

This script:
1. Reads the generated ReturnGuard CSVs.
2. Scores candidate returns for scenario diversity and capability coverage.
3. Selects representative demo/smoke-test cases.
4. Emits evidence showing WHY each case was selected:
   - scenario metadata
   - return request fields
   - evidence rows/count
   - inspection rows/count
   - network rows/count
   - economics rows/count
   - selection score and reasons
5. Optionally compares its selected IDs with IDs hard-coded or referenced
   inside scripts/smoke_test_agents_live.py.

Usage:
  python scripts/select_returnguard_demo_cases_with_proof.py
  python scripts/select_returnguard_demo_cases_with_proof.py --count 8

Output:
  data/generated_returnguard/selected_demo_cases_with_proof.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DATA_DIR = Path("data/generated_returnguard")
SMOKE_SCRIPT = Path("scripts/smoke_test_agents_live.py")
DEFAULT_OUTPUT = DATA_DIR / "selected_demo_cases_with_proof.json"


def read_csv(name: str) -> list[dict[str, str]]:
    path = DATA_DIR / name
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def first_present(row: dict[str, str], candidates: list[str]) -> str | None:
    for key in candidates:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def get_return_id(row: dict[str, str]) -> str | None:
    return first_present(row, ["return_id", "return_request_id", "id"])


def get_scenario_id(row: dict[str, str]) -> str | None:
    return first_present(row, ["scenario_id", "scenario", "scenario_code"])


def group_by_return(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        rid = get_return_id(row)
        if rid:
            out[rid].append(row)
    return out


def compact_rows(rows: list[dict[str, str]], limit: int = 3) -> list[dict[str, str]]:
    # Keep only populated fields so the proof report is readable.
    out = []
    for row in rows[:limit]:
        out.append({k: v for k, v in row.items() if v not in ("", None)})
    return out


def extract_smoke_ids(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "script_found": False,
            "explicit_ids": [],
            "notes": "smoke_test_agents_live.py not found",
        }

    text = path.read_text(encoding="utf-8", errors="replace")

    # Extract common ReturnGuard ID shapes without assuming one exact format.
    patterns = [
        r"\bRTN-[A-Za-z0-9_-]+\b",
        r"\bRET-[A-Za-z0-9_-]+\b",
        r"\bRETURN-[A-Za-z0-9_-]+\b",
    ]
    found = set()
    for pattern in patterns:
        found.update(re.findall(pattern, text))

    uses_cli_return_id = "--return-id" in text or "return_id" in text

    return {
        "script_found": True,
        "explicit_ids": sorted(found),
        "appears_parameterized_by_return_id": uses_cli_return_id,
        "notes": (
            "If explicit_ids is empty and appears_parameterized_by_return_id is true, "
            "the smoke script does not choose cases itself; the caller supplies them."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    returns = read_csv("return_requests.csv")
    scenarios = read_csv("synthetic_scenarios.csv")
    evidence = read_csv("return_evidence.csv")
    inspections = read_csv("return_inspections.csv")
    network = read_csv("synthetic_network_links.csv")
    costs = read_csv("return_operational_costs.csv")

    if not returns:
        raise SystemExit(f"No return_requests.csv found under {DATA_DIR}")

    ev = group_by_return(evidence)
    ins = group_by_return(inspections)
    net = group_by_return(network)
    econ = group_by_return(costs)

    scenario_by_return: dict[str, dict[str, str]] = {}
    for row in scenarios:
        rid = get_return_id(row)
        if rid:
            scenario_by_return[rid] = row

    candidates: list[dict[str, Any]] = []

    for row in returns:
        rid = get_return_id(row)
        if not rid:
            continue

        scenario_row = scenario_by_return.get(rid, {})
        scenario_id = get_scenario_id(scenario_row) or get_scenario_id(row) or "UNKNOWN"

        reason = first_present(
            row, ["return_reason", "reason", "reason_code", "return_reason_code"]
        )
        status = first_present(
            row, ["status", "return_status", "request_status"]
        )

        has_evidence = bool(ev.get(rid))
        has_inspection = bool(ins.get(rid))
        has_network = bool(net.get(rid))
        has_economics = bool(econ.get(rid))

        richness = sum(
            [has_evidence, has_inspection, has_network, has_economics]
        )

        candidates.append(
            {
                "return_id": rid,
                "scenario_id": scenario_id,
                "return_reason": reason,
                "status": status,
                "has_evidence": has_evidence,
                "has_inspection": has_inspection,
                "has_network": has_network,
                "has_economics": has_economics,
                "richness_score": richness,
                "return_request_row": {k: v for k, v in row.items() if v not in ("", None)},
                "scenario_row": {k: v for k, v in scenario_row.items() if v not in ("", None)},
                "evidence_rows": compact_rows(ev.get(rid, [])),
                "inspection_rows": compact_rows(ins.get(rid, [])),
                "network_rows": compact_rows(net.get(rid, [])),
                "economics_rows": compact_rows(econ.get(rid, [])),
                "evidence_count": len(ev.get(rid, [])),
                "inspection_count": len(ins.get(rid, [])),
                "network_count": len(net.get(rid, [])),
                "economics_count": len(econ.get(rid, [])),
            }
        )

    selected: list[dict[str, Any]] = []
    used_scenarios: set[str] = set()
    covered: set[str] = set()
    capabilities = [
        "has_evidence",
        "has_inspection",
        "has_network",
        "has_economics",
    ]

    while candidates and len(selected) < args.count:
        best = None
        best_key = None
        best_reasons = None
        best_score = None

        for c in candidates:
            score = c["richness_score"] * 10
            reasons = [f"richness={c['richness_score']} capability types"]

            if c["scenario_id"] not in used_scenarios:
                score += 100
                reasons.append("adds a new scenario family")

            for cap in capabilities:
                if c[cap] and cap not in covered:
                    score += 35
                    reasons.append(f"adds uncovered capability: {cap.removeprefix('has_')}")

            if c["has_evidence"] and c["has_inspection"]:
                score += 8
                reasons.append("covers evidence + inspection lifecycle together")

            if c["return_reason"]:
                score += 2
                reasons.append("has explicit return reason")

            if c["status"]:
                score += 1
                reasons.append("has explicit return status")

            key = (score, c["richness_score"], c["return_id"])
            if best_key is None or key > best_key:
                best_key = key
                best = c
                best_reasons = reasons
                best_score = score

        best["selection_score"] = best_score
        best["selection_reasons"] = best_reasons
        selected.append(best)

        used_scenarios.add(best["scenario_id"])
        for cap in capabilities:
            if best[cap]:
                covered.add(cap)

        candidates.remove(best)

    smoke = extract_smoke_ids(SMOKE_SCRIPT)
    selected_ids = [c["return_id"] for c in selected]
    smoke_ids = smoke.get("explicit_ids", [])

    if smoke_ids:
        comparison = {
            "selected_ids": selected_ids,
            "smoke_script_explicit_ids": smoke_ids,
            "intersection": sorted(set(selected_ids) & set(smoke_ids)),
            "selected_not_in_smoke": sorted(set(selected_ids) - set(smoke_ids)),
            "smoke_not_in_selected": sorted(set(smoke_ids) - set(selected_ids)),
            "interpretation": (
                "Explicit IDs embedded in the smoke script are compared directly. "
                "Differences are not automatically errors; inspect whether the script "
                "uses examples only or hard-codes its test coverage."
            ),
        }
    else:
        comparison = {
            "selected_ids": selected_ids,
            "smoke_script_explicit_ids": [],
            "intersection": [],
            "selected_not_in_smoke": selected_ids,
            "smoke_not_in_selected": [],
            "interpretation": (
                "No explicit case IDs were found in smoke_test_agents_live.py. "
                "If the script is parameterized by --return-id, then it does not select "
                "cases itself; you should pass these selected IDs into it."
            ),
        }

    report = {
        "requested_count": args.count,
        "selected_count": len(selected),
        "selection_strategy": (
            "Greedy scenario diversity plus capability coverage, with proof rows "
            "from ReturnGuard generated data."
        ),
        "covered_capabilities": sorted(covered),
        "scenario_counts": dict(Counter(c["scenario_id"] for c in selected)),
        "selected_cases": selected,
        "smoke_script_analysis": smoke,
        "selector_vs_smoke": comparison,
        "source_files_found": {
            "return_requests": bool(returns),
            "synthetic_scenarios": bool(scenarios),
            "return_evidence": bool(evidence),
            "return_inspections": bool(inspections),
            "synthetic_network_links": bool(network),
            "return_operational_costs": bool(costs),
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("Selected ReturnGuard demo/smoke-test cases WITH PROOF")
    print("-" * 100)

    for i, c in enumerate(selected, 1):
        print(f"{i:2d}. {c['return_id']}  scenario={c['scenario_id']}")
        print(f"    score={c['selection_score']}")
        print(f"    why : {'; '.join(c['selection_reasons'])}")
        print(
            "    data: "
            f"evidence={c['evidence_count']}, "
            f"inspection={c['inspection_count']}, "
            f"network={c['network_count']}, "
            f"economics={c['economics_count']}"
        )

    print("\nSmoke script:")
    print(f"  found: {smoke['script_found']}")
    print(f"  parameterized by return id: {smoke.get('appears_parameterized_by_return_id')}")
    print(f"  explicit IDs found: {smoke_ids or 'none'}")

    print(f"\nDetailed proof report written to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
