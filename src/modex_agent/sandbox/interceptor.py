"""Opt-in execute-time guards for framework-intercepted tool calls.

Judgments use ``SecurityDecisionService``, the same implementation as the
approval classifier. This layer owns denial presentation, the exact-call
approval-marker backstop, execution-uncertainty feedback, and telemetry.
The current substrate snapshot is refreshed in per-turn state before and
after execution. It is not an approval or routing decision.

Known targets receive backend-independent checks. Unknown tools claim no
boundary coverage; external provider internal tools bypass this interceptor.
HOST keeps configured guards but cannot contain dynamic shell code.

DEFAULT dormancy: the assembly factory must not build this interceptor
when ``settings.backend == DEFAULT`` (first gate); the runtime's
``ResolvedSandbox`` validator is the second. Constructing directly with
DEFAULT settings raises at first resolve — a program error.

Runtime selection lives in ``selection.py`` (``resolve_selection`` +
``select_runtime``) — the single probe + factory owner. Presentation
(verdict dispatch, denial copy, marker-anchor waiver, denial-feature
translation, container-death signatures) lives in
``guard_presentation.py``, which also documents the tool matrix.

Container-death errors never trigger re-resolution or command replay. The
bound executor remains unchanged and the caller receives an uncertainty
warning: inspect side effects and restore the selected container or rebuild
the shell before another attempt. Operation denials do not authorize HOST
fallback. ``translate_denial`` only adds policy context to denial text.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from modex_agent.core.tool_manager import ToolResult
from modex_agent.interceptor.abc import (
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
)
from modex_agent.sandbox.decision import (
    GuardCategory,
    GuardVerdict,
    SecurityDecisionService,
)
from modex_agent.sandbox.guard_presentation import (
    anchor_matches_approval,
    evaluate_call,
    is_container_dead,
    sandbox_restarted_error_text,
    translate_denial,
    verdict_to_denial,
)
from modex_agent.sandbox.runtime import (
    ResolvedSandbox,
    SandboxRuntime,
    write_enforcement_snapshot,
)
from modex_agent.sandbox.settings import (
    SandboxBackend,
    SandboxSettings,
)
from modex_agent.sandbox.shell_plan import SandboxBinding
from modex_agent.sandbox.tool_matrix import (
    describe_tool_security,
    extract_call_target,
)

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

__all__ = [
    "SandboxGuardInterceptor",
    "translate_denial",
]


class SandboxGuardInterceptor(ToolCallInterceptor):
    """Policy-layer guard around every tool call (opt-in, DEFAULT-dormant).

    Settings, runtime, workspace-root provider, and decision service are
    injected. Classification shares the decision implementation and root
    semantics, not necessarily this service instance. Assembly eagerly
    resolves the substrate; concurrent tool calls reuse the cached binding.

    Human-approved passthrough: a call the user approved on
    a card carries ``TurnCustomKey.HUMAN_APPROVED_CALLS`` in turn state
    (written by ``ApprovalResumer`` on the ALLOW decision). When this
    interceptor is about to deny with a BOUNDARY verdict and the call's
    marker anchor matches the arguments (same anchor derivation, same
    live root), that boundary denial is waived. HARDLINE
    categories (DENY_RULE / TRAVERSAL / SSRF) never honor the marker, and
    an anchor mismatch still denies. Command-text anchors do not bind dynamic
    shell state or prove that all runtime effects match the user's intent.
    """

    def __init__(
        self,
        settings: SandboxSettings,
        runtime: SandboxRuntime,
        workspace_root_provider: WorkspaceRootProvider,
        decision: SecurityDecisionService,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._root_provider = workspace_root_provider
        self._decision = decision
        self._binding: SandboxBinding | None = None
        self._resolve_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "sandbox_guard"

    async def close(self) -> None:
        """Release runtime artifacts after consumers close every bound shell."""
        await self._runtime.close()

    # -- resolve lifecycle ------------------------------------------------

    async def ensure_resolved(self) -> ResolvedSandbox:
        """Resolve the substrate once (idempotent; concurrency-guarded).

        The factory calls this eagerly (pool assembly) so shell seams read the argv
        before any tool runs; tool calls hit the cached path.
        """
        if self._settings.backend is SandboxBackend.DEFAULT:
            # Second DEFAULT gate (factory is the first, mirroring the
            # ResolvedSandbox validator): direct construction with the
            # dormant tier is a program error, never a runtime concern.
            raise ValueError(
                "DEFAULT is not a runtime tier — the assembly layer must "
                "keep the sandbox dormant when backend == DEFAULT"
            )
        if self._binding is None:
            async with self._resolve_lock:
                if self._binding is None:
                    resolved = await self._runtime.resolve_available(
                        self._settings, self._root_provider.current()
                    )
                    self._binding = SandboxBinding(resolved)
        return self._binding.current()

    async def execution_binding(self) -> SandboxBinding:
        """Shared execution owner; telemetry reads its current substrate."""
        await self.ensure_resolved()
        assert self._binding is not None
        return self._binding

    @property
    def shell_argv(self) -> tuple[str, ...]:
        """Resolved persistent-shell argv (empty before first resolve)."""
        return tuple(self._binding.current().shell_argv) if self._binding is not None else ()

    @property
    def one_shot_command_argv_prefix(self) -> tuple[str, ...]:
        """Resolved one-shot argv prefix (empty before first resolve)."""
        return (
            tuple(self._binding.current().one_shot_command_argv_prefix)
            if self._binding is not None
            else ()
        )

    # -- interception -----------------------------------------------------

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        await self.ensure_resolved()
        self._write_snapshot(ctx)

        verdict = self._evaluate(call)
        attempted = extract_call_target(
            describe_tool_security(call.tool_name), dict(call.arguments)
        )
        denial = verdict_to_denial(
            verdict,
            self._settings.policy,
            str(self._root_provider.current()),
            call.tool_name,
            attempted.path or attempted.command or attempted.url,
        )
        if denial is not None and self._waive_for_marker(verdict, call, ctx):
            denial = None
        if denial is not None:
            return ToolResult(
                tool_name=call.tool_name,
                call_id=call.tool_call.call_id or "",
                error=denial,
            )

        try:
            result = await next_call()
        finally:
            self._write_snapshot(ctx)
        if self._container_dead(result):
            return self._sandbox_restarted_error(call, result)
        return result

    # -- checks (delegated: the service judges, this layer words) ----------

    def _evaluate(self, call: ToolCallContext) -> GuardVerdict:
        """Dispatch by tool name onto the shared SecurityDecisionService."""
        return evaluate_call(self._decision, call)

    # -- exact-call human approval backstop --------------------------------

    def _waive_for_marker(
        self, verdict: GuardVerdict, call: ToolCallContext, ctx: AgentContext
    ) -> bool:
        """BOUNDARY denial waived iff this exact call was human-approved.

        The marker (``TurnCustomKey.HUMAN_APPROVED_CALLS`` in turn state,
        written by ``ApprovalResumer`` on the ALLOW decision) maps
        ``tool_call_id → anchor``; the anchor comparison lives in
        ``guard_presentation.anchor_matches_approval``. Only BOUNDARY
        honors the marker — HARDLINE categories (DENY_RULE / TRAVERSAL /
        SSRF) never honor it. A target/root change that alters the stored
        anchor still denies; command-text anchors do not bind shell state.
        """
        if verdict.category is not GuardCategory.BOUNDARY:
            return False
        state = ctx.runtime.state if ctx.runtime is not None else None
        if state is None:
            return False
        # Load the runtime-state key only when checking an approval marker.
        from modex_agent.runtime.enums import TurnCustomKey

        return anchor_matches_approval(
            call.tool_call.call_id or "",
            call.tool_name,
            dict(call.arguments),
            self._root_provider.current(),
            state.custom.get(TurnCustomKey.HUMAN_APPROVED_CALLS),
        )

    # -- execution uncertainty (never replay or replace a bound executor) --

    def _container_dead(self, result: ToolResult) -> bool:
        if result.error is None or self._binding is None:
            return False
        if self._binding.current().backend is not SandboxBackend.OCI:
            return False
        return is_container_dead(result.error)

    @staticmethod
    def _sandbox_restarted_error(call: ToolCallContext, result: ToolResult) -> ToolResult:
        return ToolResult(
            tool_name=call.tool_name,
            call_id=call.tool_call.call_id or "",
            error=sandbox_restarted_error_text(result.error or ""),
        )

    # -- snapshot -----------------------------------------------------------

    def _write_snapshot(self, ctx: AgentContext) -> None:
        """Telemetry-only enforcement record into per-turn custom state."""
        if self._binding is None:
            return
        resolved = self._binding.current()
        state = ctx.runtime.state if ctx.runtime is not None else None
        if state is None:
            return
        write_enforcement_snapshot(state.custom, resolved)
