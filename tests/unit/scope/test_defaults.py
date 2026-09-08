"""Ticket 02 — position-derived defaults table (SPEC §3.2), row by row.

Root = archive/core memory eligibility + approval eligibility + 
eager registration + toolset profile ``full``. Non-root = session-only
memory + lazy materialization + toolset profile ``read-write``. Every
default yields to the node's own declaration (framework default < node
local declaration). ``tool_preset``'s values land here as position-derived
profiles — split-brain parity with the legacy Main/Sub type defaults.
"""

from __future__ import annotations

from modex_agent.scope import (
    AgentSpec,
    MemoryDeclaration,
    MemoryPreset,
    RegistrationTiming,
    defaults_for_position,
    effective_defaults,
    memory_config_for_position,
)
from modex_agent.tools.presets import ToolPreset


class TestDefaultsForPosition:
    def test_root_defaults_row_by_row(self) -> None:
        d = defaults_for_position(is_root=True)
        assert d.memory_preset is MemoryPreset.ARCHIVE_CORE
        assert d.approval_eligible is True
        assert d.registration is RegistrationTiming.EAGER
        assert d.toolset_profile is ToolPreset.FULL

    def test_non_root_defaults_row_by_row(self) -> None:
        d = defaults_for_position(is_root=False)
        assert d.memory_preset is MemoryPreset.SESSION_ONLY
        assert d.approval_eligible is False
        assert d.registration is RegistrationTiming.LAZY
        assert d.toolset_profile is ToolPreset.READ_WRITE

    def test_root_memory_layers_default(self) -> None:
        # Eligible family, layers off by default (legacy MemoryToggle()
        # default).
        d = defaults_for_position(is_root=True)
        assert d.archive_enabled is False
        assert d.core_enabled is False

    def test_non_root_memory_layers_default(self) -> None:
        d = defaults_for_position(is_root=False)
        assert d.archive_enabled is False
        assert d.core_enabled is False

    def test_intermediate_node_follows_non_root_position(self) -> None:
        # Defaults derive from POSITION (parent or not), not depth — a
        # middle node of a 3-level tree is a non-root.
        mid = AgentSpec(name="mid", parent="main")
        d = effective_defaults(mid)
        assert d.memory_preset is MemoryPreset.SESSION_ONLY
        assert d.registration is RegistrationTiming.LAZY
        assert d.toolset_profile is ToolPreset.READ_WRITE
        assert d.approval_eligible is False


class TestEffectiveDefaultsOverrides:
    def test_toolset_override_beats_position_default(self) -> None:
        agent = AgentSpec(name="explore", parent="main", toolset=ToolPreset.READ_ONLY)
        d = effective_defaults(agent)
        assert d.toolset_profile is ToolPreset.READ_ONLY  # node declaration wins
        assert d.registration is RegistrationTiming.LAZY  # position default stands

    def test_eager_override_on_non_root(self) -> None:
        agent = AgentSpec(name="warm", parent="main", eager=True)
        assert effective_defaults(agent).registration is RegistrationTiming.EAGER

    def test_lazy_override_on_root(self) -> None:
        agent = AgentSpec(name="cold", eager=False)
        assert effective_defaults(agent).registration is RegistrationTiming.LAZY

    def test_memory_toggle_override_on_root(self) -> None:
        agent = AgentSpec(name="main", memory=MemoryDeclaration(archive_enabled=True))
        d = effective_defaults(agent)
        # node declaration beats framework default (archive off)
        assert d.archive_enabled is True
        assert d.memory_preset is MemoryPreset.ARCHIVE_CORE

    def test_non_root_memory_block_cannot_flip_preset(self) -> None:
        # SPEC §3.2: non-root = session-only. The block's session-override
        # face rides on AgentSpec.memory; layer toggles cannot opt a
        # non-root into the archive/core family.
        agent = AgentSpec(
            name="sub", parent="main", memory=MemoryDeclaration(archive_enabled=True)
        )
        d = effective_defaults(agent)
        assert d.memory_preset is MemoryPreset.SESSION_ONLY
        assert d.archive_enabled is False

    def test_no_overrides_equals_position_defaults(self) -> None:
        root = AgentSpec(name="main")
        assert effective_defaults(root) == defaults_for_position(is_root=True)
        sub = AgentSpec(name="sub", parent="main")
        assert effective_defaults(sub) == defaults_for_position(is_root=False)


class TestToolPresetLanding:
    """Split-brain parity: position-derived profiles ≡ legacy type defaults."""

    def test_root_profile_matches_legacy_main_default(self) -> None:
        # MainAgentSpec.tool_preset default == ToolPreset.FULL
        assert defaults_for_position(is_root=True).toolset_profile is ToolPreset.FULL

    def test_non_root_profile_matches_legacy_sub_default(self) -> None:
        # SubagentSpec.tool_preset default == ToolPreset.READ_WRITE
        assert defaults_for_position(is_root=False).toolset_profile is ToolPreset.READ_WRITE


class TestMemoryConfigForPosition:
    """Ticket 09 — the concrete MemoryConfig of a position-derived row.

    Split-brain parity: the root family reproduces ``main_agent_memory``
    parameterized by the resolved archive/core toggles + the session
    threshold (node ``memory:`` override, else the boot-injected model
    window); the non-root family reproduces ``subagent_memory`` with the
    session override applied.
    """

    def test_root_family_matches_main_agent_memory(self) -> None:
        from modex_agent.memory.presets import main_agent_memory

        root = defaults_for_position(is_root=True)
        assert memory_config_for_position(
            root, session_max_context_tokens=200000
        ) == main_agent_memory(
            max_context_tokens=200000,
            archive_enabled=False,
            core_enabled=False,
        )

    def test_root_family_applies_resolved_toggles(self) -> None:
        from modex_agent.memory.presets import main_agent_memory

        root = effective_defaults(
            AgentSpec(name="main", memory=MemoryDeclaration(archive_enabled=True, core_enabled=True))
        )
        assert memory_config_for_position(root) == main_agent_memory(
            archive_enabled=True, core_enabled=True
        )

    def test_session_only_family_matches_subagent_memory(self) -> None:
        from modex_agent.memory.presets import subagent_memory

        sub = defaults_for_position(is_root=False)
        assert memory_config_for_position(sub) == subagent_memory()

    def test_session_only_family_applies_session_override(self) -> None:
        from modex_agent.memory.presets import subagent_memory

        sub = defaults_for_position(is_root=False)
        expected = subagent_memory().model_copy(
            update={
                "session": subagent_memory().session.model_copy(
                    update={"max_context_tokens": 32000}
                )
            }
        )
        assert memory_config_for_position(
            sub, session_max_context_tokens=32000
        ) == expected
