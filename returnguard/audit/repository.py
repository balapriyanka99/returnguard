"""Retry-safe persistence for merchant-safe return lifecycle events."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Protocol

from google.cloud import bigquery
from pydantic import BaseModel

from returnguard.intelligence.config import IntelligenceConfig
from returnguard.observability import sanitize_structured_value


AUDIT_VERSION = "return-audit-v1"


def audit_event_id(
    return_id: str,
    assessment_id: str | None,
    event_type: str,
    event_at: datetime,
    version: str = AUDIT_VERSION,
) -> str:
    material = "|".join((return_id, assessment_id or "", event_type, event_at.isoformat(), version))
    return "AUDIT-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditEvent(BaseModel):
    audit_event_id: str
    return_id: str
    assessment_id: str | None = None
    event_type: str
    event_at: datetime
    actor_type: str
    actor_name: str | None = None
    status_from: str | None = None
    status_to: str | None = None
    summary: str | None = None
    details_json: dict[str, Any] | None = None
    trace_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        return_id: str,
        assessment_id: str | None,
        event_type: str,
        event_at: datetime,
        actor_type: str,
        actor_name: str | None = None,
        status_from: str | None = None,
        status_to: str | None = None,
        summary: str | None = None,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> "AuditEvent":
        safe_details = sanitize_structured_value(details or {})
        return cls(
            audit_event_id=audit_event_id(return_id, assessment_id, event_type, event_at),
            return_id=return_id,
            assessment_id=assessment_id,
            event_type=event_type,
            event_at=event_at,
            actor_type=actor_type,
            actor_name=actor_name,
            status_from=status_from,
            status_to=status_to,
            summary=summary,
            details_json=safe_details,
            trace_id=trace_id,
        )


class AuditRepository(Protocol):
    def append(self, event: AuditEvent) -> None: ...
    def list_for_return(self, return_id: str) -> list[dict[str, Any]]: ...


class BigQueryAuditRepository:
    def __init__(self, config: IntelligenceConfig | None = None, client: bigquery.Client | None = None) -> None:
        self.config = config or IntelligenceConfig.from_env()
        self.client = client or bigquery.Client(project=self.config.project_id)

    @property
    def table(self) -> str:
        return f"{self.config.returnguard_dataset}.return_audit_log"

    def append(self, event: AuditEvent) -> None:
        sql = f"""
        MERGE `{self.table}` target
        USING (SELECT @audit_event_id AS audit_event_id) source
        ON target.audit_event_id = source.audit_event_id
        WHEN NOT MATCHED THEN INSERT (
          audit_event_id, return_id, assessment_id, event_type, event_at,
          actor_type, actor_name, status_from, status_to, summary, details_json,
          trace_id, created_at
        ) VALUES (
          @audit_event_id, @return_id, @assessment_id, @event_type, @event_at,
          @actor_type, @actor_name, @status_from, @status_to, @summary,
          IF(@details_json IS NULL, NULL, PARSE_JSON(@details_json)), @trace_id,
          CURRENT_TIMESTAMP()
        )
        """
        params = [
            bigquery.ScalarQueryParameter("audit_event_id", "STRING", event.audit_event_id),
            bigquery.ScalarQueryParameter("return_id", "STRING", event.return_id),
            bigquery.ScalarQueryParameter("assessment_id", "STRING", event.assessment_id),
            bigquery.ScalarQueryParameter("event_type", "STRING", event.event_type),
            bigquery.ScalarQueryParameter("event_at", "TIMESTAMP", event.event_at),
            bigquery.ScalarQueryParameter("actor_type", "STRING", event.actor_type),
            bigquery.ScalarQueryParameter("actor_name", "STRING", event.actor_name),
            bigquery.ScalarQueryParameter("status_from", "STRING", event.status_from),
            bigquery.ScalarQueryParameter("status_to", "STRING", event.status_to),
            bigquery.ScalarQueryParameter("summary", "STRING", event.summary),
            bigquery.ScalarQueryParameter(
                "details_json", "STRING",
                None if event.details_json is None else json.dumps(event.details_json),
            ),
            bigquery.ScalarQueryParameter("trace_id", "STRING", event.trace_id),
        ]
        self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

    def list_for_return(self, return_id: str) -> list[dict[str, Any]]:
        sql = f"""
        SELECT * FROM `{self.table}`
        WHERE return_id = @return_id
        ORDER BY event_at, created_at
        """
        rows = self.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("return_id", "STRING", return_id)
        ])).result()
        return [dict(row.items()) for row in rows]
