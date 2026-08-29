"""TDD tests for default tool factories (task 11 of the scope-converge implementation).

Written FIRST, drives ``src/modex_agent/plugins/defaults/tools.py``.

Core invariant: the defaults registered name set MUST equal the union of
every ``ToolPreset``'s ``get_preset_tools`` expansion — dynamically
asserted, never hardcoded. Communication tools (task/send_to_peer/
send_to_agent) are NOT in defaults; they stay BIZ conditional registration.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel, ValidationError

from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.defaults.tools import (
    ExperienceToolConfig,
    ToolConfig,
    register_default_tools,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.tools.presets import (
    ToolPreset,
    get_preset_tools,
)

# Communication tool names that must NEVER appear in defaults — they are
# BIZ conditional registration (only when a pool has subagents/peers).
_COMM_TRIO = frozenset({"task", "send_to_peer", "send_to_agent"})


def _expected_preset_union() -> set[str]:
    """Compute the expected defaults name set from presets.py dynamically.

    Loops every ``ToolPreset`` member, expands via ``get_preset_tools``
    (default args — no bash, no scoped write, no root wrapping), and
    unions all tool names. This is the single source of truth the test
    compares against — if presets.py adds/removes a tool, the test
    adapts automatically.
    """
    expected: set[str] = set()
    for preset in ToolPreset:
        for tool in get_preset_tools(preset):
            expected.add(tool.name)
    return expected


def _expected_production_union() -> set[str]:
    # "bash"/"process"/"terminal" are the terminal-trio runtime factories
    # (process/terminal explicit roster opt-in, not preset-expanded);
    # "ast_grep_search"/"ast_grep_replace", "todo_write"/"todo_read", and
    # "experience" are the ast_grep / todo / experience capabilities'
    # direct registrations (no longer supplement-projected); "aci_edit"
    # is the aci capability's.
    return _expected_preset_union() | {
        "bash",
        "process",
        "terminal",
        "aci_edit",
        "ast_grep_search",
        "ast_grep_replace",
        "todo_write",
        "todo_read",
        "experience",
    }


def _register_defaults() -> ComponentRegistry:
    """Register default tools into a fresh registry and return it."""
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as ctx:
        register_default_tools(ctx)
    return registry


def _registered_tool_names(registry: ComponentRegistry) -> set[str]:
    """Extract the set of names registered in the TOOL slot."""
    slot_map = registry._factories.get(ComponentSlot.TOOL, {})  # noqa: SLF001
    return set(slot_map.keys())


# ---- Dynamic preset-union assertion (the core test) ---------------------


class TestDefaultsNameSetMatchesPresetsUnion:
    """defaults registered name set == union of all presets' tool names."""

    def test_registered_set_equals_preset_union(self):
        registry = _register_defaults()
        actual = _registered_tool_names(registry)
        expected = _expected_production_union()
        assert actual == expected, (
            f"defaults name set drifted from presets.py union: "
            f"missing={expected - actual}, extra={actual - expected}"
        )

    def test_registered_set_nonempty(self):
        """A nonempty union confirms presets.py has tools and they loaded."""
        registry = _register_defaults()
        actual = _registered_tool_names(registry)
        assert len(actual) > 0

    def test_preset_union_is_dynamic_not_hardcoded(self):
        """The expected set is computed from presets.py at call time,
        not a hardcoded literal. Verify it contains known preset tools
        that span multiple presets (FULL + WEB)."""
        expected = _expected_preset_union()
        # read/write/edit/ls/grep/glob come from FULL/READ_WRITE/READ_ONLY.
        assert "read" in expected
        assert "write" in expected
        assert "edit" in expected
        # web_search/web_reader come from WEB preset.
        assert "web_search" in expected
        assert "web_reader" in expected


# ---- Communication trio exclusion ---------------------------------------


class TestCommunicationTrioExcluded:
    """task/send_to_peer/send_to_agent must NOT be in defaults."""

    @pytest.mark.parametrize("comm_name", sorted(_COMM_TRIO))
    def test_comm_tool_absent_from_defaults(self, comm_name: str):
        registry = _register_defaults()
        actual = _registered_tool_names(registry)
        assert comm_name not in actual, (
            f"Communication tool {comm_name!r} must stay BIZ conditional "
            f"registration, not a default."
        )

    def test_comm_trio_absent_from_preset_union(self):
        """Sanity: presets.py itself does not name the comm trio —
        so a dynamic-anchored defaults set cannot accidentally include them."""
        expected = _expected_preset_union()
        assert _COMM_TRIO.isdisjoint(expected)


# ---- Factory type + create() contract -----------------------------------


# Names whose factories are runtime (pool-scoped deps) rather than SimpleFactory
# wrappers, or whose tool name deliberately differs from the registry name.
# "experience": pool-data-fed ExperienceToolFactory (moved from the bot plugin).
_RUNTIME_TOOL_NAMES = frozenset(
    {"todo_read", "todo_write", "bash", "process", "terminal", "experience"}
)
# Registry name → tool name mismatch: the ACI upgrade is registered under
# "aci_edit" but yields a tool named "edit" (drop-in upgrade contract).
_NAME_MISMATCH_TOOLS = frozenset({"aci_edit"})


class TestFactoryContract:
    """Stateless registered factories are SimpleFactory wrappers."""

    def test_stateless_factories_are_simple_factory(self):
        registry = _register_defaults()
        slot_map = registry._factories.get(ComponentSlot.TOOL, {})  # noqa: SLF001
        assert len(slot_map) > 0
        for name, factory in slot_map.items():
            if name in _RUNTIME_TOOL_NAMES:
                continue
            assert isinstance(factory, SimpleFactory), (
                f"factory for {name!r} is {type(factory).__name__}, expected SimpleFactory"
            )

    async def test_create_returns_tool_with_matching_name(self):
        """factory.create() returns the wrapped Tool instance whose
        .name matches the registered name."""
        registry = _register_defaults()
        slot_map = registry._factories.get(ComponentSlot.TOOL, {})  # noqa: SLF001
        for name, factory in slot_map.items():
            if name in _RUNTIME_TOOL_NAMES or name in _NAME_MISMATCH_TOOLS:
                continue
            instance = await factory.create(ToolConfig(), ctx=None)  # type: ignore[arg-type]
            assert instance.name == name, (
                f"factory.create() for {name!r} returned tool with name {instance.name!r}"
            )

    def test_config_model_is_toolconfig(self):
        """Each factory's config_model is ToolConfig (the shared minimal
        frozen config for stateless standard tools). The pool-data-fed
        experience factory is the one exception — its own dedicated frozen
        config, asserted inline."""
        registry = _register_defaults()
        slot_map = registry._factories.get(ComponentSlot.TOOL, {})  # noqa: SLF001
        for name, factory in slot_map.items():
            if name == "experience":
                assert factory.config_model is ExperienceToolConfig
                continue
            assert factory.config_model is ToolConfig, (
                f"factory for {name!r} has config_model "
                f"{factory.config_model!r}, expected ToolConfig"
            )


# ---- ToolConfig contract ------------------------------------------------


class TestToolConfig:
    """ToolConfig is a minimal frozen Pydantic BaseModel with extra=forbid."""

    def test_is_basemodel_subclass(self):
        assert issubclass(ToolConfig, BaseModel)

    def test_no_required_fields(self):
        """ToolConfig accepts empty construction (no required fields)."""
        config = ToolConfig()
        assert config is not None

    def test_frozen_cannot_reassign(self):
        config = ToolConfig()
        with pytest.raises(ValidationError):
            config.anything = "x"  # type: ignore[misc]

    def test_extra_forbid_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            ToolConfig(unknown_field="x")  # type: ignore[call-arg]

    def test_model_config_frozen_and_extra_forbid(self):
        assert ToolConfig.model_config.get("frozen") is True
        assert ToolConfig.model_config.get("extra") == "forbid"


# ---- register_default_tools signature + dynamic anchoring ---------------


class TestRegisterDefaultTools:
    """register_default_tools is a sync function that calls ctx.register_tool
    for each preset-derived tool — no hardcoded list."""

    def test_is_sync_not_async(self):
        assert not inspect.iscoroutinefunction(register_default_tools)

    def test_calls_register_tool_per_unique_name(self):
        """The number of register_tool calls == the number of unique tool
        names in the preset union (dedup across presets)."""
        registry = ComponentRegistry()
        # Capture register_tool calls via a thin wrapper.
        calls: list[str] = []
        original_register = PluginRegistrationContext.register_tool

        def spy_register(ctx_self: PluginRegistrationContext, name: str, factory: object) -> None:
            calls.append(name)
            original_register(ctx_self, name, factory)  # type: ignore[arg-type]

        # Monkeypatch for the duration of the test.
        PluginRegistrationContext.register_tool = spy_register  # type: ignore[method-assign]
        try:
            with PluginRegistrationContext(registry) as ctx:
                register_default_tools(ctx)
        finally:
            PluginRegistrationContext.register_tool = original_register  # type: ignore[method-assign]

        expected = _expected_production_union()
        assert set(calls) == expected
        # No duplicate registrations.
        assert len(calls) == len(set(calls)), (
            f"register_tool called with duplicates: {[n for n in calls if calls.count(n) > 1]}"
        )
