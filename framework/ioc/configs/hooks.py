"""Hook configuration."""

from pydantic import BaseModel, Field


class HookConfig(BaseModel):
    """Configuration for a single hook."""

    name: str
    enabled: bool = True


class HooksConfig(BaseModel):
    """Agent hook configuration.

    None = disable all hooks.
    Default = use built-in defaults (logging, runtime_context).
    """

    items: list[HookConfig] = Field(
        default_factory=lambda: [
            HookConfig(name="logging"),
            HookConfig(name="runtime_context"),
        ]
    )
