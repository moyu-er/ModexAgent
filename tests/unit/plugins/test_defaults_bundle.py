"""Integration test for DefaultPlugin — the FW-bundled defaults aggregator (task 14).

``DefaultPlugin`` (in ``src/modex_agent/plugins/defaults/__init__.py``) is a
single ``Plugin`` entry point that calls all 8 ``register_default_*``
functions. This test loads it through the real
``ComponentRegistryLoader.load`` path (not direct ``register_default_*``
calls) and asserts the EXACT name set registered into each of the 11
``ComponentSlot`` values.

Design decision — 4 slots are EMPTY by FW design:
- ``EXECUTION_STRATEGY`` — empty. Strategies (``react``, ``external``) are
  business-layer concerns registered by ``BotStrategiesPlugin``
  (``examples/bot_project/plugins/bot_strategies.py``), NOT framework
  defaults. The FW ships only the ``ExecutionStrategy`` ABC.
- ``INPUT_STAGE`` — empty. Input pipeline stages (``/cd``, ``/pool``,
  ``/stop`` interception, skill parsing) are IM/WebUI business wiring from
  ``examples/bot_project/bot/input_pipeline/``, NOT framework defaults.
- ``MEMORY_SYSTEM`` — empty. No built-in memory-system factory (SPEC
  Errata-7); users register their own.
- ``DATA_NAMESPACE`` — empty. The FW registers no default data
  namespaces; plugins supply their own (the former default trigger
  configs were dead registrations and were removed).

The other 7 slots are populated by the 8 ``register_default_*`` functions
(tools counts twice: preset tools + derived communication entries).
``CAPABILITY`` holds the FW-bundled capability packages (``aci``,
``ast_grep``, ``experience``, ``subagents``, ``todo``; ADR-0047 — grows
one package per migration wave).
"""

from __future__ import annotations

from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.tools.presets import (
    ToolPreset,
    get_preset_tools,
)

# ---- Expected name sets --------------------------------------------------

#: HOOK slot — 19 default hooks (SPEC §6.7, task 12 + the `tracing`
#: capability's seven span-hook resolvers).
_EXPECTED_HOOK_NAMES: frozenset[str] = frozenset(
    {
        "inbox_flush",
        "todo_continuation",
        "todo_planning_nudge",
        "deliver_retry",
        "length_guard",
        "native_env",
        "loop_detection",
        "run_logging",
        "subagent_auto_send",
        "memory_trace",
        "todo_reorientation",
        "experience_review",
        "trace_root",
        "trace_chat",
        "trace_tool",
        "trace_handoff",
        "trace_approval",
        "trace_agent_start",
        "trace_iteration",
    }
)

#: LLM_PROVIDER slot — single ``default`` factory (task 13).
_EXPECTED_LLM_PROVIDER_NAMES: frozenset[str] = frozenset({"default"})

#: SYSTEM_PROMPT_PROVIDER slot — single ``file_prompt`` factory (task 13).
_EXPECTED_PROMPT_PROVIDER_NAMES: frozenset[str] = frozenset({"file_prompt"})

#: INTERCEPTOR slot — ``tool_timeout`` (task 13) + the opt-in
#: ``sandbox_guard`` policy layer (sandbox-integration Ticket 08; refuses
#: to build under the dormant DEFAULT tier).
_EXPECTED_INTERCEPTOR_NAMES: frozenset[str] = frozenset(
    {"tool_timeout", "sandbox_guard"}
)

#: COMMAND_HANDLER slot — 6 built-in slash command handlers (task 13).
#: Names are bare (no leading ``/``) — matching register_default_commands.
_EXPECTED_COMMAND_NAMES: frozenset[str] = frozenset(
    {"cd", "stop", "pool", "approve", "deny", "continue"}
)


def _expected_tool_names() -> frozenset[str]:
    """Dynamically derive the expected TOOL names from presets.py.

    Same anchoring as ``test_defaults_tools.py``: iterate every
    ``ToolPreset``, expand via ``get_preset_tools`` (default args), union
    tool names, plus the registry-level extras �� ``bash`` (preset-gated
    name with a runtime factory), ``process``/``terminal`` (terminal-trio
    companions, explicit roster opt-in), ``aci_edit`` (the ACI
    upgrade's registry name), ``ast_grep_search``/``ast_grep_replace``
    (the ast_grep capability's direct registrations),
    ``todo_write``/``todo_read`` (the todo capability's direct
    registrations), ``experience`` (the experience capability's), and
    the derived communication entries
    ``task``/``send_to_agent``/``send_to_peer``
    (ticket 07: resolved only when a compiled spec carries them). If
    presets.py changes, this adapts automatically �� no hardcoded list.
    """
    names: set[str] = set()
    for preset in ToolPreset:
        for tool in get_preset_tools(preset):
            names.add(tool.name)
    names.update(
        {
            "bash",
            "process",
            "terminal",
            "aci_edit",
            "ast_grep_search",
            "ast_grep_replace",
            "todo_write",
            "todo_read",
            "experience",
            "task",
            "send_to_agent",
            "send_to_peer",
        }
    )
    return frozenset(names)


# ---- Loader helper -------------------------------------------------------


async def _load_default_plugin() -> ComponentRegistry:
    """Load ``DefaultPlugin`` via the real ``ComponentRegistryLoader`` path.

    Mirrors the production startup: ``PluginDiscoveryConfig`` with the
    plugin as a bundled factory, no project/user paths, default entry-point
    group. The loader calls ``plugin.register(ctx)`` inside a
    ``PluginRegistrationContext`` which flushes factories to the registry
    on clean exit.
    """
    registry = ComponentRegistry()
    discovery = PluginDiscoveryConfig(
        bundled_factories=(DefaultPlugin(),),
        project_plugin_paths=(),
        user_plugin_path=None,
        entry_point_group="modex_agent.plugins",
    )
    await ComponentRegistryLoader.load(registry, discovery)
    return registry


def _slot_names(registry: ComponentRegistry, slot: ComponentSlot) -> set[str]:
    """Return the set of names registered under *slot*.

    Uses ``registry._factories`` (same pattern as ``test_defaults_tools``
    and ``test_bot_strategies``) because ``ComponentRegistry`` exposes no
    public slot-enumeration API.
    """
    return set(registry._factories.get(slot, {}).keys())  # noqa: SLF001


# ---- Plugin class structure ---------------------------------------------


class TestDefaultPluginClass:
    def test_is_plugin_subclass(self) -> None:
        assert issubclass(DefaultPlugin, Plugin)

    def test_api_version_is_1(self) -> None:
        assert DefaultPlugin.api_version == 1

    def test_config_model_is_frozen_pydantic(self) -> None:
        config_model = DefaultPlugin.config_model
        assert issubclass(config_model, BaseModel)
        assert config_model.model_config.get("frozen") is True
        assert config_model.model_config.get("extra") == "forbid"

    def test_config_model_constructs_empty(self) -> None:
        """The minimal config accepts empty construction (no required fields)."""
        config = DefaultPlugin.config_model()
        assert config is not None

    def test_register_is_sync_not_async(self) -> None:
        import inspect

        assert not inspect.iscoroutinefunction(DefaultPlugin.register)


# ---- Per-slot name set assertions (the core integration test) -----------


class TestPerSlotNameSets:
    """Assert the EXACT name set registered into each of the 11 slots.

    7 slots are populated by the 8 ``register_default_*`` functions; 4 are
    empty by FW design (EXECUTION_STRATEGY, INPUT_STAGE, MEMORY_SYSTEM,
    DATA_NAMESPACE — see module docstring).
    """

    async def test_tool_slot_matches_preset_union(self) -> None:
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.TOOL)
        expected = _expected_tool_names()
        assert actual == expected, (
            f"TOOL slot drift: missing={expected - actual}, extra={actual - expected}"
        )

    async def test_hook_slot_has_18_names(self) -> None:
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.HOOK)
        assert actual == _EXPECTED_HOOK_NAMES, (
            f"HOOK slot drift: missing={_EXPECTED_HOOK_NAMES - actual}, "
            f"extra={actual - _EXPECTED_HOOK_NAMES}"
        )

    async def test_llm_provider_slot_has_default(self) -> None:
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.LLM_PROVIDER)
        assert actual == _EXPECTED_LLM_PROVIDER_NAMES

    async def test_system_prompt_provider_slot_has_file_prompt(self) -> None:
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.SYSTEM_PROMPT_PROVIDER)
        assert actual == _EXPECTED_PROMPT_PROVIDER_NAMES

    async def test_interceptor_slot_has_tool_timeout(self) -> None:
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.INTERCEPTOR)
        assert actual == _EXPECTED_INTERCEPTOR_NAMES

    async def test_command_handler_slot_has_6_commands(self) -> None:
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.COMMAND_HANDLER)
        assert actual == _EXPECTED_COMMAND_NAMES, (
            f"COMMAND_HANDLER drift: missing={_EXPECTED_COMMAND_NAMES - actual}, "
            f"extra={actual - _EXPECTED_COMMAND_NAMES}"
        )

    async def test_capability_slot_has_bundled_packages(self) -> None:
        """CAPABILITY carries the FW-bundled capability packages — ``aci``,
        ``ast_grep``, ``experience``, ``subagents``, ``todo`` and
        ``tracing`` (ADR-0047; grows one package per migration wave)."""
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.CAPABILITY)
        assert actual == {"aci", "ast_grep", "experience", "skills", "subagents", "todo", "tracing"}, (
            f"CAPABILITY drift: {actual}"
        )

    # ---- Empty slots (FW design — bot plugin territory) -----------------

    async def test_execution_strategy_slot_is_empty(self) -> None:
        """EXECUTION_STRATEGY is empty — strategies come from BotStrategiesPlugin.

        The FW ships only the ``ExecutionStrategy`` ABC; ``react`` and
        ``external`` strategy implementations live in
        ``examples/bot_project/bot/service/`` and are registered by the
        business-layer ``BotStrategiesPlugin``, NOT by framework defaults.
        """
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.EXECUTION_STRATEGY)
        assert actual == set(), (
            f"EXECUTION_STRATEGY must be empty (bot plugin territory), got {actual}"
        )

    async def test_input_stage_slot_is_empty(self) -> None:
        """INPUT_STAGE is empty — stages come from the bot IM/WebUI pipeline.

        Input pipeline stages (environment control, session control, skill
        parsing, etc.) are business wiring in
        ``examples/bot_project/bot/input_pipeline/``, NOT framework
        defaults.
        """
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.INPUT_STAGE)
        assert actual == set(), f"INPUT_STAGE must be empty (bot IM plugin territory), got {actual}"

    async def test_data_namespace_slot_is_empty(self) -> None:
        """DATA_NAMESPACE is empty — no default data namespaces.

        Plugins register their own data-namespace models (the former
        default trigger configs were dead registrations and were removed).
        """
        registry = await _load_default_plugin()
        actual = _slot_names(registry, ComponentSlot.DATA_NAMESPACE)
        assert actual == set(), f"DATA_NAMESPACE must be empty, got {actual}"


# ---- Aggregate: all 11 slots accounted for ------------------------------


class TestAllSlotsAccounted:
    """The 7 populated + 4 empty slots cover all 11 ComponentSlot values.

    This is a structural assertion: every slot is either populated with a
    known name set or explicitly empty by design. No slot is left
    unaccounted.
    """

    async def test_populated_slots_count_is_7(self) -> None:
        registry = await _load_default_plugin()
        populated = {slot for slot in ComponentSlot if _slot_names(registry, slot)}
        assert len(populated) == 7, (
            f"Expected 7 populated slots, got {len(populated)}: {[s.value for s in populated]}"
        )

    async def test_empty_slots_are_exactly_the_4_designated(self) -> None:
        registry = await _load_default_plugin()
        empty = {slot for slot in ComponentSlot if not _slot_names(registry, slot)}
        expected_empty = {
            ComponentSlot.EXECUTION_STRATEGY,
            ComponentSlot.INPUT_STAGE,
            ComponentSlot.MEMORY_SYSTEM,
            ComponentSlot.DATA_NAMESPACE,
        }
        assert empty == expected_empty, (
            f"Empty slots drift: unexpected_empty="
            f"{[s.value for s in empty - expected_empty]}, "
            f"unexpected_populated="
            f"{[s.value for s in expected_empty - empty]}"
        )

    async def test_every_slot_resolves_without_error_for_populated_names(
        self,
    ) -> None:
        """Every expected name in a populated slot resolves via registry.resolve."""
        registry = await _load_default_plugin()
        checks: list[tuple[ComponentSlot, frozenset[str]]] = [
            (ComponentSlot.TOOL, _expected_tool_names()),
            (ComponentSlot.HOOK, _EXPECTED_HOOK_NAMES),
            (ComponentSlot.LLM_PROVIDER, _EXPECTED_LLM_PROVIDER_NAMES),
            (
                ComponentSlot.SYSTEM_PROMPT_PROVIDER,
                _EXPECTED_PROMPT_PROVIDER_NAMES,
            ),
            (ComponentSlot.INTERCEPTOR, _EXPECTED_INTERCEPTOR_NAMES),
            (ComponentSlot.COMMAND_HANDLER, _EXPECTED_COMMAND_NAMES),
            (
                ComponentSlot.CAPABILITY,
                frozenset({"aci", "ast_grep", "experience", "skills", "subagents", "todo", "tracing"}),
            ),
        ]
        for slot, names in checks:
            for name in names:
                factory = registry.resolve(slot, name)
                assert factory is not None, f"{name!r} not resolvable in {slot.value!r}"
