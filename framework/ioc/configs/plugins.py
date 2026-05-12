"""Plugin system configuration."""

from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    """Plugin system configuration. None = plugins disabled."""

    enabled: bool = True
    configurations: dict[str, dict[str, object]] = Field(default_factory=dict)
