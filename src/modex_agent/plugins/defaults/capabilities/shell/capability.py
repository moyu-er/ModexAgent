"""Capability declaration and per-agent wiring for the native shell group."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.core.tool_group import ToolGroupSpec, ToolGroupVariant
from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityWiring,
    FinalRosterView,
    TreePositionView,
)
from modex_agent.sandbox.shell_plan import SandboxBinding, resolved_binding
from modex_agent.workspace.boundary import canonicalize_path

if TYPE_CHECKING:
    from modex_agent.plugins.assembly.context import AgentContext

SHELL_CAPABILITY_NAME = "shell"
SHELL_WIRING_KEY = "shell"


class ShellMode(StrEnum):
    SUBPROCESS = "subprocess"
    PERSISTENT = "persistent"
    TERMINAL = "terminal"


class ShellCapabilityConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: ShellMode = ShellMode.PERSISTENT
    terminal_visibility: bool = False


class _ShellBindingPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    active: bool
    config: ShellCapabilityConfig


class ShellWiring:
    """Typed runtime artifact shared by shell capability assembly and its factory."""

    def __init__(
        self,
        *,
        config: ShellCapabilityConfig,
        binding: SandboxBinding | None,
        initial_cwd: str,
        is_main: bool,
        agent_name: str,
    ) -> None:
        self.config = config
        self.binding = binding
        self.initial_cwd = initial_cwd
        self.is_main = is_main
        self.agent_name = agent_name


SHELL_TOOL_GROUP_SPEC = ToolGroupSpec(
    anchor="bash",
    variants=(
        ToolGroupVariant(name=ShellMode.SUBPROCESS, tools=("bash",)),
        ToolGroupVariant(
            name=ShellMode.PERSISTENT,
            tools=("bash", "bash_input"),
        ),
        ToolGroupVariant(
            name=ShellMode.TERMINAL,
            tools=("bash", "process", "terminal"),
        ),
    ),
)

class ShellCapability(Capability):
    """Reusable explicit-opt-in native shell group."""

    name = SHELL_CAPABILITY_NAME
    config_model: ClassVar[type[BaseModel]] = ShellCapabilityConfig

    def contribute(
        self, tree: TreePositionView, config: BaseModel
    ) -> CapabilityContribution:
        shell_config = ShellCapabilityConfig.model_validate(config.model_dump())
        if shell_config.mode is ShellMode.SUBPROCESS:
            variants = SHELL_TOOL_GROUP_SPEC.variants[:1]
        elif shell_config.mode is ShellMode.PERSISTENT or not tree.is_root:
            variants = SHELL_TOOL_GROUP_SPEC.variants[:2]
        else:
            variants = SHELL_TOOL_GROUP_SPEC.variants
        return CapabilityContribution(
            tools=("bash",),
            tool_groups=(
                ToolGroupSpec(
                    anchor=SHELL_TOOL_GROUP_SPEC.anchor,
                    variants=variants,
                ),
            ),
        )

    def bind(
        self,
        tree: TreePositionView,
        config: BaseModel,
        final: FinalRosterView,
    ) -> CapabilityBinding:
        del tree
        shell_config = ShellCapabilityConfig.model_validate(config.model_dump())
        payload = _ShellBindingPayload(
            active="bash" in final.tools,
            config=shell_config,
        )
        return CapabilityBinding(payload=payload.model_dump())

    async def assemble(
        self, binding: CapabilityBinding, ctx: AgentContext
    ) -> CapabilityWiring:
        payload = _ShellBindingPayload.model_validate(binding.payload)
        if not payload.active:
            return CapabilityWiring()
        if ctx.spec is None:
            raise ValueError("shell capability assembly requires the compiled agent spec")

        pool_runtime = ctx.pool_runtime
        sandbox_binding = await resolved_binding(
            pool_runtime.interceptor_chain if pool_runtime is not None else None
        )
        root_provider = pool_runtime.root_provider if pool_runtime is not None else None
        root = root_provider.current() if root_provider is not None else ctx.workspace_ctx.target
        wiring = ShellWiring(
            config=payload.config,
            binding=sandbox_binding,
            initial_cwd=str(canonicalize_path(root)),
            is_main=ctx.spec.agent_type is AgentType.native_main,
            agent_name=ctx.agent_name,
        )
        return CapabilityWiring(artifacts={SHELL_WIRING_KEY: wiring})


__all__ = [
    "SHELL_CAPABILITY_NAME",
    "SHELL_TOOL_GROUP_SPEC",
    "SHELL_WIRING_KEY",
    "ShellCapability",
    "ShellCapabilityConfig",
    "ShellMode",
    "ShellWiring",
]
