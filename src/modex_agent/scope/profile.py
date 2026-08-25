"""Scope profiles — named default-combination macros (SPEC §3.4, ticket 06).

A Profile is a frozen bundle of AgentSpec-field overrides building on
framework defaults ONLY (single level — a profile may never reference
another profile, SPEC §3.4 rule 1 / V7 / N10). Resolution semantics:

- **inheritance + deep merge (O1)** — the three-layer chain is
  ``framework default ← profile ← local declaration``; nested blocks
  (:class:`~modex_agent.scope.spec.MemoryDeclaration`) merge field-wise so
  a locally-declared nested field never silently drops profile-set siblings.
- **wholesale lists (O4/V8)** — a set ``tools`` list IS the whole list;
  item-wise add/remove stays the dedicated ``tool_supplements`` mechanism.
- **no nesting** — a profile referencing another profile is refused at the
  store boundary (loud), keeping the compiler's input single-level.

The FW standard profiles are code-level frozen constants: the five toolset
presets — the landing place of the dead legacy tool-preset field's values
(SPEC §3.4).
An agent's resolved toolset preset names the profile bound as its profile
layer (root position binds ``full``, non-root ``read_write``); the bound
profile's remaining fields contribute the profile layer. BIZ custom
profiles and boot loading of ``config/profiles/`` land with ticket 07.

The bill recomputes from the YAML declaration per request (P1) — there is
deliberately no boot-time profile cache.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from modex_agent.scope.spec import MemoryDeclaration, SessionMemoryOverride
from modex_agent.scope.validator import ProfileDeclaration
from modex_agent.tools.presets import ToolPreset, ToolSupplement


class Profile(BaseModel):
    """One named default-combination macro (SPEC §3.4).

    Every field is an override: ``None``/unset = inherit the framework
    default. A set list field is the WHOLE list (O4/V8).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    profile: str | None = None
    """Base profile reference — must stay ``None`` (single level, V7/N10).
    Representable so violations surface; :class:`ProfileStore` refuses to
    carry them."""
    toolset: ToolPreset | None = None
    """The toolset preset this macro is named after (the replacement for
    the dead legacy tool-preset field — a toolset-carrying profile does
    everything the field did)."""
    tools: list[str] | None = None
    """Wholesale tool list — ``None`` defers to the toolset preset."""
    tool_supplements: list[ToolSupplement] | None = None
    eager: bool | None = None
    max_steps: int | None = None
    memory: MemoryDeclaration | None = None


class ProfileStore(BaseModel):
    """The loaded profile storage: name → Profile (``config/profiles/``).

    FW standard content by default; ticket 07's boot wiring merges BIZ
    profiles in. The store is the compiler's trust boundary — it refuses
    nested profile references (V7 semantics enforced at construction).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profiles: dict[str, Profile]

    @model_validator(mode="after")
    def _validate_single_level(self) -> ProfileStore:
        nested = [p.name for p in self.profiles.values() if p.profile is not None]
        if nested:
            raise ValueError(
                f"profile(s) {nested!r} reference a base profile — profiles "
                f"may build on framework defaults only (single-level "
                f"references, SPEC §3.4 rule 1 / V7); inline the referenced "
                f"overrides instead"
            )
        return self

    def get(self, name: str) -> Profile | None:
        """The profile stored under ``name``, or ``None`` when absent."""
        return self.profiles.get(name)

    def declarations(self) -> list[ProfileDeclaration]:
        """The whole store projected onto the V7 validator input face."""
        return [
            ProfileDeclaration(name=p.name, profile=p.profile)
            for p in self.profiles.values()
        ]


FULL_PROFILE = Profile(name=ToolPreset.FULL.value, toolset=ToolPreset.FULL)
READ_WRITE_PROFILE = Profile(name=ToolPreset.READ_WRITE.value, toolset=ToolPreset.READ_WRITE)
READ_ONLY_PROFILE = Profile(name=ToolPreset.READ_ONLY.value, toolset=ToolPreset.READ_ONLY)
NONE_PROFILE = Profile(name=ToolPreset.NONE.value, toolset=ToolPreset.NONE)
WEB_PROFILE = Profile(name=ToolPreset.WEB.value, toolset=ToolPreset.WEB)

STANDARD_PROFILES = ProfileStore(
    profiles={
        profile.name: profile
        for profile in (
            FULL_PROFILE,
            READ_WRITE_PROFILE,
            READ_ONLY_PROFILE,
            NONE_PROFILE,
            WEB_PROFILE,
        )
    }
)
"""FW standard profiles — the five toolset presets as code-level frozen
constants (SPEC §3.4: the dead legacy tool-preset values land here)."""


def merge_memory_declarations(
    profile_memory: MemoryDeclaration | None,
    local_memory: MemoryDeclaration | None,
) -> MemoryDeclaration | None:
    """Deep-merge the profile and local memory blocks (SPEC §3.4 O1).

    Locally-declared fields win; undeclared fields inherit the profile's
    values — sibling fields are never silently dropped (the whole-row
    replacement failure mode the SPEC rejects). ``None`` on either side
    passes the other through. The merged result is re-validated, so a
    field-wise merge that produces core-without-archive across layers
    fails loudly.
    """
    if profile_memory is None:
        return local_memory
    if local_memory is None:
        return profile_memory
    merged = profile_memory.model_dump()
    local_dump = local_memory.model_dump()
    for field in local_memory.model_fields_set:
        if (
            field == "session"
            and profile_memory.session is not None
            and local_memory.session is not None
        ):
            merged["session"] = _merge_session_overrides(
                profile_memory.session, local_memory.session
            )
        else:
            merged[field] = local_dump[field]
    return MemoryDeclaration.model_validate(merged)


def _merge_session_overrides(
    profile_session: SessionMemoryOverride,
    local_session: SessionMemoryOverride,
) -> SessionMemoryOverride:
    """Field-wise session merge — an unset local threshold keeps the
    profile's; a set local threshold wins."""
    merged = profile_session.model_dump()
    local_dump = local_session.model_dump()
    for field in local_session.model_fields_set:
        merged[field] = local_dump[field]
    return SessionMemoryOverride.model_validate(merged)
