"""TDD tests for Plugin ABC + PluginRegistrationContext + PluginDiscoveryConfig
+ ComponentRegistryLoader (task 4).

Written FIRST, drives the rewrite of ``src/modex_agent/plugins/loader.py``.

Covers:
- ``Plugin`` ABC — cannot instantiate directly, subclass must implement
  ``register()``.
- ``PluginRegistrationContext`` — 10 ``register_*`` methods buffer into
  internal storage; ``__exit__`` flushes on clean exit / discards on
  exception (atomicity — no half-registration from a failing plugin).
- ``PluginDiscoveryConfig`` — frozen dataclass with correct defaults.
- ``ComponentRegistryLoader.load`` — four-source discovery (bundled,
  project, user, entry_points); fault isolation (one plugin fails, others
  load); atomicity (failing plugin's factories discarded); project
  path discovery from a temp directory; cross-source duplicate →
  source-priority resolution (user > project > entry_points > bundled,
  SPEC §3.5 O2: higher-priority source overrides, lower skips, direct
  registration preempts); same-source duplicate → ``ValueError``
  (SPEC §4.1).
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentSlot, PluginSource, SimpleFactory
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import ComponentNotFoundError, ComponentRegistry

# ---- Test helpers --------------------------------------------------------


class _DummyConfig(BaseModel):
    """Minimal frozen Pydantic config for SimpleFactory."""

    model_config = {"frozen": True, "extra": "forbid"}


def _factory(instance: object | None = None) -> SimpleFactory:
    """Build a SimpleFactory wrapping an arbitrary instance."""
    return SimpleFactory(instance=instance or object(), config_model=_DummyConfig)


# ---- Test plugins --------------------------------------------------------


class _NormalPlugin(Plugin):
    """Registers one tool, succeeds."""

    config_model = _DummyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("normal_tool", _factory())


class _FailingPlugin(Plugin):
    """Registers one tool then raises — tests fault isolation."""

    config_model = _DummyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("fail_tool", _factory())
        raise ValueError("plugin failed")


class _AtomicFailPlugin(Plugin):
    """Registers 4 tools then raises — tests atomicity (all discarded)."""

    config_model = _DummyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("t1", _factory())
        ctx.register_tool("t2", _factory())
        ctx.register_tool("t3", _factory())
        ctx.register_tool("t4", _factory())
        raise ValueError("atomic failure")


_FIRST_SHARED = object()
_SECOND_SHARED = object()


class _MultiSlotPlugin(Plugin):
    """Registers across all 9 slots — verifies every register_* method."""

    config_model = _DummyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("tool1", _factory())
        ctx.register_hook("hook1", _factory())
        ctx.register_provider("llm1", _factory())
        ctx.register_prompt_provider("prompt1", _factory())
        ctx.register_interceptor("int1", _factory())
        ctx.register_command("cmd1", _factory())
        ctx.register_execution_strategy("exec1", _factory())
        ctx.register_input_stage("stage1", _factory())
        ctx.register_namespace("ns1", _factory())


class _FirstPluginWithCollision(Plugin):
    """First plugin — registers shared_tool + a unique tool."""

    config_model = _DummyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("shared_tool", _factory(_FIRST_SHARED))
        ctx.register_tool("first_only_tool", _factory())


class _SecondPluginWithCollision(Plugin):
    """Second plugin — collides on shared_tool + adds a unique tool."""

    config_model = _DummyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("shared_tool", _factory(_SECOND_SHARED))
        ctx.register_tool("second_only_tool", _factory())


class _SameSourceDuplicatePlugin(Plugin):
    """Registers the same name twice in its own buffer — developer error."""

    config_model = _DummyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool("dup_tool", _factory(_FIRST_SHARED))
        ctx.register_tool("dup_tool", _factory(_SECOND_SHARED))
        ctx.register_tool("after_dup", _factory())


# ---- Plugin ABC tests ----------------------------------------------------


class TestPluginABC:
    """Plugin ABC contract — abstract, must implement register()."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract"):
            Plugin()  # type: ignore[abstract]

    def test_subclass_must_implement_register(self):
        class _Incomplete(Plugin):
            config_model = _DummyConfig

        with pytest.raises(TypeError, match="abstract"):
            _Incomplete()  # type: ignore[abstract]

    def test_api_version_default(self):
        assert _NormalPlugin.api_version == 1

    def test_config_model_is_classvar(self):
        assert _NormalPlugin.config_model is _DummyConfig


# ---- PluginRegistrationContext tests -------------------------------------


class TestPluginRegistrationContext:
    """Context buffering, flush, and atomic discard."""

    def test_enter_returns_self(self):
        registry = ComponentRegistry()
        ctx = PluginRegistrationContext(registry)
        assert ctx.__enter__() is ctx

    def test_flush_on_clean_exit(self):
        registry = ComponentRegistry()
        plugin = _NormalPlugin()
        with PluginRegistrationContext(registry) as ctx:
            plugin.register(ctx)
        factory = registry.resolve(ComponentSlot.TOOL, "normal_tool")
        assert factory is not None

    def test_discard_on_exception(self):
        registry = ComponentRegistry()
        plugin = _FailingPlugin()
        with pytest.raises(ValueError, match="plugin failed"):
            with PluginRegistrationContext(registry) as ctx:
                plugin.register(ctx)
        with pytest.raises(ComponentNotFoundError):
            registry.resolve(ComponentSlot.TOOL, "fail_tool")

    def test_atomicity_all_discarded_on_exception(self):
        """Plugin registers 4 factories then raises — NONE in registry."""
        registry = ComponentRegistry()
        plugin = _AtomicFailPlugin()
        with pytest.raises(ValueError, match="atomic failure"):
            with PluginRegistrationContext(registry) as ctx:
                plugin.register(ctx)
        for name in ("t1", "t2", "t3", "t4"):
            with pytest.raises(ComponentNotFoundError):
                registry.resolve(ComponentSlot.TOOL, name)

    def test_all_9_register_methods_flush_to_correct_slots(self):
        """Each register_* method maps to the correct ComponentSlot."""
        registry = ComponentRegistry()
        plugin = _MultiSlotPlugin()
        with PluginRegistrationContext(registry) as ctx:
            plugin.register(ctx)
        assert registry.resolve(ComponentSlot.TOOL, "tool1")
        assert registry.resolve(ComponentSlot.HOOK, "hook1")
        assert registry.resolve(ComponentSlot.LLM_PROVIDER, "llm1")
        assert registry.resolve(ComponentSlot.SYSTEM_PROMPT_PROVIDER, "prompt1")
        assert registry.resolve(ComponentSlot.INTERCEPTOR, "int1")
        assert registry.resolve(ComponentSlot.COMMAND_HANDLER, "cmd1")
        assert registry.resolve(ComponentSlot.EXECUTION_STRATEGY, "exec1")
        assert registry.resolve(ComponentSlot.INPUT_STAGE, "stage1")
        assert registry.resolve(ComponentSlot.DATA_NAMESPACE, "ns1")

    def test_exit_does_not_suppress_exception(self):
        """__exit__ must return None/False — must not suppress."""
        registry = ComponentRegistry()
        ctx = PluginRegistrationContext(registry)
        with pytest.raises(ValueError, match="test"), ctx:
            raise ValueError("test")


# ---- PluginDiscoveryConfig tests -----------------------------------------


class TestPluginDiscoveryConfig:
    """Frozen config with correct defaults."""

    def test_frozen_cannot_reassign(self):
        config = PluginDiscoveryConfig(
            bundled_factories=(),
            project_plugin_paths=(),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            config.entry_point_group = "other"  # type: ignore[misc]

    def test_defaults(self):
        config = PluginDiscoveryConfig(
            bundled_factories=(),
            project_plugin_paths=(),
        )
        assert config.user_plugin_path is None
        assert config.entry_point_group == "modex_agent.plugins"

    def test_bundled_factories_is_tuple(self):
        """Frozen value object carries an immutable sequence (M4) — a list
        field would let callers mutate frozen state."""
        config = PluginDiscoveryConfig(
            bundled_factories=(_NormalPlugin(),),
            project_plugin_paths=(),
        )
        assert isinstance(config.bundled_factories, tuple)

    def test_custom_values(self):
        path = Path("/tmp/plugins")
        config = PluginDiscoveryConfig(
            bundled_factories=(_NormalPlugin(),),
            project_plugin_paths=(path,),
            user_plugin_path=Path("/tmp/user"),
            entry_point_group="custom.plugins",
        )
        assert len(config.bundled_factories) == 1
        assert config.project_plugin_paths == (path,)
        assert config.user_plugin_path == Path("/tmp/user")
        assert config.entry_point_group == "custom.plugins"


# ---- ComponentRegistryLoader tests ---------------------------------------


class TestComponentRegistryLoader:
    """Loader: bundled loading, fault isolation, atomicity, project discovery."""

    async def test_bundled_plugins_load(self):
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_NormalPlugin(),),
            project_plugin_paths=(),
        )
        await ComponentRegistryLoader.load(registry, config)
        assert registry.resolve(ComponentSlot.TOOL, "normal_tool")

    async def test_fault_isolation_failing_plugin_does_not_block_others(self):
        """One plugin fails → its factories absent, other plugin's present."""
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_FailingPlugin(), _NormalPlugin()),
            project_plugin_paths=(),
        )
        await ComponentRegistryLoader.load(registry, config)
        with pytest.raises(ComponentNotFoundError):
            registry.resolve(ComponentSlot.TOOL, "fail_tool")
        assert registry.resolve(ComponentSlot.TOOL, "normal_tool")

    async def test_atomicity_failing_plugin_factories_all_discarded(self):
        """Plugin registers 4 then raises — NONE in registry, no exception propagated."""
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_AtomicFailPlugin(),),
            project_plugin_paths=(),
        )
        await ComponentRegistryLoader.load(registry, config)
        for name in ("t1", "t2", "t3", "t4"):
            with pytest.raises(ComponentNotFoundError):
                registry.resolve(ComponentSlot.TOOL, name)

    async def test_project_path_discovery(self, tmp_path: Path):
        """Plugin discovered from a temp directory .py file."""
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(
            textwrap.dedent(
                """
                from modex_agent.plugins.abc import SimpleFactory
                from modex_agent.plugins.loader import (
                    Plugin,
                    PluginRegistrationContext,
                )
                from pydantic import BaseModel


                class _Config(BaseModel):
                    model_config = {"frozen": True, "extra": "forbid"}


                class DiscoveredPlugin(Plugin):
                    config_model = _Config

                    def register(self, ctx: PluginRegistrationContext) -> None:
                        ctx.register_tool(
                            "discovered_tool",
                            SimpleFactory(instance="found", config_model=_Config),
                        )
                """
            ),
            encoding="utf-8",
        )

        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(),
            project_plugin_paths=(tmp_path,),
        )
        await ComponentRegistryLoader.load(registry, config)
        factory = registry.resolve(ComponentSlot.TOOL, "discovered_tool")
        assert factory is not None

    async def test_nonexistent_project_path_logs_and_continues(
        self, tmp_path: Path
    ):
        """Non-existent path should log warning and continue, not crash."""
        nonexistent = tmp_path / "does_not_exist"
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_NormalPlugin(),),
            project_plugin_paths=(nonexistent,),
        )
        await ComponentRegistryLoader.load(registry, config)
        assert registry.resolve(ComponentSlot.TOOL, "normal_tool")

    async def test_multi_slot_plugin_loads_all_9(self):
        """A plugin registering across all 9 slots loads everything."""
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_MultiSlotPlugin(),),
            project_plugin_paths=(),
        )
        await ComponentRegistryLoader.load(registry, config)
        for slot, name in [
            (ComponentSlot.TOOL, "tool1"),
            (ComponentSlot.HOOK, "hook1"),
            (ComponentSlot.LLM_PROVIDER, "llm1"),
            (ComponentSlot.SYSTEM_PROMPT_PROVIDER, "prompt1"),
            (ComponentSlot.INTERCEPTOR, "int1"),
            (ComponentSlot.COMMAND_HANDLER, "cmd1"),
            (ComponentSlot.EXECUTION_STRATEGY, "exec1"),
            (ComponentSlot.INPUT_STAGE, "stage1"),
            (ComponentSlot.DATA_NAMESPACE, "ns1"),
        ]:
            assert registry.resolve(slot, name), f"{name} missing from {slot}"


# ---- Cross-source source-priority / same-source conflict (SPEC §4.1, §3.5 O2) -----


_PROJECT_COLLIDING_PLUGIN = textwrap.dedent(
    """
    from modex_agent.plugins.abc import SimpleFactory
    from modex_agent.plugins.loader import (
        Plugin,
        PluginRegistrationContext,
    )
    from pydantic import BaseModel


    class _Config(BaseModel):
        model_config = {"frozen": True, "extra": "forbid"}


    class ProjectCollidingPlugin(Plugin):
        \"\"\"Project-source plugin colliding with a bundled name.\"\"\"

        config_model = _Config

        def register(self, ctx: PluginRegistrationContext) -> None:
            ctx.register_tool(
                "shared_tool",
                SimpleFactory(instance="project_marker", config_model=_Config),
            )
            ctx.register_tool(
                "second_only_tool",
                SimpleFactory(instance="project_marker", config_model=_Config),
            )
    """
)


_USER_COLLIDING_PLUGIN = textwrap.dedent(
    """
    from modex_agent.plugins.abc import SimpleFactory
    from modex_agent.plugins.loader import (
        Plugin,
        PluginRegistrationContext,
    )
    from pydantic import BaseModel


    class _Config(BaseModel):
        model_config = {"frozen": True, "extra": "forbid"}


    class UserCollidingPlugin(Plugin):
        \"\"\"User-source plugin colliding with a bundled name.\"\"\"

        config_model = _Config

        def register(self, ctx: PluginRegistrationContext) -> None:
            ctx.register_tool(
                "shared_tool",
                SimpleFactory(instance="user_marker", config_model=_Config),
            )
            ctx.register_tool(
                "user_only_tool",
                SimpleFactory(instance="user_marker", config_model=_Config),
            )
    """
)


class TestCrossSourcePriority:
    """SPEC §4.1 + §3.5 O2: cross-source duplicate resolves by source
    priority (user > project > entry_points > bundled — the
    higher-priority source overrides with an info log, the lower is
    skipped with an info log, a direct registration preempts with a
    warning); same-source duplicate → ValueError out of load()."""

    def test_source_priority_table_ordering(self):
        """Explicit priority table on PluginSource: user > project >
        entry_points > bundled (nearest-to-user wins)."""
        assert (
            PluginSource.SOURCE_PRIORITY[PluginSource.USER]
            > PluginSource.SOURCE_PRIORITY[PluginSource.PROJECT]
            > PluginSource.SOURCE_PRIORITY[PluginSource.ENTRY_POINTS]
            > PluginSource.SOURCE_PRIORITY[PluginSource.BUNDLED]
        )

    async def test_user_plugin_overrides_bundled_same_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A USER-source plugin colliding with a bundled name OVERRIDES
        it (ticket 01 AC): ``resolve`` returns the user factory,
        ``registration_source`` is USER, and the override info log names
        slot/name and the old→new sources."""
        (tmp_path / "user_plugin.py").write_text(
            _USER_COLLIDING_PLUGIN, encoding="utf-8"
        )
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_FirstPluginWithCollision(),),
            project_plugin_paths=(),
            user_plugin_path=tmp_path,
        )
        with caplog.at_level("INFO", logger="modex_agent.plugins.loader"):
            await ComponentRegistryLoader.load(registry, config)

        factory = registry.resolve(ComponentSlot.TOOL, "shared_tool")
        assert isinstance(factory, SimpleFactory)
        assert factory._instance == "user_marker"  # noqa: SLF001
        assert (
            registry.registration_source(ComponentSlot.TOOL, "shared_tool")
            is PluginSource.USER
        )
        # Non-colliding names from BOTH sources survive.
        assert registry.resolve(ComponentSlot.TOOL, "first_only_tool")
        assert registry.resolve(ComponentSlot.TOOL, "user_only_tool")
        assert any(
            "shared_tool" in record.message
            and "tool" in record.message
            and "bundled" in record.message
            and "user" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )

    async def test_cross_source_collision_project_overrides_bundled(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Bundled plugin registers ``shared_tool`` first; a PROJECT-source
        plugin colliding on it OVERRIDES (project > bundled — SPEC §3.5
        O2) while its non-colliding name still registers."""
        (tmp_path / "colliding_plugin.py").write_text(
            _PROJECT_COLLIDING_PLUGIN, encoding="utf-8"
        )
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_FirstPluginWithCollision(),),
            project_plugin_paths=(tmp_path,),
        )
        with caplog.at_level("INFO", logger="modex_agent.plugins.loader"):
            await ComponentRegistryLoader.load(registry, config)

        factory = registry.resolve(ComponentSlot.TOOL, "shared_tool")
        assert isinstance(factory, SimpleFactory)
        assert factory._instance == "project_marker"  # noqa: SLF001
        assert (
            registry.registration_source(ComponentSlot.TOOL, "shared_tool")
            is PluginSource.PROJECT
        )
        assert registry.resolve(ComponentSlot.TOOL, "first_only_tool")
        assert registry.resolve(ComponentSlot.TOOL, "second_only_tool")
        assert any(
            "shared_tool" in record.message
            and "bundled" in record.message
            and "project" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )

    def test_lower_priority_source_late_registration_skips(
        self, caplog: pytest.LogCaptureFixture
    ):
        """An existing USER registration preempts a later BUNDLED-sourced
        registration: skipped with an INFO log; the user factory and its
        attribution are untouched."""
        registry = ComponentRegistry()
        user_ctx = PluginRegistrationContext(registry, source=PluginSource.USER)
        user_ctx.register_tool("shared_tool", _factory(_FIRST_SHARED))
        user_ctx.flush()

        bundled_ctx = PluginRegistrationContext(
            registry, source=PluginSource.BUNDLED
        )
        bundled_ctx.register_tool("shared_tool", _factory(_SECOND_SHARED))
        with caplog.at_level("INFO", logger="modex_agent.plugins.loader"):
            bundled_ctx.flush()

        factory = registry.resolve(ComponentSlot.TOOL, "shared_tool")
        assert isinstance(factory, SimpleFactory)
        assert factory._instance is _FIRST_SHARED  # noqa: SLF001
        assert (
            registry.registration_source(ComponentSlot.TOOL, "shared_tool")
            is PluginSource.USER
        )
        assert any(
            "shared_tool" in record.message
            and "user" in record.message
            and "bundled" in record.message
            and record.levelname == "INFO"
            for record in caplog.records
        )

    def test_direct_registration_preempts_user_source(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A directly-registered entry (source=None — the code path,
        bypassing source semantics) preempts even a USER-sourced
        registration: skipped with a WARNING log; the direct factory and
        its None attribution are untouched."""
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.TOOL, "shared_tool", _factory(_FIRST_SHARED)
        )

        user_ctx = PluginRegistrationContext(registry, source=PluginSource.USER)
        user_ctx.register_tool("shared_tool", _factory(_SECOND_SHARED))
        with caplog.at_level("WARNING", logger="modex_agent.plugins.loader"):
            user_ctx.flush()

        factory = registry.resolve(ComponentSlot.TOOL, "shared_tool")
        assert isinstance(factory, SimpleFactory)
        assert factory._instance is _FIRST_SHARED  # noqa: SLF001
        assert (
            registry.registration_source(ComponentSlot.TOOL, "shared_tool") is None
        )
        assert any(
            "shared_tool" in record.message
            and "direct registration" in record.message
            and record.levelname == "WARNING"
            for record in caplog.records
        )

    async def test_same_source_duplicate_across_plugins_raises(self):
        """Two BUNDLED plugins colliding on one name → ValueError out of
        load() (SPEC §4.1 same-source conflict). The first registration
        survives; the second plugin's remaining buffered names are
        discarded."""
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(
                _FirstPluginWithCollision(),
                _SecondPluginWithCollision(),
            ),
            project_plugin_paths=(),
        )
        with pytest.raises(ValueError, match="shared_tool.*same-source conflict"):
            await ComponentRegistryLoader.load(registry, config)

        factory = registry.resolve(ComponentSlot.TOOL, "shared_tool")
        assert isinstance(factory, SimpleFactory)
        assert factory._instance is _FIRST_SHARED  # noqa: SLF001
        with pytest.raises(ComponentNotFoundError):
            registry.resolve(ComponentSlot.TOOL, "second_only_tool")

    async def test_same_source_duplicate_within_plugin_raises(self):
        """One plugin registering the same name twice → ValueError out of
        load(); the flush is atomic — NOTHING from the conflicting plugin
        is registered (validation precedes any mutation)."""
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_SameSourceDuplicatePlugin(),),
            project_plugin_paths=(),
        )
        with pytest.raises(ValueError, match="dup_tool.*same-source conflict"):
            await ComponentRegistryLoader.load(registry, config)

        with pytest.raises(ComponentNotFoundError):
            registry.resolve(ComponentSlot.TOOL, "dup_tool")
        with pytest.raises(ComponentNotFoundError):
            registry.resolve(ComponentSlot.TOOL, "after_dup")

    async def test_with_ctx_inside_register_then_loader_flush_is_noop(self):
        """A plugin using the documented ``with ctx:`` pattern inside
        ``register()`` must not crash the loader's explicit ``flush()``.

        Regression anchor: flush() once drained the buffer only by
        iteration (no clear), so the second flush re-processed the same
        entries and raised a false "same-source conflict" out of load().
        """

        class _WithContextPlugin(_NormalPlugin):
            def register(self, ctx: PluginRegistrationContext) -> None:
                with ctx:
                    super().register(ctx)

        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_WithContextPlugin(),),
            project_plugin_paths=(),
        )
        await ComponentRegistryLoader.load(registry, config)

        factory = registry.resolve(ComponentSlot.TOOL, "normal_tool")
        assert isinstance(factory, SimpleFactory)

    def test_flush_is_idempotent_direct(self):
        """Direct double flush() is a no-op, not a same-source crash."""
        registry = ComponentRegistry()
        ctx = PluginRegistrationContext(registry, source=PluginSource.BUNDLED)
        ctx.register_tool("tool_a", _factory())
        ctx.flush()
        ctx.flush()

        factory = registry.resolve(ComponentSlot.TOOL, "tool_a")
        assert isinstance(factory, SimpleFactory)

    async def test_entry_points_discovery_failure_logs_and_continues(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """``entry_points()`` itself raising is logged (warning, group +
        exception) and the load continues with the remaining sources."""

        def _boom() -> object:
            raise RuntimeError("entry points exploded")

        monkeypatch.setattr("importlib.metadata.entry_points", _boom)
        registry = ComponentRegistry()
        config = PluginDiscoveryConfig(
            bundled_factories=(_NormalPlugin(),),
            project_plugin_paths=(),
        )
        with caplog.at_level("WARNING", logger="modex_agent.plugins.loader"):
            await ComponentRegistryLoader.load(registry, config)

        assert registry.resolve(ComponentSlot.TOOL, "normal_tool")
        assert any(
            "Entry point discovery" in record.message
            and "entry points exploded" in record.message
            for record in caplog.records
        )


# ---- Deterministic module identity (same file, repeated discovery) -----


_STABLE_TOOL_PLUGIN = textwrap.dedent(
    """
    from modex_agent.plugins.abc import SimpleFactory
    from modex_agent.plugins.loader import (
        Plugin,
        PluginRegistrationContext,
    )
    from pydantic import BaseModel


    class _Config(BaseModel):
        model_config = {"frozen": True, "extra": "forbid"}


    class StableToolPlugin(Plugin):
        config_model = _Config

        def register(self, ctx: PluginRegistrationContext) -> None:
            ctx.register_tool(
                "stable_tool",
                SimpleFactory(instance="stable", config_model=_Config),
            )
    """
)


def _discovered_module_names() -> set[str]:
    return {name for name in sys.modules if name.startswith("_modex_discovered_")}


class TestDeterministicModuleImport:
    """Same file discovered repeatedly → ONE sys.modules entry.

    Regression: the module name used to be a fresh UUID per discovery, so
    every scan leaked a module entry forever and the same file discovered
    twice produced two UNRELATED Plugin class objects (isinstance /
    issubclass always False between them).
    """

    async def test_same_directory_loaded_twice_reuses_module(
        self, tmp_path: Path
    ):
        """Two full discovery scans over the same directory (a fresh
        registry each) do not raise, register the names once per registry,
        and grow ``_modex_discovered_*`` by exactly 1 across BOTH loads —
        the second scan reuses the already-executed module."""
        (tmp_path / "stable_plugin.py").write_text(
            _STABLE_TOOL_PLUGIN, encoding="utf-8"
        )
        before = _discovered_module_names()

        registry_one = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry_one,
            PluginDiscoveryConfig(
                bundled_factories=(),
                project_plugin_paths=(tmp_path,),
            ),
        )
        assert registry_one.names(ComponentSlot.TOOL) == ("stable_tool",)
        after_first = _discovered_module_names()
        assert len(after_first - before) == 1

        registry_two = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry_two,
            PluginDiscoveryConfig(
                bundled_factories=(),
                project_plugin_paths=(tmp_path,),
            ),
        )
        assert registry_two.names(ComponentSlot.TOOL) == ("stable_tool",)
        # Deterministic-name reuse: the second load added NO new module.
        assert _discovered_module_names() == after_first
        assert len(_discovered_module_names() - before) == 1

    async def test_same_file_via_two_paths_single_module_identity(
        self, tmp_path: Path
    ):
        """Two different Path objects pointing at the SAME plugin file
        (a real dir + a symlinked dir) resolve to ONE module: both loads
        succeed (no ValueError), exactly one module entry is created, and
        both discoveries return the SAME Plugin class object."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        plugin_file = real_dir / "linked_plugin.py"
        plugin_file.write_text(_STABLE_TOOL_PLUGIN, encoding="utf-8")
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        assert (link_dir / "linked_plugin.py") != plugin_file

        before = _discovered_module_names()

        registry_one = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry_one,
            PluginDiscoveryConfig(
                bundled_factories=(),
                project_plugin_paths=(real_dir,),
            ),
        )
        assert registry_one.names(ComponentSlot.TOOL) == ("stable_tool",)
        after_first = _discovered_module_names()
        assert len(after_first - before) == 1

        registry_two = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry_two,
            PluginDiscoveryConfig(
                bundled_factories=(),
                project_plugin_paths=(link_dir,),
            ),
        )
        assert registry_two.names(ComponentSlot.TOOL) == ("stable_tool",)
        # Single module identity: the symlink-path load reused the module
        # created by the real-path load.
        assert _discovered_module_names() == after_first

        # Direct identity check: both path objects yield the same class.
        classes_real = ComponentRegistryLoader._import_plugin_classes(plugin_file)
        classes_link = ComponentRegistryLoader._import_plugin_classes(
            link_dir / "linked_plugin.py"
        )
        assert len(classes_real) == 1
        assert classes_real[0] is classes_link[0]
