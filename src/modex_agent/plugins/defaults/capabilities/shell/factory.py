"""The single TOOL-slot factory for every native shell group variant."""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.core.tool_group import ToolGroup, ToolGroupResource
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AgentContext
from modex_agent.sandbox.container_executor import ContainerShellExecutor
from modex_agent.sandbox.runtime import ResolvedSandbox
from modex_agent.sandbox.settings import SandboxBackend
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import (
    TerminalManagerBase,
    create_terminal_manager_or_none,
)
from modex_agent.tools.terminal.persistent_bash import (
    BashInputTool,
    PersistentBashTool,
    PersistentShellManager,
    persistent_bash_supported,
)
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.subprocess_tool import (
    SubprocessTool,
    create_subprocess_executor,
)
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.watchdog import TerminalWatchdog

from .capability import (
    SHELL_CAPABILITY_NAME,
    SHELL_WIRING_KEY,
    ShellMode,
    ShellWiring,
)

logger = logging.getLogger(__name__)


class ShellSelectionReason(StrEnum):
    REQUESTED = "requested"
    SUBAGENT_TERMINAL_DOWNGRADE = "subagent_terminal_downgrade"
    SANDBOX_TERMINAL_DOWNGRADE = "sandbox_terminal_downgrade"
    TERMINAL_UNAVAILABLE = "terminal_unavailable"
    PERSISTENT_UNAVAILABLE = "persistent_unavailable"


class ShellToolFactoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _PersistentShellResource(ToolGroupResource):
    def __init__(self, manager: PersistentShellManager) -> None:
        self._manager = manager
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            await self._manager.close_all()
            self._closed = True


class _TerminalShellResource(ToolGroupResource):
    def __init__(
        self,
        manager: TerminalManagerBase,
        watchdog: TerminalWatchdog,
    ) -> None:
        self._manager = manager
        self._watchdog = watchdog
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            await self._watchdog.stop()
            first_error: BaseException | None = None
            for name in list(self._manager.list_names()):
                try:
                    await self._manager.close(name)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
            self._closed = True


class ShellToolGroupFactory(ComponentFactory):
    """Build exactly one owned shell group for one native agent."""

    config_model: ClassVar[type[BaseModel]] = ShellToolFactoryConfig

    async def create(self, config: BaseModel, ctx: AgentContext) -> ToolGroup:
        del config
        wiring = _require_shell_wiring(ctx)
        resolved = wiring.binding.current() if wiring.binding is not None else None
        if resolved is not None and resolved.backend is not SandboxBackend.HOST:
            group, reason = await _sandbox_group(wiring, resolved)
        else:
            group, reason = await _host_group(
                wiring,
                tuple(resolved.shell_argv) if resolved else (),
            )
        effective_variant = ShellMode(group.variant).value
        logger.info(
            "Shell group assembled for %s: %s -> %s (%s)",
            wiring.agent_name,
            wiring.config.mode.value,
            effective_variant,
            reason.value,
            extra={
                "requested_mode": wiring.config.mode.value,
                "effective_variant": effective_variant,
                "selection_reason": reason.value,
                "shell_agent": wiring.agent_name,
                "shell_substrate": (
                    resolved.backend.value
                    if resolved is not None
                    else SandboxBackend.HOST.value
                ),
            },
        )
        return group


def _require_shell_wiring(ctx: AgentContext) -> ShellWiring:
    wirings = ctx.capability_wirings
    capability_wiring = (
        wirings.get(SHELL_CAPABILITY_NAME) if wirings is not None else None
    )
    artifact = (
        capability_wiring.artifacts.get(SHELL_WIRING_KEY)
        if capability_wiring is not None
        else None
    )
    if not isinstance(artifact, ShellWiring):
        raise ValueError(
            "bash requires capabilities: {shell: {}} so its atomic tool group "
            "has a typed per-agent wiring owner"
        )
    return artifact


async def _host_group(
    wiring: ShellWiring,
    shell_argv: tuple[str, ...],
) -> tuple[ToolGroup, ShellSelectionReason]:
    if wiring.config.mode is ShellMode.SUBPROCESS:
        return _host_subprocess_group(wiring), ShellSelectionReason.REQUESTED

    if wiring.config.mode is ShellMode.PERSISTENT:
        if persistent_bash_supported():
            return (
                await _persistent_group(
                    wiring, shell_argv=shell_argv, sandboxed=False
                ),
                ShellSelectionReason.REQUESTED,
            )
        return (
            _host_subprocess_group(wiring),
            ShellSelectionReason.PERSISTENT_UNAVAILABLE,
        )

    if not wiring.is_main:
        group = (
            await _persistent_group(
                wiring, shell_argv=shell_argv, sandboxed=False
            )
            if persistent_bash_supported()
            else _host_subprocess_group(wiring)
        )
        return group, ShellSelectionReason.SUBAGENT_TERMINAL_DOWNGRADE

    manager = create_terminal_manager_or_none(
        terminal_visibility=wiring.config.terminal_visibility,
        owner_name=wiring.agent_name,
        default_cwd=wiring.initial_cwd,
    )
    if manager is not None:
        return await _terminal_group(manager), ShellSelectionReason.REQUESTED

    group = (
        await _persistent_group(
            wiring, shell_argv=shell_argv, sandboxed=False
        )
        if persistent_bash_supported()
        else _host_subprocess_group(wiring)
    )
    return group, ShellSelectionReason.TERMINAL_UNAVAILABLE


async def _sandbox_group(
    wiring: ShellWiring,
    resolved: ResolvedSandbox,
) -> tuple[ToolGroup, ShellSelectionReason]:
    if wiring.config.mode is ShellMode.SUBPROCESS:
        return _sandbox_subprocess_group(wiring, resolved), ShellSelectionReason.REQUESTED

    if persistent_bash_supported() and resolved.shell_argv:
        group = await _persistent_group(
            wiring,
            shell_argv=tuple(resolved.shell_argv),
            sandboxed=True,
        )
        reason = (
            ShellSelectionReason.SANDBOX_TERMINAL_DOWNGRADE
            if wiring.config.mode is ShellMode.TERMINAL
            else ShellSelectionReason.REQUESTED
        )
        return group, reason

    reason = (
        ShellSelectionReason.SANDBOX_TERMINAL_DOWNGRADE
        if wiring.config.mode is ShellMode.TERMINAL
        else ShellSelectionReason.PERSISTENT_UNAVAILABLE
    )
    return _sandbox_subprocess_group(wiring, resolved), reason


def _sandbox_subprocess_group(
    wiring: ShellWiring,
    resolved: ResolvedSandbox,
) -> ToolGroup:
    assert wiring.binding is not None
    if not resolved.one_shot_command_argv_prefix:
        raise ValueError(
            f"resolved {resolved.backend.value} substrate carries neither a "
            "persistent shell_argv nor a one-shot prefix — refusing to "
            "silently run a host shell under a FULL enforcement report"
        )
    shell_path = (
        resolved.shell_argv[-4]
        if resolved.backend is SandboxBackend.LOCAL and resolved.shell_argv
        else "/bin/bash"
    )
    bash = SubprocessTool(
        executor=ContainerShellExecutor(
            list(resolved.one_shot_command_argv_prefix),
            backend=resolved.backend,
            shell_path=shell_path,
            binding=wiring.binding,
        ),
        timeout=300,
        working_dir=wiring.initial_cwd,
    )
    return ToolGroup(
        anchor="bash",
        variant=ShellMode.SUBPROCESS,
        tools=(bash,),
    )


async def _persistent_group(
    wiring: ShellWiring,
    *,
    shell_argv: tuple[str, ...],
    sandboxed: bool,
) -> ToolGroup:
    manager = PersistentShellManager(
        initial_cwd=wiring.initial_cwd,
        max_output_chars=None,
        shell_argv=list(shell_argv) if shell_argv else None,
        launch_owner=wiring.binding if sandboxed else None,
    )
    resource = _PersistentShellResource(manager)
    try:
        return ToolGroup(
            anchor="bash",
            variant=ShellMode.PERSISTENT,
            tools=(
                PersistentBashTool(manager=manager),
                BashInputTool(manager=manager),
            ),
            resource=resource,
        )
    except BaseException:
        try:
            await resource.aclose()
        except BaseException:
            logger.exception("shell persistent group rollback failed")
        raise


def _host_subprocess_group(wiring: ShellWiring) -> ToolGroup:
    bash = SubprocessTool(
        executor=create_subprocess_executor(),
        timeout=300,
        working_dir=wiring.initial_cwd,
    )
    return ToolGroup(
        anchor="bash",
        variant=ShellMode.SUBPROCESS,
        tools=(bash,),
    )


async def _terminal_group(manager: TerminalManagerBase) -> ToolGroup:
    registry = ProcessRegistry()
    watchdog = TerminalWatchdog(manager, registry)
    resource = _TerminalShellResource(manager, watchdog)
    try:
        tools = (
            CommandTool(
                manager=manager,
                registry=registry,
                config=TerminalRuntimeConfig(),
            ),
            ProcessTool(registry=registry, manager=manager),
            TerminalTool(manager, registry=registry),
        )
        watchdog.start()
        return ToolGroup(
            anchor="bash",
            variant=ShellMode.TERMINAL,
            tools=tools,
            resource=resource,
        )
    except BaseException:
        try:
            await resource.aclose()
        except BaseException:
            logger.exception("shell terminal group rollback failed")
        raise


__all__ = ["ShellToolFactoryConfig", "ShellToolGroupFactory"]
