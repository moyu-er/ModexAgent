"""Ticket 06 — Profile resolution semantics (SPEC §3.4).

A Profile is a named default-combination macro building on framework
defaults only: inheritance + deep merge (O1), single-level references
(V7/N10 — a profile may never reference another profile), wholesale list
replacement (O4/V8 — a set ``tools`` list IS the whole list;
``tool_supplements`` is the dedicated additive mechanism). The compiler's
three-layer chain is ``framework default ← profile ← local declaration``.

The FW standard profiles are code-level frozen constants (the five toolset
presets — the landing place of the dead ``tool_preset`` values, SPEC §3.4).
BIZ custom profiles and boot loading land with ticket 07.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.scope.profile import (
    STANDARD_PROFILES,
    Profile,
    ProfileStore,
    merge_memory_declarations,
)
from modex_agent.scope.spec import (
    MemoryDeclaration,
    SessionMemoryOverride,
)
from modex_agent.scope.validator import ProfileDeclaration
from modex_agent.tools.presets import ToolPreset


class TestProfileType:
    def test_profile_is_frozen_and_closed(self) -> None:
        profile = Profile(name="p")
        with pytest.raises(ValidationError):
            Profile(name="p", unknown_field=1)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            profile.name = "other"

    def test_default_face_is_all_unset(self) -> None:
        # Every field None/absent = "no override, inherit framework default".
        profile = Profile(name="p")
        assert profile.profile is None
        assert profile.toolset is None
        assert profile.tools is None
        assert profile.tool_supplements is None
        assert profile.eager is None
        assert profile.max_steps is None
        assert profile.memory is None

    def test_base_reference_field_is_representable(self) -> None:
        # A violating `profile:` reference must be representable on the
        # declaration face (V7's input) — the STORE refuses to carry it.
        assert Profile(name="p", profile="other").profile == "other"


class TestStandardProfiles:
    def test_five_toolset_presets_shipped(self) -> None:
        # The dead tool_preset values land as FW standard profiles: one per
        # ToolPreset member, each carrying its preset (SPEC §3.4).
        expected = {preset.value for preset in ToolPreset}
        assert set(STANDARD_PROFILES.profiles) == expected
        for preset in ToolPreset:
            profile = STANDARD_PROFILES.profiles[preset.value]
            assert profile.toolset is preset
            assert profile.profile is None

    def test_standard_profiles_carry_no_extras(self) -> None:
        # FW standard profiles are toolset macros only — the other position
        # dimensions (memory family, registration) stay framework machinery.
        for profile in STANDARD_PROFILES.profiles.values():
            assert profile.tools is None
            assert profile.tool_supplements is None
            assert profile.eager is None
            assert profile.max_steps is None
            assert profile.memory is None


class TestProfileStore:
    def test_get_hit_and_miss(self) -> None:
        store = ProfileStore(profiles={"p": Profile(name="p")})
        assert store.get("p") is not None
        assert store.get("p").name == "p"
        assert store.get("missing") is None

    def test_store_refuses_nested_profile_references(self) -> None:
        # Single level (SPEC §3.4 rule 1 / V7): a profile referencing another
        # profile cannot even enter the store — the compiler's trust boundary.
        with pytest.raises(ValidationError, match="single-level"):
            ProfileStore(
                profiles={"p": Profile(name="p", profile="q")},
            )

    def test_declarations_project_v7_input_face(self) -> None:
        # The V7 validator input face: name + optional base reference.
        store = ProfileStore(
            profiles={
                "a": Profile(name="a"),
                "b": Profile(name="b"),
            }
        )
        assert store.declarations() == [
            ProfileDeclaration(name="a"),
            ProfileDeclaration(name="b"),
        ]


class TestMemoryDeepMerge:
    """``merge_memory_declarations`` — the O1 deep-merge semantics.

    A locally-declared nested field must never silently drop the profile's
    sibling fields (the whole-row replacement failure mode the SPEC
    rejects). Locally-set fields win; unset fields inherit the profile.
    """

    def test_none_none_is_none(self) -> None:
        assert merge_memory_declarations(None, None) is None

    def test_profile_only_passes_through(self) -> None:
        profile = MemoryDeclaration(archive_enabled=True)
        assert merge_memory_declarations(profile, None) == profile

    def test_local_only_passes_through(self) -> None:
        local = MemoryDeclaration(archive_enabled=True, core_enabled=True)
        assert merge_memory_declarations(None, local) == local

    def test_local_fields_win_over_profile(self) -> None:
        profile = MemoryDeclaration(archive_enabled=True)
        local = MemoryDeclaration(archive_enabled=False)
        merged = merge_memory_declarations(profile, local)
        assert merged is not None
        assert merged.archive_enabled is False

    def test_local_block_keeps_profile_sibling_fields(self) -> None:
        # The money case: local declares archive+core; the profile's session
        # override survives (deep merge, not whole-block replacement).
        profile = MemoryDeclaration(
            archive_enabled=True,
            session=SessionMemoryOverride(max_context_tokens=32000),
        )
        local = MemoryDeclaration(archive_enabled=True, core_enabled=True)
        merged = merge_memory_declarations(profile, local)
        assert merged is not None
        assert merged.archive_enabled is True
        assert merged.core_enabled is True
        assert merged.session is not None
        assert merged.session.max_context_tokens == 32000

    def test_session_overrides_merge_fieldwise(self) -> None:
        # An empty local session block does not drop the profile's threshold;
        # a set local threshold wins.
        profile = MemoryDeclaration(
            session=SessionMemoryOverride(max_context_tokens=32000)
        )
        empty_local = MemoryDeclaration(session=SessionMemoryOverride())
        kept = merge_memory_declarations(profile, empty_local)
        assert kept is not None
        assert kept.session is not None
        assert kept.session.max_context_tokens == 32000

        set_local = MemoryDeclaration(
            session=SessionMemoryOverride(max_context_tokens=5000)
        )
        overridden = merge_memory_declarations(profile, set_local)
        assert overridden is not None
        assert overridden.session is not None
        assert overridden.session.max_context_tokens == 5000

    def test_merged_and_gate_violation_is_loud(self) -> None:
        # Field-wise merge can produce core-without-archive across layers
        # (profile core, local archive=False) — re-validation fails loudly.
        profile = MemoryDeclaration(archive_enabled=True, core_enabled=True)
        local = MemoryDeclaration(archive_enabled=False)
        with pytest.raises(ValidationError):
            merge_memory_declarations(profile, local)
