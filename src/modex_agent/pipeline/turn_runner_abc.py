"""TurnRunner ABC — the seam between AgentPipeline and concrete turn runners.

The AgentPipeline owns pre-lock dispatch (route -> dedup -> busy mode -> session
lock) and then delegates the locked turn to a ``TurnRunner`` ABC implementation
via :meth:`TurnRunner.process_locked`. The runner owns everything
strategy-specific about a single turn: context assembly, approval
suspend/resume, governance, hooks, interceptors, runtime state (react's
``ReActTurnRunner``); or the minimal "set ``current_input``, call
``agent.run()``, fire ``on_session_start``/``on_session_end``" path
(external's ``ExternalTurnRunner``).

``AgentPipeline`` holds a ``TurnRunner`` (ABC) reference, never a concrete
subclass — adding a new strategy never touches the pipeline.

Why the ABC lives in ``pipeline/`` (not ``multi_agent/``)
---------------------------------------------------------
To preserve the existing ``multi_agent/ -> pipeline/`` non-dependence.
``multi_agent/execution_strategy.py`` imports ``TurnRunner`` from this module
under ``TYPE_CHECKING`` only — a type-only dependency. A runtime import would
be a cycle, because ``pipeline/`` already depends on ``multi_agent/`` at
runtime (for ``RouteResult``). Keeping the ABC in ``pipeline/`` means the
type-only arrow points ``multi_agent/ -> pipeline/`` and no runtime cycle is
introduced.

Concrete runners
----------------
- ``ReActTurnRunner`` — ``pipeline/turn_runner.py`` (renamed from ``TurnRunner``
  in Ticket 2 of the execution-strategy refactor).
- ``ExternalTurnRunner`` — ``agents/external/turn_runner.py``.

See ADR-0025 (D3) for the full decision rationale.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.context import ContextManager
    from modex_agent.core.emitter import AgentResult, ContentEmitter
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.core.types import InputMessage
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.multi_agent.router import RouteResult
    from modex_agent.pipeline.approval_renderer import ApprovalRenderer
    from modex_agent.pipeline.snapshot import PoolDataSnapshot
    from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
    from modex_agent.runtime.context import RuntimeContextManager
    from modex_agent.runtime.models import TurnSnapshot
    from modex_agent.runtime.store import TurnStateStore
    from modex_agent.workspace import WorkspaceManager

__all__ = ["TurnRunner"]


class TurnRunner(ABC):
    """Abstract base class for locked-turn runners.

    The primary contract is :meth:`process_locked` (abstract). Concrete runners
    implement the full turn-execution recipe (react: context assembly +
    approval + graph loop; external: minimal ``agent.run()`` with env
    wiring) inside it.

    The ABC also exposes a small set of pipeline-facing seams (methods with
    no-op defaults + read-only properties returning ``None``/``[]``) that the
    pipeline needs for pre-lock dispatch, lifecycle, and late-binding. React
    runners override them; external runners use the defaults. These are
    NOT strategy mirrors (ADR-0025 D4) — they are legitimate pipeline→runner
    queries. The strategy's ``assemble()`` configures the runner fully at
    assembly time; post-construction wiring targets the runner's sub-objects
    directly (never the pipeline).
    """

    # ── Abstract method: the locked-turn entry point ─────────────────────

    @abstractmethod
    async def process_locked(
        self,
        input_msg: InputMessage,
        session_id: str,
        route_result: RouteResult | None = None,
        *,
        session: SessionInfo,
    ) -> AgentResult | None:
        """Process one message while holding the session lock.

        Args:
            input_msg: The user/agent input to process.
            session_id: The session this turn belongs to.
            route_result: Optional routing decision from the pre-lock
                dispatch (router). ``None`` for non-routed inputs.
            session: Typed session info (session_id, agent_name, etc.).

        Returns:
            ``AgentResult`` on a completed turn, or ``None`` when the turn was
            suspended (e.g. approval ``GraphInterrupt``) or short-circuited
            (e.g. slash command handled without triggering the agent).
        """
        ...

    # ── Lifecycle methods (no-op defaults; react overrides) ──────────────

    async def cleanup_session(self, session_id: str) -> None:
        """Clean up per-session resources held by this runner.

        Called by ``AgentPipeline.cleanup_session_resources``. Default is a
        no-op (external has no per-session resources to clean).
        ReActTurnRunner delegates to ``ApprovalRenderer.cleanup_session``.
        """
        return None

    async def load_pending_approval(
        self,
        session_id: str,
        *,
        pool_data: PoolDataSnapshot | None = None,
    ) -> TurnSnapshot | None:
        """Load a pending approval snapshot for the session, if any.

        Called by ``AgentPipeline._load_pending_approval_snapshot`` for
        pre-lock command dispatch. Returns ``None`` by default
        (external has no approval flow). ReActTurnRunner delegates to
        ``ApprovalResumer.load_pending``.
        """
        return None

    def bind_to_pipeline(self, pipeline: AgentPipeline) -> None:
        """Late-bind any cycle that requires the pipeline instance.

        Called by the factory after the pipeline is constructed. Default is a
        no-op. ReActTurnRunner overrides to bind
        ``ApprovalRenderer.on_drain → pipeline._process_message`` (the single
        pipeline→approval→pipeline cycle broken by ticket 5a's prefactor).
        """
        return None

    # ── Post-construction wiring (no-op defaults; react overrides) ───────
    #
    # Two semantic batch-setters called by pool_builder after the runner is
    # constructed. Grouping the wiring into 2 methods (instead of 6 individual
    # setters) keeps the ABC surface small and makes the wiring intent
    # explicit at the call site. ExternalTurnRunner inherits the no-op
    # defaults (it has no pool context or builder-level emitter factory).

    def set_pool_context(
        self,
        *,
        workspace_manager: WorkspaceManager | None = None,
        pool_name: str | None = None,
    ) -> None:
        """Set pool-level context after construction (called by pool_builder).

        Default is a no-op (external has no pool context).
        ReActTurnRunner overrides to store ``workspace_manager`` + ``pool_name``
        on itself so per-turn ``_resolve_pool_data`` can resolve the active
        workspace's pool snapshot.
        """
        return None

    def set_emitter_factory(
        self, emitter_factory: Callable[..., ContentEmitter[Any]] | None
    ) -> None:
        """Set the emitter factory after construction (called by pool_builder).

        Default is a no-op. ReActTurnRunner delegates to its
        :class:`TurnContextBuilder`'s ``emitter_factory`` setter.
        ExternalTurnRunner stores it directly (it has no builder).
        """
        return None

    # ── Read-only properties (None/[] defaults; react overrides) ─────────
    #
    # These expose construction-time configuration that the pipeline needs for
    # pre-lock dispatch and lifecycle (dream engine, command context, routing).
    # They are NOT mirror setters — post-construction wiring targets the
    # runner's sub-objects directly (e.g. ``pipeline._turn_runner._builder._governance``).

    @property
    def agent_descriptor(self) -> AgentDescriptor | None:
        """The agent descriptor, if any. Used by pipeline for routing."""
        return None

    @property
    def context_manager(self) -> ContextManager | None:
        """The context manager. Used by pipeline for DreamScanner."""
        return None

    @property
    def skill_manager(self) -> SkillManager | None:
        """The skill manager. Used by pipeline for CommandContext."""
        return None

    @property
    def turn_store(self) -> TurnStateStore | None:
        """The turn store. Used by pipeline for CommandContext."""
        return None

    @property
    def hook_runner(self) -> HookRunner | None:
        """The hook runner. Used for post-construction hook injection."""
        return None

    @property
    def hooks(self) -> list[Any]:
        """List of hooks. Empty by default."""
        return []

    @property
    def sanitizer(self) -> Callable[[str], str] | None:
        """The sanitizer callable, if any."""
        return None

    @property
    def tool_manager(self) -> ToolManager | None:
        """The tool manager, if any."""
        return None

    @property
    def interceptor_chain(self) -> InterceptorChain | None:
        """The interceptor chain, if any."""
        return None

    @property
    def runtime_context_manager(self) -> RuntimeContextManager | None:
        """The runtime context manager, if any."""
        return None

    @property
    def turn_context_builder(self) -> TurnContextBuilder | None:
        """The TurnContextBuilder, if any (react only)."""
        return None

    @property
    def approval_renderer(self) -> ApprovalRenderer | None:
        """The approval renderer, if any (react only)."""
        return None


# TYPE_CHECKING-only forward reference for bind_to_pipeline's annotation.
if TYPE_CHECKING:
    from modex_agent.pipeline.pipeline import AgentPipeline  # noqa: F401
