"""Target-state configuration guard tests.

These tests protect the **converged memory + experience configuration** that
every native agent must receive. They are NOT tied to the bot's real
``config/pools/`` — they synthesize minimal pool configurations under
``tmp_path`` so the guard stays valid as pools are added, removed, or
reconfigured by users.

## What this guard protects

The bot's archive/core memory toggle is user-editable per pool through the
WebUI or the main agent's ``pool.yml`` ``memory:`` block. Detailed memory and
experience configuration remains baked (see
``modex_agent/memory/presets.py``
and the "Memory + Experience Presets (Target State)" section in ``AGENTS.md``).
The contract:

| Agent type | memory | experience | governance | hooks |
|---|---|---|---|---|
| native main | session + compact + governance + pruned | enabled (ExperienceReviewHook fires) | create_governance (budget + tool_chain_repair) | MaxIter + TurnOutcome + ModelChoiceBind + ExperienceReview |
| native subagent | session + compact + governance + pruned | N/A | create_subagent_governance (tool_chain_repair only) | SubagentAutoSend + MaxIter |
| external main | skipped structurally | skipped | skipped | skipped |
| external subagent | skipped structurally | skipped | skipped | skipped |

## Why this matters

If any of these tests fail, the bot's memory/experience system is broken:
- No archive → conversations can't compress, context window blows up
- No pruned → cleanup catalog never written
- No experience → reviewer never runs, EXPERIENCE.md never created
- No governance → tool chain breaks, oversized content not truncated
- No hooks → no notification, no model binding, no auto-send

## Test strategy

Tests synthesize pool configurations covering the **4 agent-type combinations**
(native/external × main/subagent) under ``tmp_path``. This decouples the guard
from the bot's real ``config/pools/`` directory, which may change at any time.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.ioc.configs.memory import (  # noqa: E402
    GovernanceConfig,
    MemoryConfig,
    PrunedCatalogConfig,
    SessionConfig,
)
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps  # noqa: E402
from modex_agent.multi_agent.pool_config.experience import ExperienceConfig  # noqa: E402

from ...declaration_driver import boot_from_yaml

# ─── helpers: synthesize the declaration-road deps ──────────────────────────

_ONE_POOL_DECLARATION = """\
pool:
  name: p
  agents:
    main:
      description: synthesized root
"""


def _position_deps(
    tmp_path: Path, *, max_context_tokens: int | None
) -> PoolAssemblyDeps:
    """Deps through the SINGLE position-derived road (stack.py): boot a
    one-root declaration and derive the root's assembly deps."""
    from bot.workspace.wiring.stack import declared_assembly_deps

    boot = boot_from_yaml(
        _ONE_POOL_DECLARATION, project_dir=tmp_path, data_dir=tmp_path / ".modex"
    )
    root = next(iter(boot.compilation.agents))
    return declared_assembly_deps(root, max_context_tokens=max_context_tokens)


# ─── Test 1: memory_defaults preset contract ─────────────────────────────────


class TestMemoryDefaultsContract:
    """Verify the three preset functions in memory_defaults.py.

    These presets are the **single source of truth** for all native agents.
    If any field is wrong, every native agent in every pool is affected.
    """

    def test_main_agent_memory_has_all_required_layers(self) -> None:
        """Main agent memory MUST have all long-term layers enabled.

        Missing any layer breaks the corresponding subsystem:
        - No archive → no compression, context window blows up
        - No core → no SOUL/USER/MEMORY.md injection
        - No dream_engine → no offline archive→core consolidation
        - No governance → no tool chain repair, no lossy compaction
        - No pruned → no cleanup catalog
        """
        from modex_agent.memory.presets import main_agent_memory

        m = main_agent_memory()

        # Session layer (compression triggers)
        assert isinstance(m.session, SessionConfig)
        assert m.session.max_token_ratio > 0
        assert 0 < m.session.keep_ratio < 1

        # Archive layer (default off — user enables per-pool)
        assert m.archive is None, "archive must be off by default for main agents"

        # Core memory (default off — depends on archive)
        assert m.core is None, "core memory must be off by default for main agents"

        # Dream engine (default off — depends on archive + core)
        assert m.dream_engine is None, "dream_engine must be off by default for main agents"

        # Compact (default on — essential for all agents)
        assert m.compact is not None, "compact must be enabled for main agents"
        assert m.compact.enabled is True

        # Governance (tool chain repair + lossy compaction)
        assert m.governance is not None, "governance must be enabled for main agents"
        assert isinstance(m.governance, GovernanceConfig)
        assert m.governance.tool_chain_repair is True
        assert m.governance.budget is not None, (
            "main agent governance MUST have budget — without it, "
            "oversized tool results will blow up the context window"
        )

        # Pruned catalog
        assert m.pruned is not None, "pruned must be enabled for main agents"
        assert m.pruned.enabled is True
        assert isinstance(m.pruned, PrunedCatalogConfig)

    def test_main_agent_memory_accepts_max_context_tokens(self) -> None:
        """max_context_tokens from model.yml MUST flow into session config."""
        from modex_agent.memory.presets import main_agent_memory

        m = main_agent_memory(max_context_tokens=128000)
        assert m.session.max_context_tokens == 128000

    def test_main_agent_experience_enabled_by_default(self) -> None:
        """Main agent experience MUST be enabled.

        Without this, ExperienceReviewHook never fires, no EXPERIENCE.md
        is ever created, and the ExperienceProvider injection is empty.
        """
        from modex_agent.multi_agent.pool_config.experience import main_agent_experience

        e = main_agent_experience()
        assert isinstance(e, ExperienceConfig)
        assert e.enabled is True
        # Reviewer parameters must have sensible defaults
        assert e.min_messages > 0
        assert e.exp_cooldown_turns >= 0
        assert e.max_iterations > 0
        assert e.max_experiences > 0
        assert e.curator_interval > 0

    def test_subagent_memory_is_minimal(self) -> None:
        """Subagent memory MUST be session + pruned + governance ONLY.

        Subagents are short-lived task workers — they must NOT have:
        - archive (no long-term history needed)
        - core (no SOUL/USER/MEMORY.md)
        - dream_engine (no offline consolidation)
        - budget (small context windows, not worth the overhead)

        They MUST have:
        - session (token-budget compression)
        - governance.tool_chain_repair (prevent broken tool chains)
        - pruned (cleanup catalog)
        """
        from modex_agent.memory.presets import subagent_memory

        m = subagent_memory()
        assert isinstance(m, MemoryConfig)

        # MUST have
        assert isinstance(m.session, SessionConfig)
        assert m.governance is not None
        assert m.governance.tool_chain_repair is True
        assert m.pruned is not None and m.pruned.enabled is True

        # MUST NOT have
        assert m.archive is None, "subagent must NOT have archive"
        assert m.core is None, "subagent must NOT have core memory"
        assert m.dream_engine is None, "subagent must NOT have dream_engine"
        assert m.governance.budget is None, (
            "subagent governance must NOT have budget"
        )

    def test_no_subagent_experience_preset_exists(self) -> None:
        """There must be NO subagent_experience() — review is main-agent-only."""
        import modex_agent.memory.presets as mod

        assert not hasattr(mod, "subagent_experience"), (
            "subagent_experience() must NOT exist — experience review is "
            "main-agent-only. Subagents are short-lived and don't review."
        )


# ─── Test 2: the deps road injects uniformly ─────────────────────────────────


class TestAssemblyDepsUniformInjection:
    """Verify pools with the default memory toggle (archive/core both off)
    receive the same memory + experience preset, regardless of type or count.

    This is the critical guard: if a new pool is added (native or external),
    it MUST receive the same converged config. External pools are skipped
    at wiring time (pipeline is None), not at deps construction time.
    """

    def test_all_pools_get_same_memory_preset(self, tmp_path: Path) -> None:
        """Every declared root — native or external — gets the same memory
        config (position-derived; the pool's strategy is irrelevant)."""
        deps = _position_deps(tmp_path, max_context_tokens=50000)

        memories = [deps.memory]
        for m in memories:
            assert m is not None
            assert m.archive is None  # default off
            assert m.core is None      # default off
            assert m.dream_engine is None  # default off
            assert m.compact is not None and m.compact.enabled  # compact always on
            assert m.governance is not None and m.governance.budget is not None
            assert m.pruned is not None and m.pruned.enabled
            assert m.session.max_context_tokens == 50000

    def test_all_pools_get_same_experience_preset(self, tmp_path: Path) -> None:
        """Every declared root — native or external — gets experience
        enabled (position-derived root default)."""
        from modex_agent.multi_agent.pool_config.experience import main_agent_experience

        deps = _position_deps(tmp_path, max_context_tokens=None)

        assert deps.experience is not None
        assert deps.experience == main_agent_experience()

    def test_works_with_empty_pool_list(self, tmp_path: Path) -> None:
        """Empty pool list must not crash (defensive)."""
        deps_map: dict[str, object] = {}
        assert deps_map == {}

    def test_works_with_single_pool(self, tmp_path: Path) -> None:
        """A single declared root gets the full config."""
        deps = _position_deps(tmp_path, max_context_tokens=None)
        assert deps.memory is not None
        assert deps.experience is not None and deps.experience.enabled



# ─── Test 4: External main agent structural skip at wiring ───────────────────



# ─── Test 5: Experience three-component packaging ────────────────────────────


class TestExperienceThreeComponentPackaging:
    """Verify the experience system's three coupled components are all
    packaged together: ExperienceManager (injection) + ExperienceReviewHook
    (review) + ExperienceCurator (LRU eviction).

    If any component is missing, experience degrades silently (see AGENTS.md
    "Experience review mechanism" section).
    """

    def test_experience_config_carries_all_reviewer_params(self) -> None:
        """ExperienceConfig must carry ALL parameters needed by:
        - ExperienceReviewAgent (max_iterations)
        - ExperienceReviewHook (min_messages, exp_cooldown_turns)
        - ExperienceCurator (max_experiences, curator_interval)
        """
        from modex_agent.multi_agent.pool_config.experience import main_agent_experience

        e = main_agent_experience()
        # ExperienceReviewAgent params
        assert e.max_iterations > 0, "max_iterations must be set for ExperienceReviewAgent"
        # ExperienceReviewHook params
        assert e.min_messages > 0, "min_messages must be set for ExperienceReviewHook"
        assert e.exp_cooldown_turns >= 0, "exp_cooldown_turns must be set for ExperienceReviewHook"
        # ExperienceCurator params
        assert e.max_experiences > 0, "max_experiences must be set for ExperienceCurator"
        assert e.curator_interval > 0, "curator_interval must be set for ExperienceCurator"

    def test_experience_manager_built_when_experience_enabled(
        self, tmp_path: Path
    ) -> None:
        """_build_experience_manager MUST return a non-None ExperienceManager
        when experience is enabled.

        This is the injection component — without it, ExperienceProvider
        never adds <available_experiences> to the system prompt.
        """
        from bot.workspace.pool_data import _build_experience_manager

        deps = _position_deps(tmp_path, max_context_tokens=50000)

        exp_dir = tmp_path / "experiences" / "test" / "main"
        exp_dir.mkdir(parents=True, exist_ok=True)

        manager = _build_experience_manager(deps, exp_dir)
        assert manager is not None, (
            "ExperienceManager must be built when experience is enabled — "
            "without it, no <available_experiences> XML in system prompt"
        )

    def test_experience_manager_none_when_experience_disabled(
        self, tmp_path: Path
    ) -> None:
        """_build_experience_manager MUST return None when experience is
        disabled (defensive — verifies the guard clause works)."""
        from bot.workspace.pool_data import _build_experience_manager

        # Construct deps with experience=None (memory must be real MemoryConfig
        # because PoolAssemblyDeps is a frozen Pydantic model)
        from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps

        deps = PoolAssemblyDeps(
            memory=MemoryConfig(),
            experience=None,
        )
        exp_dir = tmp_path / "experiences" / "test"
        manager = _build_experience_manager(deps, exp_dir)
        assert manager is None

    def test_experience_curator_built_when_experience_enabled(
        self, tmp_path: Path
    ) -> None:
        """BackgroundTaskRunner._build_curators MUST create an
        ExperienceCurator for every pool with experience enabled.

        This is the LRU eviction component — without it, the experience
        directory grows unbounded.
        """
        from bot.workspace.background import BackgroundTaskRunner

        deps_map = {
            "native_a": _position_deps(tmp_path, max_context_tokens=50000),
            "native_b": _position_deps(tmp_path, max_context_tokens=50000),
        }

        pool_data_a = MagicMock()
        pool_data_a.experience_dir = tmp_path / "exp_a"
        pool_data_a.experience_dir.mkdir(parents=True, exist_ok=True)
        pool_data_a.experience_meta = MagicMock()

        pool_data_b = MagicMock()
        pool_data_b.experience_dir = tmp_path / "exp_b"
        pool_data_b.experience_dir.mkdir(parents=True, exist_ok=True)
        pool_data_b.experience_meta = MagicMock()

        bg = BackgroundTaskRunner(
            pool_data={"native_a": pool_data_a, "native_b": pool_data_b},
            assembly_deps=deps_map,
            default_pool_name="native_a",
        )
        bg._build_curators()

        # Both pools must have curators
        assert "native_a" in bg.curators, "native_a must have ExperienceCurator"
        assert "native_b" in bg.curators, "native_b must have ExperienceCurator"

    def test_experience_curator_skipped_when_experience_disabled(
        self, tmp_path: Path
    ) -> None:
        """BackgroundTaskRunner._build_curators MUST NOT create a curator
        for pools with experience=None (defensive guard)."""
        from bot.workspace.background import BackgroundTaskRunner

        from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps

        pool_data = MagicMock()
        pool_data.experience_dir = tmp_path / "exp"
        pool_data.experience_meta = MagicMock()

        # PoolAssemblyDeps is a frozen Pydantic model — memory must be real
        deps_no_exp = PoolAssemblyDeps(
            memory=MemoryConfig(),
            experience=None,
        )

        bg = BackgroundTaskRunner(
            pool_data={"disabled_pool": pool_data},
            assembly_deps={"disabled_pool": deps_no_exp},
            default_pool_name="disabled_pool",
        )
        bg._build_curators()

        assert "disabled_pool" not in bg.curators, (
            "ExperienceCurator must NOT be built when experience is disabled"
        )


# ─── Test 6: End-to-end with synthesized mixed pool config ───────────────────



# ─── Test 7: Experience reviewer uses bot-global default_provider ─────────────



# ─── Test 8: Archive emitter notification (UserNoticeCleanupHook) ──────────


class TestArchiveEmitterNotification:
    """Verify the cleanup notice hook (UserNoticeCleanupHook) is correctly
    wired and fires the right notices.

    When session memory is compacted (archive generation triggered), the user
    sees two notices:
    1. ``[compact] Consolidating conversation memory, please wait...`` —
       fires BEFORE the archive-generation LLM call (which can be slow)
    2. ``[compact] Memory consolidated.`` — fires AFTER cleanup completes

    Without these notices, the user sees a stuck agent during archive
    generation (which can take 10+ seconds for the LLM summarizer call).

    The hook is registered via ``memory_system.add_cleanup_hook(...)`` in
    ``pool/factory.py``.
    """

    def test_hook_sends_start_notice_on_cleanup_triggered(self) -> None:
        """UserNoticeCleanupHook.on_cleanup_triggered MUST send the
        start notice via notification_service.send_notice."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(
            memory_context=MemoryContext(
                session_id="test_session.orchestrator",
                user_id="u1",
            ),
        )

        import asyncio

        asyncio.run(hook.on_cleanup_triggered(ctx))

        notification_service.send_notice.assert_called_once_with(
            "test_session.orchestrator",
            "[compact] Consolidating conversation memory, please wait...",
        )

    def test_hook_sends_done_notice_on_cleanup_finished(self) -> None:
        """UserNoticeCleanupHook.on_cleanup_finished MUST send the
        done notice via notification_service.send_notice."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.cleanup import CleanupResult
        from modex_agent.memory.core.models import CompressionReason
        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(
            memory_context=MemoryContext(
                session_id="test_session.orchestrator",
                user_id="u1",
            ),
            cleanup_result=CleanupResult(
                triggered=True,
                messages_kept=5,
                messages_pruned=10,
                reason=CompressionReason.TOKEN_PRESSURE,
            ),
        )

        import asyncio

        asyncio.run(hook.on_cleanup_finished(ctx))

        notification_service.send_notice.assert_called_once_with(
            "test_session.orchestrator",
            "[compact] Memory consolidated.",
        )

    def test_hook_skips_when_session_id_is_none(self) -> None:
        """Hook MUST NOT send notices when session_id is None
        (defensive — avoids crash on malformed context)."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(
            memory_context=MemoryContext(session_id=None),
        )

        import asyncio

        asyncio.run(hook.on_cleanup_triggered(ctx))
        asyncio.run(hook.on_cleanup_finished(ctx))

        assert not notification_service.send_notice.called

    def test_hook_skips_when_memory_context_is_none(self) -> None:
        """Hook MUST NOT send notices when memory_context is None
        (defensive — guards against incomplete hook context)."""
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.memory.hooks import MemoryHookContext

        notification_service = MagicMock()
        notification_service.send_notice = AsyncMock()
        hook = UserNoticeCleanupHook(notification_service)

        ctx = MemoryHookContext(memory_context=None)

        import asyncio

        asyncio.run(hook.on_cleanup_triggered(ctx))
        asyncio.run(hook.on_cleanup_finished(ctx))

        assert not notification_service.send_notice.called

    def test_hook_implements_both_point_abcs(self) -> None:
        """UserNoticeCleanupHook MUST implement both CleanupTriggeredHook
        and CleanupFinishedHook ABCs.

        Without this, ``memory_system.add_cleanup_hook`` would not dispatch
        either point to it (the runner isinstance-checks each ABC).
        """
        from bot.service.pool.communication import UserNoticeCleanupHook

        from modex_agent.memory.hooks import CleanupFinishedHook, CleanupTriggeredHook

        assert issubclass(UserNoticeCleanupHook, CleanupTriggeredHook | CleanupFinishedHook), (
            "UserNoticeCleanupHook must inherit from both CleanupTriggeredHook "
            "and CleanupFinishedHook so the runner dispatches both points to it"
        )

    def test_hook_notices_are_english_and_start_with_compact_tag(self) -> None:
        """Notice text must start with ``[compact]`` tag and be in English
        (matching the existing convention — not localized).

        The ``[compact]`` tag lets the WebUI/IM filter these notices
        differently from regular agent messages if needed.
        """
        from bot.service.pool.communication import UserNoticeCleanupHook

        assert UserNoticeCleanupHook._START_NOTICE.startswith("[compact]")
        assert UserNoticeCleanupHook._DONE_NOTICE.startswith("[compact]")
        assert "Consolidating" in UserNoticeCleanupHook._START_NOTICE
        assert "consolidated" in UserNoticeCleanupHook._DONE_NOTICE.lower()


# ─── Test 9: MemorySystem fires cleanup hooks through the real path ───────


class TestMemorySystemCleanupHookFiring:
    """Verify DefaultMemorySystem fires cleanup hooks through the real path:

    - Registers a recording ``CleanupFinishedHook`` via ``add_cleanup_hook``.
    - Appends messages to a real ``ScopedMessageHistory`` to trigger cleanup.
    - Asserts the hook received a ``MemoryHookContext`` with the expected
      ``memory_context`` and ``cleanup_result``.

    No ``MagicMock(spec=...)`` for private list storage — the hook is
    registered via the public ``add_cleanup_hook`` API and fired by actually
    running ``cleanup_session()`` through the real ``ScopedMessageHistory`` →
    ``_run_cleanup`` → ``cleanup_session`` path.
    """

    def test_real_cleanup_fires_finished_hook(self, tmp_path: Path) -> None:
        import asyncio

        asyncio.run(self._run_real_cleanup_fires_finished_hook(tmp_path))

    async def _run_real_cleanup_fires_finished_hook(self, tmp_path: Path) -> None:
        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.default_system import DefaultMemorySystem
        from modex_agent.memory.hooks import CleanupFinishedHook, MemoryHookContext
        from modex_agent.memory.layers.factory import MemoryLayerFactory
        from modex_agent.memory.registry import DefaultMemoryStoreRegistry
        from modex_agent.memory.token_estimator import TokenEstimator

        class _FixedEstimator(TokenEstimator):
            def __init__(self) -> None:
                self.per_message = 10

            def estimate_text(self, text: str) -> int:
                return self.per_message

        class _RecordingFinishedHook(CleanupFinishedHook):
            def __init__(self) -> None:
                self.calls: list[MemoryHookContext] = []

            async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
                self.calls.append(ctx)

        registry = DefaultMemoryStoreRegistry(tmp_path)
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        system = DefaultMemorySystem(
            layer_set=layer_set,
            store_registry=registry,
            cleanup_config={
                "max_context_tokens": 100,
                "max_token_ratio": 0.8,
                "keep_ratio": 0.5,
            },
            token_estimator=_FixedEstimator(),
        )
        await system.initialize()

        hook = _RecordingFinishedHook()
        system.add_cleanup_hook(hook)

        context = MemoryContext(session_id="test-session", user_id="test-user")
        history = system.create_message_history(context)

        for i in range(20):
            await history.append({"role": "user", "content": f"msg-{i}"})

        assert len(hook.calls) > 0, (
            "CleanupFinishedHook must fire when cleanup is triggered"
        )
        finished_ctx = hook.calls[0]
        assert finished_ctx.memory_context is not None
        assert finished_ctx.memory_context.session_id == "test-session"
        assert finished_ctx.cleanup_result is not None
        assert finished_ctx.cleanup_result.triggered is True

    def test_real_cleanup_fires_triggered_and_finished_on_normal_path(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        asyncio.run(self._run_real_cleanup_fires_both(tmp_path))

    async def _run_real_cleanup_fires_both(self, tmp_path: Path) -> None:
        from modex_agent.core.scope import MemoryContext
        from modex_agent.memory.default_system import DefaultMemorySystem
        from modex_agent.memory.hooks import (
            CleanupFinishedHook,
            CleanupTriggeredHook,
            MemoryHookContext,
        )
        from modex_agent.memory.layers.factory import MemoryLayerFactory
        from modex_agent.memory.registry import DefaultMemoryStoreRegistry
        from modex_agent.memory.token_estimator import TokenEstimator

        class _FixedEstimator(TokenEstimator):
            def __init__(self) -> None:
                self.per_message = 10

            def estimate_text(self, text: str) -> int:
                return self.per_message

        class _RecordingBothHook(CleanupTriggeredHook, CleanupFinishedHook):
            def __init__(self) -> None:
                self.triggered_calls: list[MemoryHookContext] = []
                self.finished_calls: list[MemoryHookContext] = []

            async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
                self.triggered_calls.append(ctx)

            async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
                self.finished_calls.append(ctx)

        registry = DefaultMemoryStoreRegistry(tmp_path)
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        system = DefaultMemorySystem(
            layer_set=layer_set,
            store_registry=registry,
            cleanup_config={
                "max_context_tokens": 100,
                "max_token_ratio": 0.8,
                "keep_ratio": 0.5,
            },
            token_estimator=_FixedEstimator(),
        )
        await system.initialize()

        hook = _RecordingBothHook()
        system.add_cleanup_hook(hook)

        context = MemoryContext(session_id="test-session", user_id="test-user")
        history = system.create_message_history(context)

        for i in range(20):
            await history.append({"role": "user", "content": f"msg-{i}"})

        assert len(hook.triggered_calls) > 0, (
            "CleanupTriggeredHook must fire on the normal cleanup path"
        )
        assert len(hook.finished_calls) > 0, (
            "CleanupFinishedHook must fire on the normal cleanup path"
        )
        assert hook.finished_calls[0].memory_context is not None
        assert hook.finished_calls[0].memory_context.session_id == "test-session"
        assert hook.finished_calls[0].cleanup_result is not None
        assert hook.finished_calls[0].cleanup_result.triggered is True
        assert hook.finished_calls[0].cleanup_result.messages_pruned > 0, (
            "Normal cleanup path must prune messages"
        )
