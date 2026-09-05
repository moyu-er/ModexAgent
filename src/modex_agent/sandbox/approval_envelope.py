"""Validate per-tool prompt exemptions against declared sandbox roots.

Approval exemptions do not expand the permission boundary. Concrete roots
must fit the envelope; universal patterns still leave runtime guards active.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.sandbox.settings import SandboxPolicy
from modex_agent.workspace.boundary import PathEnvelope

if TYPE_CHECKING:
    from modex_agent.ioc.configs.approval import ApprovalConfig
    from modex_agent.sandbox.settings import SandboxSettings
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

__all__ = ["validate_approval_envelope"]

_UNIVERSAL_PATTERNS = frozenset({"*", "**"})


def approval_envelope(
    settings: SandboxSettings, root_provider: WorkspaceRootProvider
) -> PathEnvelope | None:
    """The canonical sandbox envelope; None = no envelope.

    The PRD formula: workspace root + ``writable_roots`` (under every
    policy — READ_ONLY still read-allows those roots, so an approval
    allowance there is consistent; DANGER_FULL_ACCESS declares no
    envelope at all). Roots canonicalize through the boundary seam, so
    relative/symlinked/case-variant roots compare correctly.
    """
    if settings.policy is SandboxPolicy.DANGER_FULL_ACCESS:
        return None
    workspace_root = root_provider.current()
    return PathEnvelope(
        (workspace_root, *settings.writable_roots), base=workspace_root
    )


def _strip_glob(pattern: str) -> str:
    p = pattern.strip()
    if p.endswith("/**"):
        p = p[:-3]
    elif p.endswith("/*"):
        p = p[:-2]
    elif p.endswith("*"):
        p = p[:-1]
    return p.rstrip("/") or "."


def validate_approval_envelope(
    cfg: ApprovalConfig | None,
    *,
    settings: SandboxSettings,
    root_provider: WorkspaceRootProvider,
) -> None:
    """Validate concrete approval ``allowed_paths`` roots at assembly time.

    Each concrete root must resolve inside workspace + ``writable_roots``.
    These are prompt exemptions, not grants of filesystem access.
    Containment is the canonical :class:`PathEnvelope` check (relative
    patterns anchor to the live workspace root, symlinks resolve to their
    real targets, cross-drive is a typed denial); pattern resolution
    follows ``ArgumentMatcher`` semantics (trailing ``/*`` / ``/**`` /
    ``*`` glob markers strip to the directory root).

    Skipped when the approval config is None/disabled/no-op (nothing
    gated) or the policy declares no envelope (DANGER_FULL_ACCESS).
    Universal patterns (``*`` / ``**``) pass this containment validation;
    runtime guard judgments remain independent of prompt exemptions.

    Raises:
        ValueError: one or more allowed_paths roots resolve outside the
            envelope (fail-fast at assembly).
    """
    if cfg is None or not cfg.enabled or not cfg.tools:
        return
    envelope = approval_envelope(settings, root_provider)
    if envelope is None or not envelope.roots:
        return
    workspace_root = root_provider.current()
    outside: list[str] = []
    for name, entry in cfg.tools.items():
        for pattern in entry.allowed_paths:
            stripped = pattern.strip()
            if stripped in _UNIVERSAL_PATTERNS or stripped == "":
                continue
            root = _strip_glob(stripped)
            if envelope.contains(root, base=workspace_root):
                continue
            outside.append(f"{name}:{pattern}")
    if outside:
        listed = ", ".join(outside)
        raise ValueError(
            f"approval allowed_paths escape the sandbox envelope "
            f"({', '.join(str(r) for r in envelope.roots)}): {listed} — an approval "
            f"allowance wider than the sandbox boundary produces "
            f"approved-but-denied decisions; fix the allowed_paths or "
            f"extend writable_roots"
        )
