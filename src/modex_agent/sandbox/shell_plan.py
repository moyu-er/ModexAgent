"""Common native bash assembly for main agents and subagents.

``build_bash_tool`` chooses among CommandTool, PersistentBashTool, and
SubprocessTool. LOCAL/OCI binds the resolved launcher to a persistent shell
when PTY support exists, otherwise to the one-shot ContainerShellExecutor.
Missing both launcher products is a configuration error, not silent HOST use.

HOST or no guard uses the pool terminal pair first, then a stateless executor
without PTY support, otherwise a supplied or fresh persistent shell. Explicit
HOST keeps configured guards; DEFAULT leaves substrate assembly dormant.
``process`` and ``terminal`` remain host-terminal controls, not isolated bash.
The substrate report does not certify the whole tool roster.

``SandboxBinding`` shares execution and telemetry state per conversation.
Only confirmed startup unavailability before command submission permits HOST
fallback for either main agents or subagents. Uncertain operations are never
replayed, and fallback does not grant permission or disable subagent bash.

Native assembly's ``ensure_input_companion`` binds ``bash_input`` to the
final persistent bash manager, not a discarded host shell. This integration
depends on framework tool, runtime, and workspace contracts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.runtime.env_context import _current_session_id
from modex_agent.sandbox.runtime import HostRuntime, ResolvedSandbox
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings
from modex_agent.tools.terminal.subprocess_tool import ShellLaunchOwner
from modex_agent.workspace.boundary import canonicalize_path

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import Tool
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.tools.terminal.managers import TerminalManagerBase
    from modex_agent.tools.terminal.persistent_bash import (
        PersistentBashTool,
    )
    from modex_agent.tools.terminal.process_registry import ProcessRegistry
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

__all__ = ["SandboxBinding", "ShellAssemblyDeps", "build_bash_tool", "resolved_binding", "resolved_substrate"]

logger = logging.getLogger(__name__)


class SandboxBinding(ShellLaunchOwner):
    """Live substrate shared by execution and telemetry, per conversation.

    A failed startup changes only that conversation. Other live sandbox
    sessions retain their substrate, cwd, environment and pending input.
    """

    def __init__(self, resolved: ResolvedSandbox) -> None:
        self._initial = resolved
        self._effective: dict[str | None, ResolvedSandbox] = {}

    def effective(self, session_id: str | None) -> ResolvedSandbox:
        return self._effective.get(session_id, self._initial)

    def current(self) -> ResolvedSandbox:
        return self.effective(_current_session_id.get())

    def shell_argv(self, session_id: str | None) -> tuple[str, ...] | None:
        resolved = self.effective(session_id)
        return None if resolved.backend is SandboxBackend.HOST else tuple(resolved.shell_argv)

    async def fallback(self, session_id: str | None, reason: str) -> bool:
        if self.effective(session_id).backend is SandboxBackend.HOST:
            return False
        self._effective[session_id] = await HostRuntime(
            degraded_reason=f"sandbox startup unavailable before target submission: {reason}"
        ).resolve(SandboxSettings(backend=SandboxBackend.HOST), Path.cwd())
        return True


async def resolved_substrate(
    chain: InterceptorChain | None,
) -> ResolvedSandbox | None:
    """The chain's sandbox substrate, read from the assembled guard.

    The guard resolved eagerly at its own factory create; the await is
    the idempotent cached read (concurrency-guarded inside the guard).
    ``None`` (no chain, or no guard in it) means host mode — both argv
    products stay unset and the host path is bit-exact with a
    guard-less assembly. The interceptor chain is the plugin extension
    boundary, so the ``isinstance`` narrowing to the framework guard is
    the sanctioned boundary check (a name-only match could hand a
    same-named foreign interceptor to the typed read).
    """
    binding = await resolved_binding(chain)
    return binding.current() if binding is not None else None


async def resolved_binding(chain: InterceptorChain | None) -> SandboxBinding | None:
    """Read the execution owner, not a captured snapshot, from the guard."""
    if chain is None:
        return None
    from modex_agent.sandbox.interceptor import SandboxGuardInterceptor

    for interceptor in chain.interceptors:
        if isinstance(interceptor, SandboxGuardInterceptor):
            return await interceptor.execution_binding()
    return None


class ShellAssemblyDeps:
    """Assembly-time runtime references, not a serialized value object."""

    def __init__(
        self,
        resolved: ResolvedSandbox | None = None,
        terminal_pair: tuple[TerminalManagerBase, ProcessRegistry] | None = None,
        persistent_bash: PersistentBashTool | None = None,
        root_provider: WorkspaceRootProvider | None = None,
        pty_supported: bool = True,
        binding: SandboxBinding | None = None,
    ) -> None:
        self.resolved = resolved
        self.terminal_pair = terminal_pair
        self.persistent_bash = persistent_bash
        self.root_provider = root_provider
        self.pty_supported = pty_supported
        self.binding = binding


def build_bash_tool(deps: ShellAssemblyDeps) -> Tool:
    """Build the roster ``bash`` tool on the plan's chosen substrate.

    LOCAL/OCI uses resolved launchers. HOST / no guard prefers the terminal
    pair; without one, PTY support selects persistent versus stateless bash.
    """
    resolved = deps.binding.current() if deps.binding is not None else deps.resolved
    if resolved is not None and resolved.backend is not SandboxBackend.HOST:
        return _sandboxed_shell(deps, resolved)
    return _host_shell(deps, resolved)


def _sandboxed_shell(deps: ShellAssemblyDeps, resolved: ResolvedSandbox) -> Tool:
    """Bind bash to LOCAL/OCI argv without reusing a host shell.

    Startup-only fallback belongs to the shared binding, not this factory.
    """
    from modex_agent.tools.terminal.persistent_bash import (
        PersistentBashTool,
        PersistentShellManager,
    )
    binding = deps.binding if deps.binding is not None else SandboxBinding(resolved)

    if deps.pty_supported and resolved.shell_argv:
        # max_output_chars=None: truncation is interceptor-owned — every
        # assembly road wires ToolResultLimitInterceptor (50K); a self-
        # clipping shell would truncate BEFORE the interceptor sees the
        # full output.
        return PersistentBashTool(
            manager=PersistentShellManager(
                initial_cwd=_initial_cwd(deps),
                max_output_chars=None,
                shell_argv=list(resolved.shell_argv),
                launch_owner=binding,
            ),
        )
    if resolved.one_shot_command_argv_prefix:
        from modex_agent.sandbox.container_executor import ContainerShellExecutor
        from modex_agent.tools.terminal.subprocess_tool import SubprocessTool

        return SubprocessTool(
            executor=ContainerShellExecutor(
                list(resolved.one_shot_command_argv_prefix), backend=resolved.backend,
                shell_path=(resolved.shell_argv[-4] if resolved.backend is SandboxBackend.LOCAL
                            and resolved.shell_argv else "/bin/bash"),
                binding=binding,
            ),
            timeout=300,
            working_dir=_initial_cwd(deps),
        )
    raise ValueError(
        f"resolved {resolved.backend.value} substrate carries neither a "
        "persistent shell_argv nor a one-shot prefix — refusing to "
        "silently run a host shell under a FULL enforcement report"
    )


def _host_shell(deps: ShellAssemblyDeps, resolved: ResolvedSandbox | None) -> Tool:
    """Build HOST bash, preferring the pool terminal pair.

    Without a terminal pair, no PTY means stateless SubprocessTool.
    With PTY support, use the supplied persistent shell or create one rooted
    at the workspace; resolved HOST argv seeds a fresh manager when present.
    """
    from modex_agent.tools.terminal.command_tool import CommandTool
    from modex_agent.tools.terminal.config import TerminalRuntimeConfig
    from modex_agent.tools.terminal.persistent_bash import (
        PersistentBashTool,
        PersistentShellManager,
    )

    if deps.terminal_pair is not None:
        manager, registry = deps.terminal_pair
        return CommandTool(manager=manager, registry=registry, config=TerminalRuntimeConfig())
    if not deps.pty_supported:
        from modex_agent.tools.terminal.subprocess_tool import (
            SubprocessTool,
            create_subprocess_executor,
        )

        logger.warning(
            "bash fallback: POSIX pty unavailable on this host; "
            "bash falls back to SubprocessTool (stateless)"
        )
        return SubprocessTool(
            executor=create_subprocess_executor(), timeout=300, working_dir=_initial_cwd(deps)
        )
    if deps.persistent_bash is not None:
        return deps.persistent_bash
    host_argv = resolved.shell_argv if resolved is not None else []
    # max_output_chars=None: see _sandboxed_shell — interceptor-owned.
    return PersistentBashTool(
        initial_cwd=_initial_cwd(deps),
        max_output_chars=None,
        manager=PersistentShellManager(
            initial_cwd=_initial_cwd(deps), max_output_chars=None,
            shell_argv=list(host_argv),
        ) if host_argv else None,
    )


def _initial_cwd(deps: ShellAssemblyDeps) -> str | None:
    """Birth-parameter spawn cwd: the shell's ``cd`` state must survive
    across calls, so the root is read once at assembly. ``None`` spawns
    in the process CWD (bare test contexts only — production
    assemblies always carry a root_provider)."""
    return str(canonicalize_path(deps.root_provider.current())) if deps.root_provider is not None else None
