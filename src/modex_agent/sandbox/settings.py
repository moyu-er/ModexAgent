"""Opt-in sandbox execution substrate and guard configuration.

``SandboxSettings`` declares *where* commands run (backend tier) and the
file/network boundary policy. It is the opt-in declaration consumed by the
assembly layer. ``DEFAULT`` leaves the substrate dormant: no sandbox
interceptor instance or engine probe. Independent approval, WebReader safety,
and native delegation checks still apply.

Configuration semantics:

- ``backend`` selects the execution family: ``local`` (host view, kernel
  primitives) vs ``oci`` (clean container environment) vs ``host`` (native).
  LOCAL and OCI never fall back into each other. ``AUTO`` probes only LOCAL;
  confirmed pre-command unavailability permits reported HOST fallback.
- ``policy`` is independent of the backend selection. Explicit HOST keeps
  configured guards but provides no kernel file or network containment.
  Command-text checks cannot contain dynamic shell code.
- ``protected_subpaths`` defaults to ``[".git"]`` for known file-tool checks
  and backend write restrictions inside writable roots. This does not contain
  dynamic HOST commands that modify repository metadata.
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


class SandboxPolicy(StrEnum):
    """File policy; network access is a separate setting.

    READ_ONLY restricts writes, not all reads to the workspace. Effective
    kernel restrictions depend on the backend; HOST guards are best effort.
    """

    READ_ONLY = "read-only"  # Deny known file-tool writes; backend limits vary
    WORKSPACE_WRITE = "workspace-write"  # Writable roots with protected subpaths
    DANGER_FULL_ACCESS = "danger-full-access"  # No file boundary; other guards remain


class LocalSandboxEngine(StrEnum):
    """Concrete engine of the local family — platform-paired by definition."""

    BWRAP = "bwrap"  # Linux
    SEATBELT = "seatbelt"  # macOS


class OciEngine(StrEnum):
    """Concrete engine of the oci family (docker ∥ podman, one CLI surface)."""

    DOCKER = "docker"
    PODMAN = "podman"


class GuardSettings(BaseModel):
    """Guard chain toggles — which advisory checks are active.

    The built-in deny rules (destructive, fork bomb, system power —
    ``CommandPatternGuard``'s ``_DEFAULT_DENY_RULES``) are structural:
    they always run under any explicit backend and no switch here can
    disable them. ``enabled`` gates only the advisory layers
    (path traversal, network); there is deliberately no toggle for the
    deny rules themselves.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    path_traversal: bool = True
    network: bool = True


class SandboxSettings(BaseModel):
    """Sandbox execution substrate declaration (frozen, extra=forbid)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: SandboxBackend = SandboxBackend.DEFAULT
    policy: SandboxPolicy = SandboxPolicy.DANGER_FULL_ACCESS
    network: bool = False  # Request isolated networking; HOST cannot enforce it
    writable_roots: list[Path] = Field(default_factory=list)  # Extra roots beyond workspace
    protected_subpaths: list[str] = Field(default_factory=lambda: [".git"])
    image: str | None = None  # OCI only; defaults to modex-sandbox:latest
    guard: GuardSettings = Field(default_factory=GuardSettings)


__all__ = [
    "GuardSettings",
    "SandboxBackend",
    "SandboxPolicy",
    "SandboxSettings",
]
