"""MemoryToggle + MainAgentSpec.memory field tests.

``MemoryToggle`` is a frozen Pydantic ``BaseModel`` (``extra="forbid"``) that
gates the archive/core memory layers on a main agent. Default
``MemoryToggle()`` is fully off — byte-for-byte identical to the pre-field
behavior. The cross-field rule ``core_enabled and not archive_enabled`` raises
``ValidationError``: core memory requires archive to feed it.

Only :class:`MainAgentSpec` carries the field; :class:`SubagentSpec` does not.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.multi_agent.pool_config.specs import (
    MainAgentSpec,
    MemoryToggle,
    SubagentSpec,
)


class TestMemoryToggleDefaults:
    def test_defaults_both_off(self) -> None:
        toggle = MemoryToggle()
        assert toggle.archive_enabled is False
        assert toggle.core_enabled is False

    def test_default_factory_yields_fresh_instance(self) -> None:
        # Each call to the factory must produce a distinct frozen instance.
        a = MemoryToggle()
        b = MemoryToggle()
        assert a == b
        assert a is not b


class TestMemoryToggleValidation:
    def test_core_without_archive_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryToggle(core_enabled=True, archive_enabled=False)

    def test_archive_only_valid(self) -> None:
        toggle = MemoryToggle(archive_enabled=True, core_enabled=False)
        assert toggle.archive_enabled is True
        assert toggle.core_enabled is False

    def test_both_on_valid(self) -> None:
        toggle = MemoryToggle(archive_enabled=True, core_enabled=True)
        assert toggle.archive_enabled is True
        assert toggle.core_enabled is True

    def test_both_off_explicit_valid(self) -> None:
        toggle = MemoryToggle(archive_enabled=False, core_enabled=False)
        assert toggle.archive_enabled is False
        assert toggle.core_enabled is False


class TestMemoryToggleStrictness:
    def test_frozen(self) -> None:
        toggle = MemoryToggle()
        with pytest.raises(ValidationError):
            toggle.archive_enabled = True  # type: ignore[misc]

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            MemoryToggle(unknown="x")  # type: ignore[call-arg]

    def test_round_trip_via_model_dump_model_validate(self) -> None:
        toggle = MemoryToggle(archive_enabled=True, core_enabled=True)
        dumped = toggle.model_dump()
        assert dumped == {"archive_enabled": True, "core_enabled": True}
        restored = MemoryToggle.model_validate(dumped)
        assert restored == toggle


class TestMainAgentSpecMemoryField:
    def test_defaults_to_memory_toggle(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        assert isinstance(spec.memory, MemoryToggle)
        assert spec.memory.archive_enabled is False
        assert spec.memory.core_enabled is False

    def test_accepts_explicit_toggle(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            memory=MemoryToggle(archive_enabled=True, core_enabled=True),
        )
        assert spec.memory.archive_enabled is True
        assert spec.memory.core_enabled is True

    def test_round_trip_preserves_memory(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            memory=MemoryToggle(archive_enabled=True, core_enabled=True),
        )
        dumped = spec.model_dump()
        assert dumped["memory"] == {
            "archive_enabled": True,
            "core_enabled": True,
        }
        restored = MainAgentSpec.model_validate(dumped)
        assert restored.memory == spec.memory

    def test_default_round_trip_preserves_off_state(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        dumped = spec.model_dump()
        assert dumped["memory"] == {
            "archive_enabled": False,
            "core_enabled": False,
        }
        restored = MainAgentSpec.model_validate(dumped)
        assert restored.memory.archive_enabled is False
        assert restored.memory.core_enabled is False

    def test_invalid_memory_propagates_validation_error(self) -> None:
        # core=True without archive=True must surface as a ValidationError at
        # the parent level (nested model validator propagates).
        with pytest.raises(ValidationError):
            MainAgentSpec(
                agent_name="main",
                memory=MemoryToggle(archive_enabled=False, core_enabled=True),
            )


class TestSubagentSpecNoMemoryField:
    def test_subagent_has_no_memory_field(self) -> None:
        # SubagentSpec must NOT carry the memory field — subagents are
        # session-only by construction (build_session_only_memory).
        assert "memory" not in SubagentSpec.model_fields

    def test_subagent_rejects_memory_kwarg(self) -> None:
        with pytest.raises(ValidationError):
            SubagentSpec(agent_name="worker", memory=MemoryToggle())  # type: ignore[call-arg]
