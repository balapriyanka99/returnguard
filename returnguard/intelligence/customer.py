"""Customer Intelligence construction and business metrics."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .models import CustomerIntelligence, RecentPurchaseActivity, RelationshipValueProxy
from .repository import IntelligenceRepository


CONTROLLED_CUSTOMER_HISTORY_SCENARIOS = frozenset({"S02", "M01", "M02", "M05"})
_MONTH_DAYS = 30.4375


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _days(later: datetime, earlier: datetime | date | None) -> int | None:
    if earlier is None:
        return None
    if isinstance(earlier, date) and not isinstance(earlier, datetime):
        earlier = datetime.combine(earlier, datetime.min.time(), tzinfo=timezone.utc)
    return max((_utc(later) - _utc(earlier)).days, 0)


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / float(denominator)


def build_customer_intelligence(
    repository: IntelligenceRepository,
    row: dict[str, Any],
    assessment_at: datetime,
    category: str | None,
) -> CustomerIntelligence:
    raw = repository.get_customer_history(
        int(row["user_id"]), int(row["product_id"]), category,
        assessment_at, int(row["order_item_id"]),
    )
    orders, items = int(raw.get("total_orders") or 0), int(raw.get("total_items") or 0)
    spend, live_returns = float(raw.get("total_spend") or 0), int(raw.get("live_return_count") or 0)
    history = items > 0
    confidence = "none" if not history else "limited"  # no frozen customer sample bands
    tenure = _days(assessment_at, raw.get("first_observed_purchase_at"))
    controlled = row.get("scenario_id") in CONTROLLED_CUSTOMER_HISTORY_SCENARIOS
    controlled_count = _int(row.get("customer_historical_return_count"))
    if controlled and controlled_count is not None:
        count, origin, rate = controlled_count, "controlled_demo", None
    elif history:
        count, origin, rate = live_returns, "live_source", _ratio(live_returns, items)
    else:
        count, origin, rate = None, "insufficient_history", None
    returned_value = _float(raw.get("returned_value")) if history else None
    recent = RecentPurchaseActivity(
        orders_30d=int(raw.get("orders_30d") or 0), items_30d=int(raw.get("items_30d") or 0),
        orders_90d=int(raw.get("orders_90d") or 0), items_90d=int(raw.get("items_90d") or 0),
    )
    purchase_frequency = None if not tenure else orders / (tenure / _MONTH_DAYS)
    return_frequency_tenure = None if not tenure or controlled else live_returns / (tenure / _MONTH_DAYS)
    top_purchase_count = _int(raw.get("top_purchase_category_item_count"))
    top_return_count = _int(raw.get("top_returned_category_count"))
    same_product = _int(raw.get("same_product_return_count")) if history else None
    same_category = _int(raw.get("same_category_return_count")) if history else None
    relationship = RelationshipValueProxy(
        tenure_days=tenure, total_orders=orders, total_items=items, total_spend=spend,
        recent_activity=recent, returned_value=float(returned_value or 0),
        history_confidence=confidence, evidence_level=None, numeric_score=None,
    )
    return CustomerIntelligence(
        user_id=int(row["user_id"]), assessment_at=assessment_at,
        transaction_history_available=history, history_duration_days=tenure,
        history_confidence=confidence, first_observed_purchase_at=raw.get("first_observed_purchase_at"),
        customer_tenure_days=tenure, total_orders=orders, total_items=items,
        total_spend=spend, average_order_value=_float(raw.get("average_order_value")),
        average_item_value=_float(raw.get("average_item_value")),
        purchase_frequency_per_month=purchase_frequency, recent_purchase_activity=recent,
        days_since_last_purchase=_days(assessment_at, raw.get("last_purchase_at")),
        distinct_products_purchased=int(raw.get("distinct_products_purchased") or 0),
        distinct_categories_purchased=int(raw.get("distinct_categories_purchased") or 0),
        top_purchase_category=raw.get("top_purchase_category"),
        top_purchase_category_item_count=top_purchase_count,
        top_purchase_category_share=_ratio(top_purchase_count, items),
        lifetime_historical_returns=count, lifetime_return_rate=rate,
        returns_30d=_int(raw.get("returns_30d")) if history else None,
        returns_90d=_int(raw.get("returns_90d")) if history else None,
        days_since_last_return=_days(assessment_at, raw.get("last_return_at")),
        return_frequency_relative_to_tenure=return_frequency_tenure,
        returned_goods_value=returned_value,
        average_returned_item_value=_ratio(returned_value, live_returns),
        returned_value_to_total_spend_ratio=_ratio(returned_value, spend),
        distinct_returned_products=_int(raw.get("distinct_returned_products")) if history else None,
        distinct_returned_categories=_int(raw.get("distinct_returned_categories")) if history else None,
        repeat_same_product_return_count=same_product,
        repeat_same_product_return=None if same_product is None else same_product > 0,
        repeat_same_category_return_count=same_category,
        repeat_same_category_return=None if same_category is None else same_category > 0,
        top_returned_category=raw.get("top_returned_category"),
        top_returned_category_count=top_return_count,
        top_returned_category_share=_ratio(top_return_count, live_returns),
        purchased_value=spend, returned_value=returned_value,
        high_value_return_frequency=None,
        purchase_to_return_timing_days=_float(raw.get("purchase_to_return_timing_days")),
        customer_history_data_origin=origin, relationship_value_proxy=relationship,
    )
