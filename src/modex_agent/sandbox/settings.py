"""Opt-in sandbox execution substrate and the two-class permission model.

``SandboxSettings`` declares *where* commands run (backend tier) and the
two-class tool permission face. It is the opt-in declaration consumed by
the assembly layer. ``DEFAULT`` leaves the substrate dormant: no sandbox
interceptor instance or engine probe. Independent approval, WebReader
safety, and native delegation checks still apply.

Permission model — exactly two classes, derived from the typed tool
catalog (``tool_matrix.permission_class``):

- ``parallel`` (the read-only family: read/ls/glob/grep/ast_grep_search/
  lsp_*/web_reader) is UNRESTRICTED by default. A per-tool
  ``parallel.boundaries`` entry narrows that one tool to the declared
  paths.
- ``exclusive`` (the read-write family: write/edit/aci_edit/
  ast_grep_replace/bash/bash_input/process) is bounded by the declared
  write surface: ``none`` refuses file-tool writes outright,
  ``workspace`` (the default) allows the workspace plus
  ``writable_roots``, ``roots`` allows only ``writable_roots`` (the
  workspace itself is NOT implicitly writable), ``full`` disables the
  boundary entirely. bash-class members have no path argument — the
  kernel substrate and the command-text guards bound them; per-tool
  path boundaries do not apply.

Path convention: every declared path is RELATIVE and anchors to the
live workspace root on each evaluation (multi-workspace deployments
switch roots without re-declaring). The workspace root itself is never
declared — it arrives through the workspace provider like every other
consumer.

Configuration semantics:

- ``backend`` selects the execution family: ``local`` (host view, kernel
  primitives) vs ``oci`` (clean container environment) vs ``host`` (native).
  LOCAL and OCI never fall back into each other. ``AUTO`` probes only LOCAL;
  confirmed pre-command unavailability permits reported HOST fallback.
- Explicit HOST keeps configured guards but provides no kernel file or
  network containment. Command-text checks cannot contain dynamic shell
  code.
- ``protected_subpaths`` defaults to ``[".git"]`` for known file-tool write
  checks and backend write restrictions inside writable roots. This does
  not contain dynamic HOST commands that modify repository metadata.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SandboxBackend(StrEnum):
    """Backend tier: where commands execute.

    ``DEFAULT`` is *not* a runtime tier: it means "unconfigured" and the
    assembly layer must leave the sandbox fully dormant (no components, no
    probes). It never appears as ``ResolvedSandbox.backend``.
    """

    DEFAULT = "default"  # Dormant substrate: no sandbox components or probes
    AUTO = "auto"  # Probe platform LOCAL engine; fall back to HOST if unavailable
    LOCAL = "local"  # Host-view family (bwrap / Seatbelt)
    OCI = "oci"  # Container family (Docker, then Podman, then HOST if unavailable)
    HOST = "host"  # Native execution with configured guards, no kernel isolation


class WriteSurface(StrEnum):
    """The exclusive class's writable file surface.

    ``none`` refuses every file-tool write (reads unaffected — the
    parallel class is governed by its own boundaries). ``workspace``
    allows the workspace plus ``writable_roots``. ``roots`` allows ONLY
    ``writable_roots`` — the workspace root is not implicitly writable.
    ``full`` disables the file boundary entirely (the dormancy escape
    hatch); per-tool boundaries are not built under it either.
    """

    NONE = "none"
    WORKSPACE = "workspace"
    ROOTS = "roots"
    FULL = "full"


class LocalSandboxEngine(StrEnum):
    """Concrete engine of the local family — platform-paired by definition."""

    BWRAP = "bwrap"  # Linux
    SEATBELT = "seatbelt"  # macOS


class OciEngine(StrEnum):
    """Concrete engine of the oci family (docker ∥ podman, one CLI surface)."""

    DOCKER = "docker"
    PODMAN = "podman"


class ToolPaths(BaseModel):
    """A per-tool boundary: the ONLY paths this tool may touch.

    Paths anchor to the live workspace root on every evaluation;
    containment uses the canonical boundary seam, so ``..`` segments and
    symlinked targets resolve to their real location before the check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    paths: tuple[Path, ...] = ()


class ParallelConfig(BaseModel):
    """The parallel (read-only) class face.

    Absent ``boundaries`` entries mean UNRESTRICTED — the class default.
    A ``boundaries`` entry narrows that single tool to the declared
    paths (a narrowing; it can never widen anything).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundaries: dict[str, ToolPaths] = Field(default_factory=dict)


class ExclusiveConfig(BaseModel):
    """The exclusive (read-write) class face.

    ``write_surface`` declares the writable file set; ``writable_roots``
    are the extra roots beyond (or, under ``roots``, instead of) the
    workspace. ``boundaries`` may narrow an individual path-argument
    write tool below the surface; bash-class members are not
    path-scopable and ignore them (the kernel substrate bounds them).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    write_surface: WriteSurface = WriteSurface.WORKSPACE
    writable_roots: list[Path] = Field(default_factory=list)
    boundaries: dict[str, ToolPaths] = Field(default_factory=dict)
    protected_subpaths: list[str] = Field(default_factory=lambda: [".git"])


class GuardSettings(BaseModel):
    """Guard chain toggles — which advisory and deny layers are active.

    The command deny rules (``CommandPatternGuard``'s
    ``_DEFAULT_DENY_RULES``) are DEPRECATED USAGE (not deleted): a
    shape-based command check whose interception role moved to the path
    boundary and the kernel substrate. ``deny_rules`` defaults False and
    flips them back on without touching their code. ``enabled`` gates
    the advisory network layer. ``read_only_bypass`` (default on) lets
    provably read-only commands skip the envelope/approval path like
    parallel tools — an approval fast path, not a security boundary
    (the kernel substrate owns containment); switch it off to restore
    the friction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    deny_rules: bool = False
    network: bool = True
    read_only_bypass: bool = True


class SandboxSettings(BaseModel):
    """Sandbox execution substrate declaration (frozen, extra=forbid).

    One shape for every agent — main or subagent. Subagents that do not
    declare a block inherit the caller's settings wholesale
    (``resolve_agent_sandbox``); a declared block is authoritative for
    the permission face (parallel/exclusive/guard) while the substrate
    face (backend/network/image) stays with the caller.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: SandboxBackend = SandboxBackend.DEFAULT
    parallel: ParallelConfig = Field(default_factory=ParallelConfig)
    exclusive: ExclusiveConfig = Field(default_factory=ExclusiveConfig)
    network: bool = False  # Request isolated networking; HOST cannot enforce it
    image: str | None = None  # OCI only; defaults to modex-sandbox:latest
    guard: GuardSettings = Field(default_factory=GuardSettings)


__all__ = [
    "ExclusiveConfig",
    "GuardSettings",
    "ParallelConfig",
    "SandboxBackend",
    "SandboxSettings",
    "ToolPaths",
    "WriteSurface",
]
