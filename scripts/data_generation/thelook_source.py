"""Shared fixed-version references for public The Look BigQuery tables."""

from __future__ import annotations

from datetime import datetime, timezone

from google.cloud import bigquery


SOURCE_DATASET = "bigquery-public-data.thelook_ecommerce"
SOURCE_AS_OF_PARAMETER = "source_as_of"


def parse_source_as_of(value: str) -> datetime:
    """Parse an ISO timestamp and normalize it to UTC for BigQuery time travel."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def source_table(
    table_name: str,
    source_as_of: datetime | None = None,
    *,
    alias: str | None = None,
) -> str:
    """Return one safe source-table expression with optional time travel."""

    if not table_name.replace("_", "").isalnum():
        raise ValueError(f"Invalid The Look table name: {table_name!r}")
    table = f"`{SOURCE_DATASET}.{table_name}`"
    if source_as_of is not None and alias:
        # BigQuery does not accept ``... FOR SYSTEM_TIME AS OF @ts AS alias``.
        # Keep the versioned reference intact inside a derived table, then alias
        # that derived table for callers which need qualified column names.
        expression = (
            f"(SELECT * FROM {table} FOR SYSTEM_TIME AS OF "
            f"@{SOURCE_AS_OF_PARAMETER}) AS {alias}"
        )
    else:
        expression = table
        if source_as_of is not None:
            expression += f" FOR SYSTEM_TIME AS OF @{SOURCE_AS_OF_PARAMETER}"
        if alias:
            expression += f" AS {alias}"
    return expression


def source_parameters(source_as_of: datetime | None) -> list[bigquery.ScalarQueryParameter]:
    """Return the common parameter shared by every historical source query."""

    if source_as_of is None:
        return []
    return [
        bigquery.ScalarQueryParameter(
            SOURCE_AS_OF_PARAMETER, "TIMESTAMP", source_as_of
        )
    ]


def query_job_config(
    source_as_of: datetime | None,
    parameters: list[bigquery.ArrayQueryParameter | bigquery.ScalarQueryParameter] | None = None,
) -> bigquery.QueryJobConfig:
    """Build a query config containing one consistent source-version parameter."""

    return bigquery.QueryJobConfig(
        query_parameters=[*source_parameters(source_as_of), *(parameters or [])]
    )
