"""Enable the reusable shell capability for the bot's standard toolsets."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.capability import AgentDeclarationView
from modex_agent.plugins.defaults.capabilities.shell import (
    SHELL_CAPABILITY_NAME,
    ShellCapability,
)
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
from modex_agent.tools.presets import ToolPreset

_AUTO_SHELL_TOOLSETS = frozenset(
    {
        ToolPreset.FULL.value,
        ToolPreset.READ_WRITE.value,
        ToolPreset.READ_ONLY.value,
    }
)


class BotShellConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BotShellCapability(ShellCapability):
    """Bot policy for preset defaults and explicit tool-list shell requests."""

    def applies(self, view: AgentDeclarationView) -> bool:
        if (
            view.declared.toolset is None
            or view.declared.toolset in _AUTO_SHELL_TOOLSETS
        ):
            return True
        tools = view.declared.tools or []
        if any(entry.startswith(("+", "-")) for entry in tools):
            return "+bash" in tools
        return "bash" in tools


class BotShellPlugin(Plugin):
    """Project-priority shell policy; implementation remains framework-owned."""

    config_model = BotShellConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_capability(
            SHELL_CAPABILITY_NAME,
            BotShellCapability(),
        )


__all__ = ["BotShellCapability", "BotShellConfig", "BotShellPlugin"]
