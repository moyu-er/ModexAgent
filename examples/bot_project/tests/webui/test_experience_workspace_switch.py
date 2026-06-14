"""Tests for experience workspace switching.

Covers all four subsystems that must follow workspace changes:

1. **ExperienceReviewHook** — ``_get_dir()`` returns the current workspace's experience dir
2. **ExperienceCurator** — ``_get_dir()`` returns the current workspace's experience dir
3. **PerFileExperienceMetaStore** — ``_root`` resolves to the current workspace
4. **Injection-side ExperienceManager** — rebuilt on ``context_manager`` after switch

Each test verifies both directions: that the old path was correct, and that the
new path takes effect after the switch — proving the mutable-ref pattern works
for all objects simultaneously.

Design note: all three objects accept ``Path | Callable[[], Path]`` in their
constructors.  The bot wires them with ``lambda: _dir_ref[0]`` so updating
``_dir_ref[0]`` on workspace switch propagates everywhere without rebuilding.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.core.provider import LLMProvider


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


def _make_enabled_agent_cfg() -> MagicMock:
    """Return an AgentConfig mock with experience.enabled=True."""
    exp_mock = MagicMock()
    exp_mock.enabled = True
    exp_mock.min_messages = 6
    exp_mock.exp_cooldown_turns = 3
    exp_mock.max_iterations = 50
    exp_mock.max_experiences = 20
    exp_mock.curator_interval = 3600
    agent_mock = MagicMock()
    agent_mock.name = "testagent"
    agent_mock.role = "main"
    agent_mock.max_steps = 10
    agent_mock.experience = exp_mock
    return agent_mock


# ═══════════════════════════════════════════════════════════════════════════
# _build_experience_layer — dir_ref mechanism
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildExperienceLayerDirRef:
    """Verify ``_build_experience_layer`` returns a mutable dir_ref shared by all
    three objects (hook, curator, meta_store)."""

    def test_returns_dir_ref_when_enabled(self) -> None:
        """When experience is enabled, the third return value is a list[Path]."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = _make_enabled_agent_cfg()
        provider = MagicMock(spec=LLMProvider)
        data_dir = Path("/fake/data")

        hook, curator, dir_ref = _build_experience_layer(
            agent_mock, provider, data_dir, "testpool",
        )

        assert hook is not None, "Expected a review hook when experience is enabled"
        assert curator is not None, "Expected a curator when experience is enabled"
        assert dir_ref is not None, "Expected a mutable dir_ref"
        assert isinstance(dir_ref, list), f"dir_ref should be list, got {type(dir_ref)}"
        assert len(dir_ref) == 1, f"dir_ref should have 1 element, got {len(dir_ref)}"
        assert isinstance(dir_ref[0], Path), f"dir_ref[0] should be Path, got {type(dir_ref[0])}"

    def test_returns_none_triple_when_disabled(self) -> None:
        """When experience is disabled, all three return values are None."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = MagicMock()
        agent_mock.name = "testagent"
        agent_mock.role = "main"
        agent_mock.max_steps = 10
        exp_mock = MagicMock()
        exp_mock.enabled = False
        agent_mock.experience = exp_mock

        provider = MagicMock(spec=LLMProvider)
        data_dir = Path("/fake/data")

        hook, curator, dir_ref = _build_experience_layer(
            agent_mock, provider, data_dir, "testpool",
        )

        assert hook is None
        assert curator is None
        assert dir_ref is None

    def test_returns_none_triple_when_no_experience_config(self) -> None:
        """When agent config has no experience attribute, returns None triple."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = MagicMock()
        agent_mock.name = "testagent"
        agent_mock.role = "main"
        agent_mock.max_steps = 10
        # No .experience attribute → getattr returns None
        del agent_mock.experience

        provider = MagicMock(spec=LLMProvider)
        data_dir = Path("/fake/data")

        hook, curator, dir_ref = _build_experience_layer(
            agent_mock, provider, data_dir, "testpool",
        )

        assert hook is None
        assert curator is None
        assert dir_ref is None


class TestDirRefSharedByAllObjects:
    """Verify the same mutable ref is used by hook, curator, and meta_store."""

    def test_all_three_objects_resolve_through_same_ref(self, tmp_path: Path) -> None:
        """Hook._get_dir(), Curator._get_dir(), MetaStore._root all equal ref[0]."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = _make_enabled_agent_cfg()
        provider = MagicMock(spec=LLMProvider)
        data_dir = tmp_path / "data"

        hook, curator, dir_ref = _build_experience_layer(
            agent_mock, provider, data_dir, "testpool",
        )

        assert dir_ref is not None
        expected = dir_ref[0]

        # All three MUST resolve to the same directory.
        assert hook._get_dir() == expected, (
            f"Hook dir mismatch: {hook._get_dir()} != {expected}"
        )
        assert curator._get_dir() == expected, (
            f"Curator dir mismatch: {curator._get_dir()} != {expected}"
        )
        # Meta store is internal to hook; expose via the hook's _meta_store.
        assert hook._meta_store._root == expected.resolve(), (
            f"MetaStore root mismatch: {hook._meta_store._root} != {expected.resolve()}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Workspace switch simulation — mutating dir_ref[0]
# ═══════════════════════════════════════════════════════════════════════════


class TestExperienceDirRefMutationSwitchesAllSubsystems:
    """Updating ``dir_ref[0]`` must switch hook, curator, and meta_store
    simultaneously — no object recreation needed."""

    def test_mutate_dir_ref_switches_hook_dir(self, tmp_path: Path) -> None:
        """After ``dir_ref[0] = new_dir``, hook._get_dir() returns new_dir."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = _make_enabled_agent_cfg()
        provider = MagicMock(spec=LLMProvider)
        old_dir = tmp_path / "data"
        new_dir = tmp_path / "other"

        hook, _curator, dir_ref = _build_experience_layer(
            agent_mock, provider, old_dir, "testpool",
        )

        assert dir_ref is not None

        # Before switch: points to old.
        assert hook._get_dir() == dir_ref[0]
        assert dir_ref[0].parts[:2] == old_dir.parts[:2]

        # Simulate workspace switch.
        new_exp_dir = new_dir / "experiences" / "testpool" / "testagent"
        dir_ref[0] = new_exp_dir

        # After switch: hook resolves to new.
        assert hook._get_dir() == new_exp_dir, (
            f"Hook should resolve to {new_exp_dir}, got {hook._get_dir()}"
        )

    def test_mutate_dir_ref_switches_curator_dir(self, tmp_path: Path) -> None:
        """After ``dir_ref[0] = new_dir``, curator._get_dir() returns new_dir."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = _make_enabled_agent_cfg()
        provider = MagicMock(spec=LLMProvider)
        old_dir = tmp_path / "data"
        new_dir = tmp_path / "other"

        _hook, curator, dir_ref = _build_experience_layer(
            agent_mock, provider, old_dir, "testpool",
        )

        assert dir_ref is not None
        old_exp_dir = dir_ref[0]

        # Simulate workspace switch.
        new_exp_dir = new_dir / "experiences" / "testpool" / "testagent"
        dir_ref[0] = new_exp_dir

        assert curator._get_dir() == new_exp_dir, (
            f"Curator should resolve to {new_exp_dir}, got {curator._get_dir()}"
        )
        # Sanity: old value no longer returned.
        assert curator._get_dir() != old_exp_dir

    def test_mutate_dir_ref_switches_meta_store(self, tmp_path: Path) -> None:
        """After ``dir_ref[0] = new_dir``, meta_store._root resolves to new_dir."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = _make_enabled_agent_cfg()
        provider = MagicMock(spec=LLMProvider)
        old_dir = tmp_path / "data"
        new_dir = tmp_path / "other"

        hook, _curator, dir_ref = _build_experience_layer(
            agent_mock, provider, old_dir, "testpool",
        )

        assert dir_ref is not None

        # Simulate workspace switch.
        new_exp_dir = new_dir / "experiences" / "testpool" / "testagent"
        dir_ref[0] = new_exp_dir

        # Meta store uses _root property which calls _get_root().resolve()
        assert hook._meta_store._root == new_exp_dir.resolve(), (
            f"MetaStore root should be {new_exp_dir.resolve()}, "
            f"got {hook._meta_store._root}"
        )

    @pytest.mark.asyncio
    async def test_hook_review_uses_switched_dir(self, tmp_path: Path) -> None:
        """After dir_ref mutation, _build_existing_experiences_xml reads from
        the new directory, not the old one."""
        from bot.service.pool_builder import _build_experience_layer

        agent_mock = _make_enabled_agent_cfg()
        provider = MagicMock(spec=LLMProvider)

        # Create two workspaces, each with an experience.
        ws_a_exp = tmp_path / "ws-a" / "experiences" / "testpool" / "testagent"
        ws_b_exp = tmp_path / "ws-b" / "experiences" / "testpool" / "testagent"

        ws_a_exp.mkdir(parents=True)
        ws_b_exp.mkdir(parents=True)

        # Write an EXPERIENCE.md in ws-b so we can verify the hook reads from it.
        (ws_b_exp / "some-skill").mkdir(parents=True, exist_ok=True)
        (ws_b_exp / "some-skill" / "EXPERIENCE.md").write_text(
            "---\nname: some-skill\ndescription: desc\nscenario: test\n---\n# Body\n",
            encoding="utf-8",
        )

        hook, _curator, dir_ref = _build_experience_layer(
            agent_mock, provider, ws_a_exp.parent.parent, "testpool",
        )

        assert dir_ref is not None

        # Before switch: hook sees ws-a (empty — no experiences).
        xml_before = await hook._build_existing_experiences_xml()  # type: ignore[call-arg]
        assert "some-skill" not in xml_before, (
            f"ws-a should have no experiences, got: {xml_before!r}"
        )

        # Switch to ws-b.
        dir_ref[0] = ws_b_exp

        # After switch: hook sees ws-b's experiences.
        xml_after = await hook._build_existing_experiences_xml()  # type: ignore[call-arg]
        assert "some-skill" in xml_after, (
            f"ws-b should have 'some-skill' experience, got: {xml_after!r}"
        )

    @pytest.mark.asyncio
    async def test_curator_eviction_uses_switched_dir(self, tmp_path: Path) -> None:
        """After dir_ref mutation, curator.run() evicts from the new directory."""
        from bot.service.pool_builder import _build_experience_layer
        from framework.core.experience.meta import ExperienceMetaRecord

        agent_mock = _make_enabled_agent_cfg()
        agent_mock.experience.max_experiences = 1  # keep only 1
        provider = MagicMock(spec=LLMProvider)

        ws_a_exp = tmp_path / "ws-a" / "experiences" / "testpool" / "testagent"
        ws_b_exp = tmp_path / "ws-b" / "experiences" / "testpool" / "testagent"
        ws_a_exp.mkdir(parents=True)
        ws_b_exp.mkdir(parents=True)

        hook, curator, dir_ref = _build_experience_layer(
            agent_mock, provider, ws_a_exp.parent.parent, "testpool",
        )

        assert dir_ref is not None

        # Create 2 experiences with meta records in ws-a.
        for name in ("skill-a", "skill-b"):
            (ws_a_exp / name).mkdir(parents=True, exist_ok=True)
            hook._meta_store.set(name, ExperienceMetaRecord(use_count=1))

        assert len(hook._meta_store.list_all()) == 2

        # Run curator on ws-a: should evict down to 1.
        await curator.run()

        remaining_a = hook._meta_store.list_all()
        assert len(remaining_a) <= 1, (
            f"Curator on ws-a should evict excess, got {len(remaining_a)}: {remaining_a}"
        )

        # Switch to ws-b.
        dir_ref[0] = ws_b_exp

        # Create 2 experiences in ws-b.
        for name in ("skill-c", "skill-d"):
            (ws_b_exp / name).mkdir(parents=True, exist_ok=True)
            hook._meta_store.set(name, ExperienceMetaRecord(use_count=1))

        assert len(hook._meta_store.list_all()) == 2

        # Run curator on ws-b: should evict down to 1.
        await curator.run()

        remaining_b = hook._meta_store.list_all()
        assert len(remaining_b) <= 1, (
            f"Curator on ws-b should evict excess, got {len(remaining_b)}: {remaining_b}"
        )

        # Switch back to ws-a — old record still evicted to 1.
        dir_ref[0] = ws_a_exp
        remaining_a2 = hook._meta_store.list_all()
        assert len(remaining_a2) <= 1, (
            f"Curator on ws-a (round 2) should still have <=1, got {len(remaining_a2)}: {remaining_a2}"
        )

    def test_meta_store_read_write_switches_with_dir_ref(self, tmp_path: Path) -> None:
        """After dir_ref mutation, meta_store reads/writes from the new directory."""
        from bot.service.pool_builder import _build_experience_layer
        from framework.core.experience.meta import ExperienceMetaRecord

        agent_mock = _make_enabled_agent_cfg()
        provider = MagicMock(spec=LLMProvider)

        ws_a_exp = tmp_path / "ws-a" / "experiences" / "testpool" / "testagent"
        ws_b_exp = tmp_path / "ws-b" / "experiences" / "testpool" / "testagent"

        hook, _curator, dir_ref = _build_experience_layer(
            agent_mock, provider, tmp_path / "ws-a", "testpool",
        )

        assert dir_ref is not None
        meta = hook._meta_store

        # Write to ws-a.
        record = ExperienceMetaRecord(use_count=1)
        meta.set("test-exp", record)
        # Verify via API (not filesystem, which may differ on Windows).
        loaded = meta.get("test-exp")
        assert loaded is not None, "Meta store should return what was just set"
        assert loaded.use_count == 1
        all_a = meta.list_all()
        assert "test-exp" in all_a

        # Switch to ws-b (fresh — should have no records).
        dir_ref[0] = ws_b_exp
        ws_b_exp.mkdir(parents=True, exist_ok=True)

        # ws-b should NOT have ws-a's record.
        assert meta.get("test-exp") is None, (
            "Meta store should read from ws-b, not ws-a"
        )
        assert meta.list_all() == {}

        # Write to ws-b.
        meta.set("wsb-exp", ExperienceMetaRecord(use_count=2))
        all_b = meta.list_all()
        assert "wsb-exp" in all_b, f"ws-b should have 'wsb-exp', got: {all_b}"

        # Switch back to ws-a — old record still there.
        dir_ref[0] = ws_a_exp
        record_a = meta.get("test-exp")
        assert record_a is not None
        assert record_a.use_count == 1
        # ws-b record NOT visible from ws-a.
        assert meta.get("wsb-exp") is None


# ═══════════════════════════════════════════════════════════════════════════
# _rebuild_experience — BotService integration
# ═══════════════════════════════════════════════════════════════════════════


class TestRebuildExperienceUpdatesPoolInstances:
    """Verify ``_rebuild_experience`` updates ``experience_dir_ref`` on every
    pool instance and rebuilds the injection-side ``ExperienceManager``."""

    def test_rebuild_updates_dir_ref_on_pool_with_experience(self, tmp_path: Path) -> None:
        """Pool with experience_dir_ref gets its ref[0] updated to new path."""
        from bot.service.core import BotService, _find_main_agent_name

        # Build a minimal service.
        service = object.__new__(BotService)
        # Bypass __init__ — set only what _rebuild_experience needs.
        service._pools = {}

        # Create a fake pool instance with an experience dir ref.
        pool_mock = MagicMock()
        pool_mock.name = "testpool"
        pool_mock.experience_dir_ref = None
        pool_mock.config = MagicMock()
        pool_mock.config.agents = [_make_enabled_agent_cfg()]
        # context_manager._experience_manager is a declared attribute on MemorySystemContextManager
        pool_mock.context_manager = MagicMock()
        pool_mock.context_manager._experience_manager = MagicMock()

        service._pools["testpool"] = pool_mock

        # Pool WITHOUT experience (dir_ref is None) — should be skipped.
        pool_no_exp = MagicMock()
        pool_no_exp.name = "nopool"
        pool_no_exp.experience_dir_ref = None
        pool_no_exp.config = MagicMock()
        pool_no_exp.config.agents = [_make_enabled_agent_cfg()]
        pool_no_exp.context_manager = MagicMock()
        pool_no_exp.context_manager._experience_manager = MagicMock()
        service._pools["nopool"] = pool_no_exp

        # Give testpool a valid dir_ref pointing to old workspace.
        old_exp_dir = tmp_path / "old" / "experiences" / "testpool" / "testagent"
        old_exp_dir.mkdir(parents=True)
        dir_ref = [old_exp_dir]
        pool_mock.experience_dir_ref = dir_ref

        import asyncio

        new_data_dir = tmp_path / "new"
        asyncio.run(service._rebuild_experience(new_data_dir))

        # Verify dir_ref updated.
        new_exp_dir = BotService._ws_experience(new_data_dir, pool_name="testpool", agent_name="testagent")
        assert dir_ref[0] == new_exp_dir, (
            f"dir_ref[0] should be {new_exp_dir}, got {dir_ref[0]}"
        )

        # Verify injection-side ExperienceManager rebuilt.
        assert pool_mock.context_manager._experience_manager is not None

        # Verify pool without exp dir_ref was NOT touched.
        assert pool_no_exp.experience_dir_ref is None

    def test_rebuild_skips_pool_without_experience_manager(
        self, tmp_path: Path,
    ) -> None:
        """Pool whose context_manager._experience_manager is None is skipped
        for injection rebuild, but dir_ref update still runs."""
        from bot.service.core import BotService

        service = object.__new__(BotService)
        service._pools = {}

        # Pool WITH a None experience_manager on context_manager — should be skipped.
        pool_mock = MagicMock()
        pool_mock.name = "testpool"
        old_exp_dir = tmp_path / "old" / "experiences" / "testpool" / "testagent"
        old_exp_dir.mkdir(parents=True)
        pool_mock.experience_dir_ref = [old_exp_dir]
        pool_mock.config = MagicMock()
        pool_mock.config.agents = [_make_enabled_agent_cfg()]
        pool_mock.context_manager = MagicMock()
        pool_mock.context_manager._experience_manager = None  # explicitly None

        service._pools["testpool"] = pool_mock

        import asyncio

        new_data_dir = tmp_path / "new"
        asyncio.run(service._rebuild_experience(new_data_dir))

        # dir_ref must still be updated even without an injection-side manager.
        new_exp_dir = BotService._ws_experience(new_data_dir, pool_name="testpool", agent_name="testagent")
        assert pool_mock.experience_dir_ref[0] == new_exp_dir, (
            "dir_ref must be updated even without injection-side manager"
        )
        # context_manager._experience_manager must remain None (was skipped).
        assert pool_mock.context_manager._experience_manager is None

    def test_rebuild_handles_multiple_pools(self, tmp_path: Path) -> None:
        """_rebuild_experience updates dir_ref on all pools that have one."""
        from bot.service.core import BotService

        service = object.__new__(BotService)
        service._pools = {}

        for pname in ("pool-a", "pool-b"):
            old_exp_dir = tmp_path / "old" / "experiences" / pname / "testagent"
            old_exp_dir.mkdir(parents=True)
            pool_mock = MagicMock()
            pool_mock.name = pname
            pool_mock.experience_dir_ref = [old_exp_dir]
            pool_mock.config = MagicMock()
            pool_mock.config.agents = [_make_enabled_agent_cfg()]
            pool_mock.context_manager = MagicMock()
            service._pools[pname] = pool_mock

        import asyncio

        new_data_dir = tmp_path / "new"
        asyncio.run(service._rebuild_experience(new_data_dir))

        for pname in ("pool-a", "pool-b"):
            pool_mock = service._pools[pname]
            new_exp_dir = BotService._ws_experience(
                new_data_dir, pool_name=pname, agent_name="testagent",
            )
            assert pool_mock.experience_dir_ref[0] == new_exp_dir, (
                f"Pool '{pname}' dir_ref not updated"
            )


# ═══════════════════════════════════════════════════════════════════════════
# PoolInstance integration
# ═══════════════════════════════════════════════════════════════════════════


class TestPoolInstanceCarriesDirRef:
    """``PoolInstance.experience_dir_ref`` stores the mutable ref so
    ``_rebuild_experience`` can update it."""

    def test_pool_instance_has_experience_dir_ref_field(self) -> None:
        """PoolInstance dataclass must declare experience_dir_ref."""
        from bot.service.pool_instance import PoolInstance

        # Instantiate with minimal required fields.
        pi = PoolInstance(
            name="test",
            config=MagicMock(),
            pool=MagicMock(),
            broker_bridge=MagicMock(),
            memory_system=MagicMock(),
            context_manager=MagicMock(),
            tool_manager=MagicMock(),
            skill_manager=None,
            mcp_manager=None,
            terminal_manager=None,
            main_agent_name="main",
            provider=MagicMock(),
            notification_service=MagicMock(),
            communication_service=MagicMock(),
        )

        assert hasattr(pi, "experience_dir_ref"), (
            "PoolInstance must have experience_dir_ref field"
        )
        # Default is None.
        assert pi.experience_dir_ref is None

    def test_create_pool_stores_dir_ref_on_pool_instance(self, tmp_path: Path) -> None:
        """create_pool() must store the dir_ref from _build_experience_layer."""
        import asyncio

        from bot.service.pool_builder import create_pool

        with (
            patch("bot.service.pool_builder._build_llm_provider") as mock_llm,
            patch("bot.service.pool_builder._build_memory") as mock_mem,
            patch("bot.service.pool_builder._build_tools") as mock_tools,
            patch("bot.service.pool_builder._build_skill_manager") as mock_skills,
            patch("bot.service.pool_builder._build_agent_pool") as mock_build_pool,
        ):
            mock_llm.return_value = MagicMock(spec=LLMProvider)
            mock_mem_sys = MagicMock()
            mock_mem_sys.pruned_manager = MagicMock()
            mock_mem.return_value = mock_mem_sys

            mock_tool_mgr = MagicMock()
            mock_tool_mgr.list_tools.return_value = []
            mock_tools.return_value = (mock_tool_mgr, None)
            mock_skills.return_value = None

            mock_pipeline = MagicMock()
            mock_pipeline.hook_runner = MagicMock()
            mock_main_instance = MagicMock()
            mock_main_instance.pipeline = mock_pipeline
            mock_pool = AsyncMock()
            mock_pool._agents = {"testagent": mock_main_instance}
            mock_pool.list_profiles = MagicMock(return_value=[])
            mock_build_pool.return_value = mock_pool

            agent_mock = _make_enabled_agent_cfg()
            pool_cfg = MagicMock(
                llm=MagicMock(model="test", api_key="k", temperature=0.5, max_tokens=1000),
                memory=MagicMock(),
                agents=[agent_mock],
            )

            pi = asyncio.run(
                create_pool(
                    pool_name="testpool",
                    pool_cfg=pool_cfg,
                    project_dir=tmp_path,
                    data_dir=tmp_path / "data",
                    broker=MagicMock(),
                    inbox_server=MagicMock(),
                    inbox_consumer=MagicMock(),
                    agent_bus=MagicMock(),
                    output_adapter=MagicMock(),
                    safety=MagicMock(),
                    retention=MagicMock(),
                    comm_tracker=MagicMock(),
                    approval_workspace=tmp_path / "approval",
                    im_ui=MagicMock(),
                    shared_hooks=[],
                    shared_hook_runner=MagicMock(),
                    shared_interceptor_chain=MagicMock(),
                )
            )

            assert pi.experience_dir_ref is not None, (
                "create_pool must store dir_ref on PoolInstance"
            )
            assert isinstance(pi.experience_dir_ref, list)
            assert len(pi.experience_dir_ref) == 1
            assert isinstance(pi.experience_dir_ref[0], Path)
