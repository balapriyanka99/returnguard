"""Environment-backed configuration for the intelligence data layer."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


_BQ_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class IntelligenceConfig:
    project_id: str = "return-guard-506407"
    dataset_id: str = "returnguard"
    source_project_id: str = "return-guard-506407"
    source_dataset_id: str = "returnguard"
    source_order_items_table: str = "source_order_items_snapshot"
    source_products_table: str = "source_products_snapshot"

    @classmethod
    def from_env(cls) -> "IntelligenceConfig":
        config = cls(
            project_id=os.getenv("RETURNGUARD_GCP_PROJECT", cls.project_id),
            dataset_id=os.getenv("RETURNGUARD_BQ_DATASET", cls.dataset_id),
            source_project_id=os.getenv("RETURNGUARD_SOURCE_PROJECT", cls.source_project_id),
            source_dataset_id=os.getenv("RETURNGUARD_SOURCE_DATASET", cls.source_dataset_id),
            source_order_items_table=os.getenv(
                "RETURNGUARD_SOURCE_ORDER_ITEMS_TABLE", cls.source_order_items_table
            ),
            source_products_table=os.getenv(
                "RETURNGUARD_SOURCE_PRODUCTS_TABLE", cls.source_products_table
            ),
        )
        for name, value in (
            ("dataset_id", config.dataset_id),
            ("source_dataset_id", config.source_dataset_id),
            ("source_order_items_table", config.source_order_items_table),
            ("source_products_table", config.source_products_table),
        ):
            if not _BQ_IDENTIFIER.fullmatch(value):
                raise ValueError(f"Invalid BigQuery {name}: {value!r}")
        return config

    @property
    def returnguard_dataset(self) -> str:
        return f"{self.project_id}.{self.dataset_id}"

    @property
    def source_dataset(self) -> str:
        return f"{self.source_project_id}.{self.source_dataset_id}"

    @property
    def source_order_items(self) -> str:
        return f"{self.source_dataset}.{self.source_order_items_table}"

    @property
    def source_products(self) -> str:
        return f"{self.source_dataset}.{self.source_products_table}"
