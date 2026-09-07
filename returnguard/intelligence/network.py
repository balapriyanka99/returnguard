"""Network Intelligence construction from contextual relationship evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import NetworkIntelligence
from .repository import IntelligenceRepository


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def build_network_intelligence(
    repository: IntelligenceRepository,
    row: dict[str, Any],
    assessment_at: datetime,
) -> NetworkIntelligence:
    links = repository.get_network_links(row["return_id"], assessment_at)
    current_user = int(row["user_id"])
    users, returns, identifiers, relationships = set(), set(), set(), set()
    first, last = [], []
    for link in links:
        for key in ("user_id", "linked_user_id"):
            value = link.get(key)
            if value is not None and int(value) != current_user:
                users.add(int(value))
        if link.get("return_id"):
            returns.add(link["return_id"])
        if link.get("network_identifier"):
            identifiers.add(link["network_identifier"])
        if link.get("relationship_type"):
            relationships.add(link["relationship_type"])
        if link.get("first_observed_at"):
            first.append(link["first_observed_at"])
        if link.get("last_observed_at"):
            last.append(link["last_observed_at"])
    controlled = any(link.get("source_type") == "synthetic_demo" for link in links)
    return NetworkIntelligence(
        user_id=current_user, assessment_at=assessment_at, linked_user_count=len(users),
        linked_return_count=len(returns), network_identifiers=sorted(identifiers),
        relationship_types=sorted(relationships), linked_user_ids=sorted(users),
        first_observed_at=min(first) if first else None, last_observed_at=max(last) if last else None,
        recent_network_activity={
            "links_30d": sum(
                1 for value in last if 0 <= (_utc(assessment_at) - _utc(value)).days <= 30
            ),
            "links_90d": sum(
                1 for value in last if 0 <= (_utc(assessment_at) - _utc(value)).days <= 90
            ),
        },
        relationship_strength=None,
        coordination_indicators={"shared_identifier_count": len(identifiers)} if links else None,
        network_history_confidence="controlled" if controlled else "limited" if links else "none",
        data_origin="controlled_demo" if controlled else "live_source" if links else "insufficient_history",
    )
