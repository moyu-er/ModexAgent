"""Live sandbox binding shared by shell execution and telemetry.

``SandboxBinding`` shares execution and telemetry state per conversation.
Only confirmed startup unavailability before command submission permits HOST
fallback for either main agents or subagents. Uncertain operations are never
replayed, and fallback does not grant permission or disable subagent bash.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.runtime.env_context import _current_session_id
from modex_agent.sandbox.runtime import HostRuntime, ResolvedSandbox
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings
from modex_agent.tools.terminal.subprocess_tool import ShellLaunchOwner

if TYPE_CHECKING:
    from modex_agent.interceptor.chain import InterceptorChain

__all__ = ["SandboxBinding", "resolved_binding", "resolved_substrate"]


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
