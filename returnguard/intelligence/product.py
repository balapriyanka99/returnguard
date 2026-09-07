"""Product Intelligence construction and comparison metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import ProductIntelligence
from .repository import IntelligenceRepository


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else float(numerator) / float(denominator)


def _product_confidence(n: int) -> str:
    return "none" if n == 0 else "limited" if n < 5 else "moderate" if n < 10 else "high"


def _category_confidence(n: int) -> str:
    return "none" if n == 0 else "limited" if n < 20 else "moderate" if n < 100 else "high"


def build_product_intelligence(
    repository: IntelligenceRepository,
    row: dict[str, Any],
    assessment_at: datetime,
) -> ProductIntelligence:
    raw = repository.get_product_history(
        int(row["product_id"]), assessment_at, int(row["order_item_id"])
    )
    pi, pr = int(raw.get("product_items_observed") or 0), int(raw.get("product_returns_observed") or 0)
    ci, cr = int(raw.get("category_items_observed") or 0), int(raw.get("category_returns_observed") or 0)
    product_rate, category_rate = _ratio(pr, pi), _ratio(cr, ci)
    delta = None if product_rate is None or category_rate is None else product_rate - category_rate
    return ProductIntelligence(
        product_id=int(row["product_id"]), assessment_at=assessment_at,
        product_items_observed=pi, product_returns_observed=pr,
        product_return_rate=product_rate,
        product_returned_value=float(raw.get("product_returned_value") or 0),
        product_average_sale_price=_float(raw.get("product_average_sale_price")),
        product_last_return_at=raw.get("product_last_return_at"),
        product_history_confidence=_product_confidence(pi), category=raw.get("category"),
        category_items_observed=ci, category_returns_observed=cr,
        category_return_rate=category_rate,
        category_returned_value=float(raw.get("category_returned_value") or 0),
        category_average_sale_price=_float(raw.get("category_average_sale_price")),
        category_history_confidence=_category_confidence(ci),
        product_vs_category_return_rate_delta=delta,
        product_return_rate_elevated=None if delta is None else product_rate > category_rate,
        data_origin="live_source" if pi or ci else "insufficient_history",
    )
