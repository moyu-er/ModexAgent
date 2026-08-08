"""Tests for baked memory + experience presets (``bot.config.memory_defaults``).

Covers the ``archive_enabled`` / ``core_enabled`` toggle surface added to
``main_agent_memory()`` and the unchanged ``subagent_memory()`` preset.

The AND gate (``core_enabled`` requires ``archive_enabled``) lives at the
schema layer (:class:`modex_agent.multi_agent.pool_config.specs.MemoryToggle`
validator), NOT at this preset layer. ``main_agent_memory()`` performs no
validation — it merely constructs the requested configs. A caller that passes
``core_enabled=True, archive_enabled=False`` gets a ``core`` config with
``archive=None``; preventing that combination is the schema validator's job.
"""

from __future__ import annotations

from bot.config.memory_defaults import main_agent_memory, subagent_memory

from modex_agent.ioc.configs.memory import MemoryConfig

# ---------------------------------------------------------------------------
# subagent_memory — unchanged by Task 2
# ---------------------------------------------------------------------------


def test_subagent_memory_is_minimal_and_matches_session_only() -> None:
    m: MemoryConfig = subagent_memory()
    assert m.session is not None
    assert m.archive is None or m.archive.enabled is False
    assert m.core is None or m.core.enabled is False
    assert m.dream_engine is None
    assert m.pruned is not None and m.pruned.enabled is True
    assert m.governance is not None and m.governance.tool_chain_repair is True


# ---------------------------------------------------------------------------
# main_agent_memory — defaults (Task 2 contract: identical to pre-Task-2)
# ---------------------------------------------------------------------------


def test_main_memory_defaults_all_long_term_layers_off() -> None:
    """Default params produce archive=None, core=None, dream_engine=None."""
    m: MemoryConfig = main_agent_memory()
    assert m.archive is None
    assert m.core is None
    assert m.dream_engine is None
    assert m.compact is not None and m.compact.enabled is True
    assert m.pruned is not None and m.pruned.enabled is True
    assert m.governance is not None
    assert m.governance.tool_chain_repair is True
    assert m.governance.budget is not None


def test_main_memory_defaults_byte_for_byte_identical_to_no_params() -> None:
    """Explicit defaults must equal implicit defaults — no hidden branching."""
    explicit = main_agent_memory(archive_enabled=False, core_enabled=False)
    implicit = main_agent_memory()
    assert explicit.model_dump() == implicit.model_dump()


# ---------------------------------------------------------------------------
# main_agent_memory — archive_enabled=True only
# ---------------------------------------------------------------------------


def test_main_memory_archive_only() -> None:
    m: MemoryConfig = main_agent_memory(archive_enabled=True)
    assert m.archive is not None
    assert m.archive.enabled is True
    assert m.core is None
    assert m.dream_engine is None


# ---------------------------------------------------------------------------
# main_agent_memory — archive_enabled=True, core_enabled=True (both on)
# ---------------------------------------------------------------------------


def test_main_memory_archive_and_core_enables_dream() -> None:
    m: MemoryConfig = main_agent_memory(archive_enabled=True, core_enabled=True)
    assert m.archive is not None and m.archive.enabled is True
    assert m.core is not None and m.core.enabled is True
    assert m.dream_engine is not None
    assert m.dream_engine.enabled is True


# ---------------------------------------------------------------------------
# main_agent_memory — core_enabled=True, archive_enabled=False
#
# This combination is REJECTED at the schema layer by MemoryToggle's validator
# (core_enabled requires archive_enabled). main_agent_memory() itself does NOT
# validate — it just constructs. Documenting the expected (unvalidated) behavior
# here so the boundary between schema-layer gating and preset-layer construction
# stays explicit.
# ---------------------------------------------------------------------------


def test_main_memory_core_without_archive_is_unvalidated_preset_layer() -> None:
    """Preset layer does not gate; schema layer (MemoryToggle) does.

    Calling ``main_agent_memory(core_enabled=True, archive_enabled=False)``
    directly yields ``core`` enabled with ``archive=None`` and ``dream=None``.
    This is intentional: the AND gate belongs to ``MemoryToggle``'s validator,
    not this constructor. A correct caller never reaches here because
    ``MemoryToggle`` rejects the combination upstream.
    """
    m: MemoryConfig = main_agent_memory(core_enabled=True, archive_enabled=False)
    assert m.archive is None
    assert m.core is not None and m.core.enabled is True
    assert m.dream_engine is None
