"""Plugin system configuration."""

from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    """Plugin system configuration.

    None (no plugins: section in YAML) = plugins disabled.
    enabled=False (explicit) = plugins disabled.
    enabled=True = discover and load plugins (each plugin still respects its own
    `configurations[<name>].enabled` if defined).
    """

    enabled: bool = False
    configurations: dict[str, dict[str, object]] = Field(default_factory=dict)
