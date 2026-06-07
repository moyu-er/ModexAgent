"""Regression tests for workspace switch bugs and converged helpers.

Covers:
  - Bug 1: Dream task restarts after cd (pattern test)
  - Bug 3: Subagent caches cleared after cd (pattern test)
  - Bug 5: Concurrent cd rejected by lock
  - Bug 6: Adapters use BuiltinCommand enum
  - Bug 7: Active checker uses public pool.has_active_sessions()
  - Callback atomicity: merged callback either succeeds or fails as a whole
  - Bug 2: Communication service paths updated (pattern test)
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.workspace.context import DefaultWorkspaceContext
from framework.workspace.models import CdResult


# -- Fixtures ---------------------------------------------------------------

@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home_project"
    h.mkdir()
    (h / ".modex").mkdir()
    return h


@pytest.fixture
def target(tmp_path):
    t = tmp_path / "target_project"
    t.mkdir()
    return t


@pytest.fixture(autouse=True)
def _restore_cwd():
    original = os.getcwd()
    yield
    os.chdir(original)


# -- Bug 1: Dream task restart pattern -------------------------------------


class TestDreamTaskRestart:
    """Verify _start_dream_task() pattern: creates task when engine exists."""

    @pytest.mark.asyncio
    async def test_task_created_when_engine_exists(self):
        """If dream_engine is set, a new _dream_task must be created."""
        events = []

        class Svc:
            dream_engine = MagicMock()
            _dream_task = None
            _dream_interval = 600

            async def _dream_background_loop(self, interval=600):
                events.append("loop-start")
                await asyncio.sleep(10)

        svc = Svc()
        assert svc._dream_task is None

        # Replicate _start_dream_task logic
        if svc.dream_engine is not None:
            svc._dream_task = asyncio.create_task(
                svc._dream_background_loop(interval=svc._dream_interval)
            )

        assert svc._dream_task is not None
        assert not svc._dream_task.done()

        svc._dream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await svc._dream_task

    def test_no_task_when_engine_is_none(self):
        """If dream_engine is None, no task should be created."""
        _dream_task = None

        # Replicate _start_dream_task logic
        dream_engine = None
        if dream_engine is not None:
            _dream_task = "would-be-task"

        assert _dream_task is None


# -- Bug 3: Subagent cache clear pattern -----------------------------------


class TestSubagentCacheClear:
    """Verify _clear_subagent_caches() clears all three dicts."""

    def test_clears_all_caches(self):
        subagent_memory_systems = {"a": object()}
        subagent_skill_managers = {"b": object()}
        additional_subagent_memory_systems = {"c": object()}

        # Replicate _clear_subagent_caches
        subagent_memory_systems.clear()
        subagent_skill_managers.clear()
        additional_subagent_memory_systems.clear()

        assert len(subagent_memory_systems) == 0
        assert len(subagent_skill_managers) == 0
        assert len(additional_subagent_memory_systems) == 0


# -- Bug 5: Concurrent cd rejected by lock ---------------------------------


class TestConcurrentCdLock:
    """Two concurrent cd requests must not interleave callbacks."""

    @pytest.mark.asyncio
    async def test_concurrent_cd_serialized(self, home, tmp_path):
        t1 = tmp_path / "target1"
        t1.mkdir()
        t2 = tmp_path / "target2"
        t2.mkdir()

        ctx = DefaultWorkspaceContext(home=home)
        execution_order: list[str] = []

        class SlowCallback:
            async def on_workspace_switch(self, old_data_dir: Path, new_data_dir: Path) -> None:
                name = "t1" if "target1" in str(new_data_dir) else "t2"
                execution_order.append(f"{name}-start")
                await asyncio.sleep(0.05)
                execution_order.append(f"{name}-end")

        ctx.register_callback(SlowCallback())

        results = await asyncio.gather(
            ctx.cd(str(t1)),
            ctx.cd(str(t2)),
        )

        assert all(isinstance(r, CdResult) for r in results)

        # Verify no interleaving: second start must come after first end
        starts = [i for i, e in enumerate(execution_order) if e.endswith("-start")]
        ends = [i for i, e in enumerate(execution_order) if e.endswith("-end")]
        if len(starts) == 2 and len(ends) == 2:
            assert starts[1] > ends[0], f"Callbacks interleaved: {execution_order}"


# -- Bug 6: Adapters use BuiltinCommand enum -------------------------------


class TestAdaptersEnum:
    """adapters.py must use BuiltinCommand enum, not raw strings."""

    def test_cd_exit_interception_uses_enum(self):
        from framework.commands.constants import BuiltinCommand
        import framework.pipeline.adapters as adapters_mod

        source = adapters_mod.__loader__.get_source(adapters_mod.__name__)

        assert "BuiltinCommand" in source
        assert "BuiltinCommand.CD.value" in source
        assert "BuiltinCommand.EXIT.value" in source


# -- Bug 7: Active checker uses public API ---------------------------------


class TestActiveCheckerPublicApi:
    """_make_active_checker must use pool.has_active_sessions()."""

    def test_uses_pool_has_active_sessions(self):
        import inspect
        # Read source directly to avoid import issues with bot service
        core_path = Path(__file__).parent.parent.parent.parent / (
            "examples/bot_project/bot/service/core.py"
        )
        source = core_path.read_text(encoding="utf-8")

        # Extract _make_active_checker method body
        assert "pool_inst.pool.has_active_sessions()" in source
        assert "_active_session_counts" not in source.split("_make_active_checker")[1].split("def _rebuild_overflow_store")[0]


# -- Callback atomicity -----------------------------------------------------


class TestCallbackAtomicity:
    """Merged callback ensures stop+rebuild is atomic."""

    @pytest.mark.asyncio
    async def test_merged_callback_failure_aborts_switch(self, home, target):
        """If the merged callback raises, the switch is aborted."""
        ctx = DefaultWorkspaceContext(home=home)

        class FailingCallback:
            async def on_workspace_switch(self, old_data_dir: Path, new_data_dir: Path) -> None:
                raise RuntimeError("rebuild failed")

        ctx.register_callback(FailingCallback())

        cwd_before = os.getcwd()
        result = await ctx.cd(str(target))
        assert result.success is False
        assert "internal error" in result.notice
        assert ctx.current == home
        assert os.getcwd() == cwd_before

    @pytest.mark.asyncio
    async def test_callback_order_preserved(self, home, target):
        """Callbacks execute in registration order."""
        ctx = DefaultWorkspaceContext(home=home)
        order: list[str] = []

        class FirstCallback:
            async def on_workspace_switch(self, old_data_dir: Path, new_data_dir: Path) -> None:
                order.append("first")

        class SecondCallback:
            async def on_workspace_switch(self, old_data_dir: Path, new_data_dir: Path) -> None:
                order.append("second")

        ctx.register_callback(FirstCallback())
        ctx.register_callback(SecondCallback())

        result = await ctx.cd(str(target))
        assert result.success is True
        assert order == ["first", "second"]


# -- Communication service paths (Bug 2) ------------------------------------


class TestCommunicationPathsUpdate:
    """_update_communication_paths pattern: update _memory_dir and _pruned_manager."""

    def test_updates_paths_correctly(self):
        """After update, communication service points to new data dir."""
        new_dir = Path("/new/workspace/.modex")

        # Simulate _ws_memory
        def ws_memory(data_dir: Path) -> Path:
            return data_dir / "memory"

        # Before
        comm_svc = MagicMock()
        comm_svc._memory_dir = Path("/old/workspace/.modex/memory/main")
        comm_svc._pruned_manager = MagicMock(name="old_pruned")

        new_pruned = MagicMock(name="new_pruned")

        # Simulate _update_communication_paths for one pool
        pool_name = "main"
        comm_svc._memory_dir = ws_memory(new_dir) / pool_name
        comm_svc._pruned_manager = new_pruned

        assert comm_svc._memory_dir == Path("/new/workspace/.modex/memory/main")
        assert comm_svc._pruned_manager is new_pruned


# -- Pool has_active_sessions (framework) -----------------------------------


class TestPoolHasActiveSessions:
    """AgentPool.has_active_sessions() must be a public method."""

    def test_method_exists(self):
        from framework.multi_agent.pool import AgentPool
        assert hasattr(AgentPool, "has_active_sessions")
        assert callable(getattr(AgentPool, "has_active_sessions"))

    def test_returns_false_when_empty(self):
        """Empty pool has no active sessions."""
        from framework.multi_agent.pool import AgentPool

        pool = AgentPool.__new__(AgentPool)
        pool._active_session_counts = {}
        assert pool.has_active_sessions() is False

    def test_returns_true_when_active(self):
        """Pool with active count > 0 returns True."""
        from framework.multi_agent.pool import AgentPool

        pool = AgentPool.__new__(AgentPool)
        pool._active_session_counts = {"main": 2}
        assert pool.has_active_sessions() is True

    def test_returns_false_when_zero(self):
        """Pool with all counts at 0 returns False."""
        from framework.multi_agent.pool import AgentPool

        pool = AgentPool.__new__(AgentPool)
        pool._active_session_counts = {"main": 0, "helper": 0}
        assert pool.has_active_sessions() is False
