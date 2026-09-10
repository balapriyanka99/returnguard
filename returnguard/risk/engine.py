"""Pure deterministic implementation of the frozen ReturnGuard Risk-v1 rules."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from returnguard.intelligence.models import (
    CustomerIntelligence,
    EvidenceRecord,
    InspectionRecord,
    NetworkIntelligence,
    ProductIntelligence,
    ReturnBehaviorIntelligence,
)

from .models import CoverageLevel, RiskAssessment, RiskBand, RiskPattern, RiskReason


CUSTOMER_CAP = 25
INSPECTION_CAP = 40
NETWORK_CAP = 10
CROSS_SIGNAL_CAP = 10
PRODUCT_MITIGATION_CAP = 10


@dataclass(frozen=True)
class _Signal:
    code: str
    group: str
    points: int
    field: str
    explanation: str

    def reason(self) -> RiskReason:
        return RiskReason(
            code=self.code,
            group=self.group,
            contribution=self.points,
            canonical_field=self.field,
            explanation=self.explanation,
        )


def _event_id(
    return_id: str, assessment_id: str | None, assessment_at: datetime
) -> str:
    material = "|".join((
        return_id,
        assessment_id or "",
        assessment_at.isoformat(),
        "risk-v1",
    ))
    return "RISK-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _band(score: int) -> RiskBand:
    if score <= 24:
        return RiskBand.LOW
    if score <= 49:
        return RiskBand.MEDIUM
    if score <= 74:
        return RiskBand.HIGH
    return RiskBand.CRITICAL


def _confidence_rank(value: str) -> int:
    return {"none": 0, "limited": 1, "moderate": 2, "high": 3}.get(value, 0)


class RiskV1Engine:
    """Score typed intelligence without scenario IDs, labels, or LLM reasoning."""

    engine_version = "risk-v1"

    def assess(
        self,
        *,
        return_id: str,
        assessment_at: datetime,
        assessment_id: str | None,
        customer: CustomerIntelligence,
        behavior: ReturnBehaviorIntelligence,
        product: ProductIntelligence,
        inspection: InspectionRecord | None,
        evidence: list[EvidenceRecord],
        network: NetworkIntelligence,
    ) -> RiskAssessment:
        del evidence  # Metadata is coverage context and contributes zero risk.
        reasons: list[RiskReason] = []
        limitations: list[str] = []

        customer_signals, customer_evaluable = self._customer(
            customer, behavior, limitations
        )
        customer_score = min(sum(item.points for item in customer_signals), CUSTOMER_CAP)
        reasons.extend(item.reason() for item in customer_signals if item.points)

        mitigation, product_evaluable, product_reason = self._product(product)
        if product_reason is not None:
            reasons.append(product_reason.reason())

        inspection_signals, inspection_evaluable, inspection_facts = self._inspection(
            inspection, limitations
        )
        inspection_score = min(
            sum(item.points for item in inspection_signals), INSPECTION_CAP
        )
        reasons.extend(item.reason() for item in inspection_signals if item.points)

        network_signals, network_pre_cap = self._network(
            network, customer_score, inspection_score
        )
        network_score = min(sum(item.points for item in network_signals), NETWORK_CAP)
        reasons.extend(item.reason() for item in network_signals if item.points)

        cross_signals = self._cross(
            behavior,
            inspection_facts,
            customer_score,
            inspection_score,
            network_pre_cap,
        )
        cross_score = min(sum(item.points for item in cross_signals), CROSS_SIGNAL_CAP)
        reasons.extend(item.reason() for item in cross_signals if item.points)

        domains = []
        if customer_evaluable:
            domains.append("CUSTOMER_BEHAVIOR")
        if product_evaluable:
            domains.append("PRODUCT_CONTEXT")
        if inspection_evaluable:
            domains.append("INSPECTION")
        coverage = (
            CoverageLevel.INSUFFICIENT if not domains
            else CoverageLevel.PARTIAL if len(domains) == 1
            else CoverageLevel.SUBSTANTIAL
        )
        direct_inspection = bool(
            inspection_facts["serial_comparison"]
            or inspection_facts["item_presence_observed"]
            or inspection_facts["valid_weight_comparison"]
            or inspection_facts["valid_accessory_comparison"]
        )
        numeric_allowed = len(domains) >= 2 or direct_inspection
        positive = customer_score + inspection_score + network_score + cross_score
        score = max(0, min(100, positive - mitigation)) if numeric_allowed else None
        band = RiskBand.UNDETERMINED if score is None else _band(score)

        patterns = self._patterns(
            score,
            coverage,
            customer_signals,
            behavior,
            inspection_facts,
            inspection_score,
            network,
            network_score,
            mitigation,
        )
        if score is None:
            limitations.append(
                "Numeric score unavailable: fewer than two evaluable core domains "
                "and no direct inspection comparison."
            )

        reasons.sort(key=lambda item: (
            item.contribution <= 0,
            -item.contribution if item.contribution > 0 else 0,
            item.code,
        ))
        return RiskAssessment(
            risk_event_id=_event_id(return_id, assessment_id, assessment_at),
            return_id=return_id,
            assessment_id=assessment_id,
            assessment_at=assessment_at,
            score=score,
            band=band,
            coverage=coverage,
            evaluable_domains=domains,
            group_scores={
                "CUSTOMER_BEHAVIOR": customer_score,
                "INSPECTION": inspection_score,
                "NETWORK_CONTEXT": network_score,
                "CROSS_SIGNAL": cross_score,
                "VISION": 0,
            },
            product_mitigation=-mitigation,
            reasons=reasons,
            patterns=patterns,
            limitations=limitations,
        )

    @staticmethod
    def _customer(customer, behavior, limitations):
        signals: list[_Signal] = []
        evaluable = False
        rate = behavior.lifetime_return_rate
        if rate is not None and customer.total_items >= 5:
            evaluable = True
            raw_points = 0 if rate <= .10 else 3 if rate <= .20 else 6 if rate <= .35 else 9
            sample_cap = 3 if customer.total_items < 10 else 6 if customer.total_items < 20 else raw_points
            points = min(raw_points, sample_cap)
            explanation = "Eligible lifetime return rate evaluated against Risk-v1 bands."
            if points < raw_points:
                explanation += " Evidentiary contribution capped because of limited historical sample size."
            signals.append(_Signal(
                "ELEVATED_LIFETIME_RETURN_RATE", "CUSTOMER_BEHAVIOR", points,
                "return_behavior.lifetime_return_rate",
                explanation,
            ))
        elif rate is not None:
            limitations.append(
                "Lifetime return rate unavailable for scoring: historical sample below 5 items."
            )

        count = behavior.historical_return_count
        if rate is None and count is not None and behavior.data_origin != "insufficient_history":
            evaluable = True
            points = 0 if count <= 1 else 2 if count <= 3 else 4 if count <= 5 else 5
            signals.append(_Signal(
                "COUNT_BASED_HISTORY_FALLBACK", "CUSTOMER_BEHAVIOR", points,
                "return_behavior.historical_return_count",
                "Controlled or otherwise authoritative count used without inventing a rate.",
            ))
            limitations.append("COUNT_BASED_HISTORY_FALLBACK")

        returns_30d = behavior.returns_30d
        items_30d = customer.recent_purchase_activity.items_30d
        if returns_30d is not None and items_30d > 0:
            evaluable = True
            ratio = returns_30d / items_30d
            points = 0 if ratio <= .15 else 2 if ratio <= .30 else 4 if ratio <= .50 else 6
            signals.append(_Signal(
                "RECENT_RETURN_CONCENTRATION", "CUSTOMER_BEHAVIOR", points,
                "return_behavior.returns_30d / customer.recent_purchase_activity.items_30d",
                "Thirty-day returns evaluated relative to eligible purchased items.",
            ))
        elif returns_30d is not None:
            limitations.append(
                "Recent return concentration unavailable: no valid 30-day item denominator."
            )

        value_ratio = behavior.returned_value_to_purchase_value_ratio
        if value_ratio is not None and customer.total_items >= 5:
            evaluable = True
            raw_points = 0 if value_ratio <= .10 else 2 if value_ratio <= .25 else 4 if value_ratio <= .40 else 6
            sample_cap = 2 if customer.total_items < 10 else 4 if customer.total_items < 20 else raw_points
            points = min(raw_points, sample_cap)
            explanation = "Historical returned value evaluated relative to purchased value."
            if points < raw_points:
                explanation += " Evidentiary contribution capped because of limited historical sample size."
            signals.append(_Signal(
                "HIGH_RETURNED_VALUE_RATIO", "CUSTOMER_BEHAVIOR", points,
                "return_behavior.returned_value_to_purchase_value_ratio",
                explanation,
            ))
        elif value_ratio is not None:
            limitations.append(
                "Returned-value ratio unavailable for scoring: historical sample below 5 items."
            )

        product_count = behavior.repeated_returned_products.get("current_product_count")
        if product_count is not None:
            evaluable = True
            count_value = int(product_count)
            points = 0 if count_value == 0 else 2 if count_value == 1 else 4 if count_value == 2 else 6
            signals.append(_Signal(
                "REPEAT_SAME_PRODUCT_RETURNS", "CUSTOMER_BEHAVIOR", points,
                "return_behavior.repeated_returned_products.current_product_count",
                "Prior returns of the current product evaluated.",
            ))
        return signals, evaluable

    @staticmethod
    def _product(product):
        valid = (
            product.product_return_rate is not None
            and product.category_return_rate is not None
            and product.product_vs_category_return_rate_delta is not None
            and product.product_return_rate_elevated is not None
        )
        if not valid or not product.product_return_rate_elevated:
            return 0, valid, None
        delta = product.product_vs_category_return_rate_delta
        mitigation = 0 if delta <= .05 else 3 if delta <= .15 else 6
        weakest = min(
            _confidence_rank(product.product_history_confidence),
            _confidence_rank(product.category_history_confidence),
        )
        if weakest == 0:
            mitigation = 0
        elif weakest == 1:
            mitigation = min(mitigation, 2)
        mitigation = min(mitigation, PRODUCT_MITIGATION_CAP)
        reason = None if mitigation == 0 else _Signal(
            "PRODUCT_QUALITY_CONTEXT", "PRODUCT_CONTEXT", -mitigation,
            "product.product_vs_category_return_rate_delta",
            "Elevated product return behavior provides non-abuse context.",
        )
        return mitigation, valid, reason

    @staticmethod
    def _inspection(inspection, limitations):
        signals: list[_Signal] = []
        facts = {
            "serial_mismatch": False,
            "serial_comparison": False,
            "item_missing": False,
            "item_presence_observed": False,
            "valid_weight_comparison": False,
            "weight_deviation": None,
            "valid_accessory_comparison": False,
            "missing_accessory_count": 0,
            "accessory_points": 0,
            "weight_points": 0,
        }
        if inspection is None:
            return signals, False, facts

        if inspection.serial_mismatch is not None:
            facts["serial_comparison"] = True
            facts["serial_mismatch"] = inspection.serial_mismatch
            signals.append(_Signal(
                "SERIAL_MISMATCH", "INSPECTION",
                25 if inspection.serial_mismatch else 0,
                "inspection.serial_mismatch",
                "Deterministic expected-versus-returned serial comparison.",
            ))
        if inspection.item_present is not None:
            facts["item_presence_observed"] = True
            facts["item_missing"] = not inspection.item_present
            signals.append(_Signal(
                "ITEM_MISSING", "INSPECTION", 25 if not inspection.item_present else 0,
                "inspection.item_present",
                "Warehouse observation of whether the returned item is present.",
            ))

        expected_weight = inspection.expected_weight_kg
        actual_weight = inspection.actual_weight_kg
        if expected_weight is not None and expected_weight > 0 and actual_weight is not None:
            deviation = abs(actual_weight - expected_weight) / expected_weight
            facts["valid_weight_comparison"] = True
            facts["weight_deviation"] = deviation
            points = 0 if deviation < .05 else 3 if deviation < .15 else 7 if deviation < .30 else 12
            if facts["item_missing"]:
                points = 0
            facts["weight_points"] = points
            signals.append(_Signal(
                "WEIGHT_DEVIATION", "INSPECTION", points,
                "derived.abs(actual_weight_kg - expected_weight_kg) / expected_weight_kg",
                "Deterministic relative package-weight deviation.",
            ))

        expected = inspection.expected_accessories
        if expected:
            facts["valid_accessory_comparison"] = True
            present = set(inspection.accessories_present)
            missing = [item for item in dict.fromkeys(expected) if item not in present]
            count = len(missing)
            ratio = count / len(set(expected))
            points = 0 if count == 0 else 3 if count == 1 and ratio < .5 else 6
            if facts["item_missing"]:
                points = 0
            facts["missing_accessory_count"] = count
            facts["accessory_points"] = points
            signals.append(_Signal(
                "ACCESSORY_MISMATCH", "INSPECTION", points,
                "derived.expected_accessories - accessories_present",
                "Deterministic count of missing expected accessories.",
            ))
        else:
            limitations.append("Accessory comparison unavailable: expected list is empty.")
        evaluable = any((
            facts["serial_comparison"], facts["item_presence_observed"],
            facts["valid_weight_comparison"], facts["valid_accessory_comparison"],
        ))
        return signals, evaluable, facts

    @staticmethod
    def _network(network, customer_score, inspection_score):
        if customer_score < 6 and inspection_score < 10:
            return [], 0
        signals: list[_Signal] = []
        count = network.linked_return_count
        points = 0 if count <= 1 else 2 if count <= 3 else 4 if count <= 5 else 5
        signals.append(_Signal(
            "NETWORK_LINKED_RETURN_PATTERN", "NETWORK_CONTEXT", points,
            "network.linked_return_count",
            "Linked-return context activated by independent non-network evidence.",
        ))
        recent = int(network.recent_network_activity.get("links_90d", 0))
        points = 0 if recent <= 1 else 1 if recent <= 3 else 2
        signals.append(_Signal(
            "NETWORK_RECENT_ACTIVITY", "NETWORK_CONTEXT", points,
            "network.recent_network_activity.links_90d",
            "Recent network activity activated by independent evidence.",
        ))
        indicators = network.coordination_indicators or {}
        shared = int(indicators.get("shared_identifier_count") or 0)
        points = 0 if shared <= 1 else 1 if shared == 2 else 3
        signals.append(_Signal(
            "NETWORK_SHARED_IDENTIFIER_PATTERN", "NETWORK_CONTEXT", points,
            "network.coordination_indicators.shared_identifier_count",
            "Shared-identifier count activated by independent evidence.",
        ))
        return signals, sum(item.points for item in signals)

    @staticmethod
    def _cross(behavior, facts, customer_score, inspection_score, network_pre_cap):
        signals: list[_Signal] = []
        product_count = behavior.repeated_returned_products.get("current_product_count")
        if facts["serial_mismatch"] and product_count is not None and int(product_count) >= 1:
            signals.append(_Signal(
                "SERIAL_REPEAT_PRODUCT_INTERACTION", "CROSS_SIGNAL", 4,
                "inspection.serial_mismatch + return_behavior.repeated_returned_products.current_product_count",
                "Serial mismatch corroborated by repeat current-product behavior.",
            ))
        if facts["item_missing"] and customer_score >= 6:
            signals.append(_Signal(
                "MISSING_ITEM_BEHAVIOR_INTERACTION", "CROSS_SIGNAL", 3,
                "inspection.item_present + customer_behavior_score",
                "Missing item corroborated by material customer behavior evidence.",
            ))
        if inspection_score >= 20 and network_pre_cap >= 4:
            signals.append(_Signal(
                "INSPECTION_NETWORK_INTERACTION", "CROSS_SIGNAL", 3,
                "inspection_score + network_pre_cap_contribution",
                "Strong inspection anomaly corroborated by meaningful network context.",
            ))
        return signals

    @staticmethod
    def _patterns(
        score, coverage, customer_signals, behavior, facts, inspection_score,
        network, network_score, mitigation,
    ):
        patterns: list[RiskPattern] = []
        contributions = {item.code: item.points for item in customer_signals}
        positive_behavior = sum(1 for item in customer_signals if item.points > 0)
        if facts["serial_mismatch"]:
            patterns.append(RiskPattern.POSSIBLE_SERIAL_SUBSTITUTION)
            additional = (
                facts["item_missing"] or facts["weight_points"] > 0
                or facts["accessory_points"] > 0
                or any(item.points > 0 for item in customer_signals)
            )
            if additional:
                patterns.append(RiskPattern.POSSIBLE_ITEM_SUBSTITUTION)
        if facts["item_missing"]:
            patterns.append(RiskPattern.POSSIBLE_EMPTY_BOX)
        if not facts["item_missing"] and facts["missing_accessory_count"] > 0:
            patterns.append(RiskPattern.POSSIBLE_ACCESSORY_STRIPPING)
        customer_score = min(sum(item.points for item in customer_signals), CUSTOMER_CAP)
        if customer_score >= 10 and positive_behavior >= 2:
            patterns.append(RiskPattern.POSSIBLE_REPEATED_RETURN_ABUSE)
        if contributions.get("RECENT_RETURN_CONCENTRATION", 0) >= 4:
            patterns.append(RiskPattern.POSSIBLE_HIGH_FREQUENCY_RETURN_ABUSE)
        if (
            contributions.get("HIGH_RETURNED_VALUE_RATIO", 0) >= 4
            and positive_behavior >= 2
        ):
            patterns.append(RiskPattern.POSSIBLE_RETURN_VALUE_ABUSE)
        gate = customer_score >= 6 or inspection_score >= 10
        if network_score >= 5 and gate:
            patterns.append(RiskPattern.POSSIBLE_LINKED_ACCOUNT_ABUSE)
        if network_score >= 3 and network.linked_return_count >= 2:
            patterns.append(RiskPattern.POSSIBLE_SHARED_RETURN_PATTERN)
        if mitigation:
            patterns.append(RiskPattern.POSSIBLE_PRODUCT_QUALITY_ISSUE)
        if score is None:
            patterns.append(RiskPattern.AMBIGUOUS_OR_INSUFFICIENT_EVIDENCE)
        return patterns
