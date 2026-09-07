"""Customer-level return behavior projection."""

from .models import CustomerIntelligence, ReturnBehaviorIntelligence


def build_return_behavior(customer: CustomerIntelligence) -> ReturnBehaviorIntelligence:
    return ReturnBehaviorIntelligence(
        historical_return_count=customer.lifetime_historical_returns,
        lifetime_return_rate=customer.lifetime_return_rate,
        returns_30d=customer.returns_30d, returns_90d=customer.returns_90d,
        days_since_last_return=customer.days_since_last_return,
        recent_return_clustering={"returns_30d": customer.returns_30d,
            "returns_90d": customer.returns_90d,
            "lifetime_historical_returns": customer.lifetime_historical_returns,
            "recent_cluster_present": None},
        returned_value=customer.returned_value,
        average_returned_value=customer.average_returned_item_value,
        returned_value_to_purchase_value_ratio=customer.returned_value_to_total_spend_ratio,
        return_frequency_relative_to_purchase_volume=customer.lifetime_return_rate,
        return_frequency_relative_to_tenure=customer.return_frequency_relative_to_tenure,
        repeated_returned_products={"current_product_count": customer.repeat_same_product_return_count,
            "current_product_repeated": customer.repeat_same_product_return,
            "distinct_returned_products": customer.distinct_returned_products},
        repeated_returned_categories={"current_category_count": customer.repeat_same_category_return_count,
            "current_category_repeated": customer.repeat_same_category_return,
            "distinct_returned_categories": customer.distinct_returned_categories},
        purchase_to_return_timing_days=customer.purchase_to_return_timing_days,
        history_confidence=customer.history_confidence,
        data_origin=customer.customer_history_data_origin,
    )
