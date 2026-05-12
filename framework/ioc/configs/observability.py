"""Observability configuration."""

from pydantic import BaseModel


class ObservabilityConfig(BaseModel):
    """Observability configuration. None = no logging/tracing."""

    run_logging: bool = True
    level: str = "INFO"
