"""Environment-backed Google ADK and Gemini configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    model: str = "gemini-2.5-flash"
    google_cloud_project: str | None = None
    google_cloud_location: str | None = None
    use_vertex_ai: bool = False
    app_name: str = "returnguard"

    @classmethod
    def from_env(cls) -> "AgentConfig":
        vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}
        return cls(
            model=os.getenv("RETURNGUARD_GEMINI_MODEL", cls.model),
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION"),
            use_vertex_ai=vertex,
        )

    def validate_for_live_model(self) -> None:
        if not self.model.strip():
            raise ValueError("RETURNGUARD_GEMINI_MODEL must not be empty")
        if self.use_vertex_ai and not self.google_cloud_project:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required when GOOGLE_GENAI_USE_VERTEXAI is enabled")
        if self.use_vertex_ai and not self.google_cloud_location:
            raise ValueError("GOOGLE_CLOUD_LOCATION is required when GOOGLE_GENAI_USE_VERTEXAI is enabled")
