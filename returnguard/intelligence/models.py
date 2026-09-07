"""Typed, JSON-friendly contracts for deterministic intelligence results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Optional


@dataclass
class StructuredModel:
    def to_dict(self) -> dict[str, Any]:
        def json_value(value: Any) -> Any:
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: json_value(item) for key, item in value.items()}
            if isinstance(value, list):
                return [json_value(item) for item in value]
            return value

        return json_value(asdict(self))


@dataclass
class ReturnMetadata(StructuredModel):
    return_id: str
    scenario_id: str
    generator_version: str
    user_id: int
    order_item_id: int
    order_id: int
    product_id: int
    assessment_at: datetime
    requested_at: Optional[datetime]
    reason: Optional[str]
    responsibility: Optional[str]
    status: Optional[str]
    source_type: Optional[str]


@dataclass
class RecentPurchaseActivity(StructuredModel):
    orders_30d: int = 0
    items_30d: int = 0
    orders_90d: int = 0
    items_90d: int = 0


@dataclass
class RelationshipValueProxy(StructuredModel):
    tenure_days: Optional[int]
    total_orders: int
    total_items: int
    total_spend: float
    recent_activity: RecentPurchaseActivity
    returned_value: float
    history_confidence: str
    evidence_level: Optional[str] = None
    numeric_score: Optional[float] = None


@dataclass
class CustomerIntelligence(StructuredModel):
    user_id: int
    assessment_at: datetime
    transaction_history_available: bool
    history_duration_days: Optional[int]
    history_confidence: str
    first_observed_purchase_at: Optional[datetime]
    customer_tenure_days: Optional[int]
    total_orders: int
    total_items: int
    total_spend: float
    average_order_value: Optional[float]
    average_item_value: Optional[float]
    purchase_frequency_per_month: Optional[float]
    recent_purchase_activity: RecentPurchaseActivity
    days_since_last_purchase: Optional[int]
    distinct_products_purchased: int
    distinct_categories_purchased: int
    top_purchase_category: Optional[str]
    top_purchase_category_item_count: Optional[int]
    top_purchase_category_share: Optional[float]
    lifetime_historical_returns: Optional[int]
    lifetime_return_rate: Optional[float]
    returns_30d: Optional[int]
    returns_90d: Optional[int]
    days_since_last_return: Optional[int]
    return_frequency_relative_to_tenure: Optional[float]
    returned_goods_value: Optional[float]
    average_returned_item_value: Optional[float]
    returned_value_to_total_spend_ratio: Optional[float]
    distinct_returned_products: Optional[int]
    distinct_returned_categories: Optional[int]
    repeat_same_product_return_count: Optional[int]
    repeat_same_product_return: Optional[bool]
    repeat_same_category_return_count: Optional[int]
    repeat_same_category_return: Optional[bool]
    top_returned_category: Optional[str]
    top_returned_category_count: Optional[int]
    top_returned_category_share: Optional[float]
    purchased_value: float
    returned_value: Optional[float]
    high_value_return_frequency: Optional[float]
    purchase_to_return_timing_days: Optional[float]
    customer_history_data_origin: str
    relationship_value_proxy: RelationshipValueProxy


@dataclass
class ProductIntelligence(StructuredModel):
    product_id: int
    assessment_at: datetime
    product_items_observed: int
    product_returns_observed: int
    product_return_rate: Optional[float]
    product_returned_value: float
    product_average_sale_price: Optional[float]
    product_last_return_at: Optional[datetime]
    product_history_confidence: str
    category: Optional[str]
    category_items_observed: int
    category_returns_observed: int
    category_return_rate: Optional[float]
    category_returned_value: float
    category_average_sale_price: Optional[float]
    category_history_confidence: str
    product_vs_category_return_rate_delta: Optional[float]
    product_return_rate_elevated: Optional[bool]
    data_origin: str


@dataclass
class ReturnBehaviorIntelligence(StructuredModel):
    historical_return_count: Optional[int]
    lifetime_return_rate: Optional[float]
    returns_30d: Optional[int]
    returns_90d: Optional[int]
    days_since_last_return: Optional[int]
    recent_return_clustering: dict[str, Any]
    returned_value: Optional[float]
    average_returned_value: Optional[float]
    returned_value_to_purchase_value_ratio: Optional[float]
    return_frequency_relative_to_purchase_volume: Optional[float]
    return_frequency_relative_to_tenure: Optional[float]
    repeated_returned_products: dict[str, Any]
    repeated_returned_categories: dict[str, Any]
    purchase_to_return_timing_days: Optional[float]
    history_confidence: str
    data_origin: str


@dataclass
class NetworkIntelligence(StructuredModel):
    user_id: int
    assessment_at: datetime
    linked_user_count: int
    linked_return_count: int
    network_identifiers: list[str]
    relationship_types: list[str]
    linked_user_ids: list[int]
    first_observed_at: Optional[datetime]
    last_observed_at: Optional[datetime]
    recent_network_activity: dict[str, int]
    relationship_strength: Optional[float]
    coordination_indicators: Optional[dict[str, Any]]
    network_history_confidence: str
    data_origin: str
    contextual_evidence_only: bool = True


@dataclass
class ReturnEconomics(StructuredModel):
    current_item_value: Optional[float]
    product_cost: Optional[float]
    reverse_logistics_cost: Optional[float]
    inspection_cost: Optional[float]
    recovery_value: Optional[float]
    total_operational_cost: Optional[float]
    estimated_net_return_cost: Optional[float]
    estimated_loss_exposure: Optional[float]
    economics_data_confidence: str
    data_origin: str


@dataclass
class EvidenceRecord(StructuredModel):
    evidence_id: str
    return_id: str
    evidence_type: Optional[str]
    stage: Optional[str]
    image_uri: Optional[str]
    reference_image_uri: Optional[str]
    submitted_at: Optional[datetime]
    source: Optional[str]
    uploaded_by: Optional[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InspectionRecord(StructuredModel):
    return_id: str
    product_id: Optional[int]
    actual_weight_kg: Optional[float]
    expected_serial: Optional[str]
    returned_serial: Optional[str]
    serial_mismatch: Optional[bool]
    item_present: Optional[bool]
    condition: Optional[str]
    accessories_present: list[str]
    expected_accessories: list[str]
    inspection_location: Optional[str]
    inspected_at: Optional[datetime]
    expected_weight_kg: Optional[float]
    source_type: Optional[str]
    product_attributes_source_type: Optional[str]
    product_attributes_generator_version: Optional[str]
    scenario_id: Optional[str]
    generator_version: Optional[str]


@dataclass
class ReturnIntelligence(StructuredModel):
    return_metadata: ReturnMetadata
    customer: CustomerIntelligence
    product: ProductIntelligence
    return_behavior: ReturnBehaviorIntelligence
    network: NetworkIntelligence
    economics: ReturnEconomics
    evidence: list[EvidenceRecord]
    inspection: Optional[InspectionRecord]
