"""SecurityDecisionService — the single security-verdict implementation.

Owns command-pattern, path-boundary and SSRF checks, plus the
two-class file-tool permission projection. The approval
classifier and execution interceptor share this implementation and root
semantics, not necessarily the same service instance. Enablement toggles
are independent; classification and guard contracts are shared.

The service returns typed facts; presentation layers render actionable
denials. Category semantics live in ``verdict.py``.

Each boundary evaluation reads ``workspace_root_provider.current()``.
Main-agent workspace switches affect subsequent judgments; delegated native
instances use a provider fixed to their frozen permission snapshot.

File-tool judgment follows the two-class model (``tool_matrix``):

- ``parallel`` (read-only family) is CLEAN by default — unrestricted.
  A per-tool ``parallel.boundaries`` entry narrows that tool to the
  declared paths (anchored live).
- ``exclusive`` (read-write family) is bounded by the declared write
  surface: ``workspace`` → workspace + writable_roots, ``roots`` →
  writable_roots only, ``none`` → every write refused, ``full`` → no
  boundary built at all. A per-tool ``exclusive.boundaries`` entry
  replaces the write set for that one tool (validated inside the
  ceiling at assembly).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

from modex_agent.sandbox.guard import CommandGuard, CommandPatternGuard, GuardResult
from modex_agent.sandbox.guard_network import NetworkGuard, NetworkGuardConfig
from modex_agent.sandbox.guard_path import PathBoundaryConfig, PathBoundaryGuard
from modex_agent.sandbox.guard_pipeline import GuardPipeline
from modex_agent.sandbox.readonly import classify_readonly, resolve_command_family
from modex_agent.sandbox.settings import (
    SandboxSettings,
    WriteSurface,
)
from modex_agent.sandbox.tool_matrix import (
    ToolEffect,
    approval_anchor,
    describe_tool_security,
    extract_call_target,
)
from modex_agent.sandbox.verdict import GuardCategory, GuardVerdict
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.boundary import (
    PathCanonicalizationError,
    PathEnvelope,
    canonicalize_path,
)

__all__ = [
    "GuardCategory",
    "GuardVerdict",
    "SecurityDecisionService",
    "approval_anchor",
]

# GuardMatch categories the verdict mapping keys on (guard.py /
# guard_path.py). Everything else is a deny rule.
_BOUNDARY_CATEGORY = "path_boundary"


class SecurityDecisionService:
    """Shared security decisions for classification and execution checks.

    Construction injects the declared settings + the live workspace-root
    provider. The static guard layers (command-pattern deny rules, SSRF)
    compile once — they are root-independent. The
    root-dependent projections (the ``PathBoundaryGuard`` command
    envelope and the file-tool write/read surfaces) are rebuilt from
    ``root_provider.current()`` on EVERY evaluation, so a workspace
    switch updates them immediately and every declared relative path
    re-anchors to the live root.

    Under ``write_surface=full`` no file boundary is built (not
    ``enforce=False`` — the check simply does not exist).
    """

    def __init__(
        self,
        settings: SandboxSettings,
        workspace_root_provider: WorkspaceRootProvider,
    ) -> None:
        self._settings = settings
        self._root_provider = workspace_root_provider
        self._static_pipeline = self._build_static_pipeline()
        self._network_guard = self._build_network_guard()
        # The shell family that will interpret command strings — resolved
        # once with the executor's own ladder (executor and judge share
        # the same primitives, so they cannot diverge).
        self._command_family = resolve_command_family(settings.backend)

    # -- construction ------------------------------------------------------

    def _build_static_pipeline(self) -> GuardPipeline:
        """Compile the root-independent guard chain (deny rules only).

        The command deny rules are DEPRECATED USAGE (not deleted):
        interception is owned by the path boundary and the kernel
        substrate. ``CommandPatternGuard`` installs only when
        ``guard.deny_rules`` is flipped on (default off).
        """
        guards: list[CommandGuard] = []
        if self._settings.guard.deny_rules:
            guards.append(CommandPatternGuard())
        return GuardPipeline(guards)

    def _build_network_guard(self) -> NetworkGuard | None:
        """Advisory network checks are independent of path permissions."""
        if self._settings.guard.enabled and self._settings.guard.network:
            return NetworkGuard(NetworkGuardConfig())
        return None

    # -- live root projections (rebuilt on every evaluation) ---------------

    def _live_root(self) -> Path:
        """The live workspace root — re-read on every boundary build."""
        return canonicalize_path(self._root_provider.current())

    def _declared_roots(self) -> tuple[Path, ...]:
        """``writable_roots`` anchored to the live workspace root."""
        root = self._live_root()
        return PathEnvelope(self._settings.exclusive.writable_roots, base=root).roots

    def _write_roots(self) -> tuple[Path, ...]:
        """The write surface anchored live; empty under none/full."""
        surface = self._settings.exclusive.write_surface
        match surface:
            case WriteSurface.WORKSPACE:
                return (self._live_root(), *self._declared_roots())
            case WriteSurface.ROOTS:
                return self._declared_roots()
            case WriteSurface.NONE | WriteSurface.FULL:
                return ()
            case unreachable:
                assert_never(unreachable)

    def _tool_boundary(self, tool_name: str) -> tuple[Path, ...] | None:
        """The per-tool boundary paths anchored live, or None when unset.

        Parallel (read) boundaries and exclusive (write) boundaries live
        on their own class config; a hit replaces that tool's class
        default for the evaluation.
        """
        declared = (
            self._settings.parallel.boundaries.get(tool_name)
            or self._settings.exclusive.boundaries.get(tool_name)
        )
        if declared is None:
            return None
        return PathEnvelope(declared.paths, base=self._live_root()).roots

    def _live_boundary_pipeline(self) -> GuardPipeline | None:
        """The path-boundary guard rebuilt from the live root; None when off.

        Commands cannot be intent-classified, so the command envelope is
        the union face — workspace plus declared roots — under every
        guarded surface. bash remains the write-capable family's limiter
        even though parallel reads are unrestricted.
        """
        if self._settings.exclusive.write_surface is WriteSurface.FULL:
            return None
        allow_roots = [str(r) for r in self._declared_roots()]
        return GuardPipeline(
            [
                PathBoundaryGuard(
                    PathBoundaryConfig(
                        workspace_root=str(self._live_root()),
                        allow_paths=tuple(allow_roots),
                    )
                )
            ]
        )

    # -- evaluation --------------------------------------------------------

    def _command_is_readonly(self, command: str) -> bool:
        """The read-only fast path (``guard.read_only_bypass``).

        One judgment for both consumers — the command check and the
        explicit-cwd escape in ``evaluate_tool_call``.
        """
        return self._settings.guard.read_only_bypass and classify_readonly(
            command, self._command_family
        )

    def evaluate_command(self, command: str) -> GuardVerdict:
        """Best-effort command/input check: boundary, SSRF (and the
        deprecated deny rules when toggled on).

        A provably read-only command (``guard.read_only_bypass``, the
        shell-family profile in ``readonly.py``) returns CLEAN first —
        it performs no write and no network I/O, so neither the SSRF
        nor the boundary layer applies; this is the shell-world twin of
        the unrestricted parallel read class. Hard findings (deny
        rules) still precede it. This does not interpret arbitrary
        scripts, expansions, or accumulated stdin; CLEAN is not a claim
        of kernel containment.
        """
        if not command:
            return GuardVerdict(category=GuardCategory.CLEAN)
        result = self._static_pipeline.check(command)
        if not result.allowed:
            return self._verdict_from_pipeline(result)
        if self._command_is_readonly(command):
            return GuardVerdict(category=GuardCategory.CLEAN)
        # Hard findings must win over the approvable boundary category.
        if self._network_guard is not None:
            net = self._network_guard.check(command)
            if not net.allowed:
                return GuardVerdict(category=GuardCategory.SSRF, reason=net.reason, target=command)
        boundary = self._live_boundary_pipeline()
        if boundary is not None:
            boundary_result = boundary.check(command)
            if not boundary_result.allowed:
                return self._verdict_from_pipeline(boundary_result)
        return GuardVerdict(category=GuardCategory.CLEAN)

    def _verdict_from_pipeline(self, result: GuardResult) -> GuardVerdict:
        """Map a denied GuardResult to its category via match categories."""
        for match in result.matches:
            if match.category == _BOUNDARY_CATEGORY:
                return self._boundary_verdict(result.reason)
        return GuardVerdict(category=GuardCategory.DENY_RULE, reason=result.reason)

    def _boundary_verdict(
        self, reason: str, allowed_roots: tuple[Path, ...] = ()
    ) -> GuardVerdict:
        """A BOUNDARY verdict carrying the live envelope for denial copy."""
        return GuardVerdict(
            category=GuardCategory.BOUNDARY,
            reason=reason,
            allowed_roots=allowed_roots or ((self._live_root(), *self._declared_roots())),
        )

    def evaluate_file_tool(self, name: str, path: str | None) -> GuardVerdict:
        """Judge a file-tool invocation against the two-class projection.

        Parallel (read) tools are CLEAN unless a per-tool boundary
        narrows them. Exclusive (write) tools are bounded by the write
        surface; ``none`` refuses outright (DENY_RULE — a hard policy
        refuse, never approvable). Missing required targets deny.
        Protected subpaths shadow the standard write set; a per-tool
        boundary replaces the write set entirely (its own scope is the
        declaration).

        ``name`` selects the descriptor and labels denials.
        """
        descriptor = describe_tool_security(name)
        surface = self._settings.exclusive.write_surface
        boundary = self._tool_boundary(name)
        if surface is WriteSurface.FULL:
            # The dormancy escape: no file boundary built, per-tool
            # boundaries included.
            return GuardVerdict(category=GuardCategory.CLEAN)
        root = self._live_root()
        envelope = PathEnvelope((root, *self._declared_roots()), base=root).roots
        try:
            target = str(canonicalize_path(path, base=root)) if path is not None else None
        except PathCanonicalizationError:
            target = path
        if descriptor.effect is ToolEffect.WRITE and surface is WriteSurface.NONE:
            return GuardVerdict(
                category=GuardCategory.DENY_RULE,
                reason=f"write tool {name} refused — write surface is none",
                target=target,
                allowed_roots=envelope,
            )
        if path is None:
            return GuardVerdict(category=GuardCategory.DENY_RULE, reason="missing file target", allowed_roots=envelope)
        if descriptor.effect is ToolEffect.READ:
            if boundary is None:
                # Parallel default: unrestricted — the class governs itself.
                return GuardVerdict(category=GuardCategory.CLEAN)
            return self._containment_verdict(path, boundary, target)
        # Exclusive write tool.
        if boundary is not None:
            write_roots: tuple[Path, ...] = boundary
        else:
            write_roots = self._write_roots()
            if write_roots:
                protected = PathEnvelope(
                    tuple(
                        write_root / sub
                        for write_root in write_roots
                        for sub in self._settings.exclusive.protected_subpaths
                    ),
                    base=root,
                )
                if protected.contains(path, base=root):
                    return GuardVerdict(
                        category=GuardCategory.DENY_RULE, reason="protected path is read-only",
                        target=target, allowed_roots=write_roots,
                    )
        if not write_roots:
            return GuardVerdict(
                category=GuardCategory.BOUNDARY,
                reason=f"write surface {surface.value} declares no writable roots",
                target=target,
                allowed_roots=(),
            )
        return self._containment_verdict(path, write_roots, target)

    def _containment_verdict(
        self, path: str, roots: tuple[Path, ...], target: str | None
    ) -> GuardVerdict:
        """CLEAN inside the roots, BOUNDARY outside (canonical seam)."""
        root = self._live_root()
        envelope = PathEnvelope(roots, base=root)
        if envelope.contains(path, base=root):
            return GuardVerdict(category=GuardCategory.CLEAN)
        resolved = target
        if resolved is None:
            try:
                resolved = str(canonicalize_path(path, base=root))
            except PathCanonicalizationError:
                resolved = path
        return GuardVerdict(
            category=GuardCategory.BOUNDARY,
            reason=(
                f"Path '{path}' resolves to '{resolved}' which is outside "
                "the allowed roots"
            ),
            target=resolved,
            allowed_roots=tuple(envelope.roots),
        )

    def _working_dir_verdict(self, working_dir: str | None) -> GuardVerdict:
        """BOUNDARY when the explicit cwd escapes the command envelope."""
        if working_dir is None:
            return GuardVerdict(
                category=GuardCategory.DENY_RULE, reason="missing working directory"
            )
        root = self._live_root()
        envelope = (root, *self._declared_roots())
        check = PathEnvelope(envelope, base=root)
        if check.contains(working_dir, base=root):
            return GuardVerdict(category=GuardCategory.CLEAN)
        try:
            resolved = str(canonicalize_path(working_dir, base=root))
        except PathCanonicalizationError:
            resolved = working_dir
        return GuardVerdict(
            category=GuardCategory.BOUNDARY,
            reason=(
                f"Working directory '{working_dir}' resolves to '{resolved}' "
                "which is outside the allowed roots"
            ),
            target=resolved,
            allowed_roots=tuple(check.roots),
        )

    def evaluate_url(self, url: str) -> GuardVerdict:
        """Judge a URL string through the shared command guard pipeline."""
        return self.evaluate_command(url)

    def evaluate_tool_call(self, tool_name: str, arguments: Mapping[str, object]) -> GuardVerdict:
        """Judge one tool call through the typed tool-effect seam.

        The descriptor's effect selects the judgment; its declared target
        argument supplies the claimable target (path / command / url —
        whatever the tool's schema names). Execution input uses the same
        best-effort command guards. Unknown/MCP names claim no target;
        their existing approval and tool policy still apply.
        """
        descriptor = describe_tool_security(tool_name)
        target = extract_call_target(descriptor, arguments)
        match descriptor.effect:
            case ToolEffect.READ | ToolEffect.WRITE:
                return self.evaluate_file_tool(tool_name, target.path)
            case ToolEffect.EXECUTE | ToolEffect.EXECUTION_INPUT:
                verdict = self.evaluate_command(target.command or "")
                if not verdict.is_clean or descriptor.effect is ToolEffect.EXECUTION_INPUT:
                    return verdict
                working_dir = arguments.get("working_dir")
                if working_dir is not None:
                    # An explicit cwd is where the shell WRITES — judge it
                    # against the command envelope (the union face), not
                    # the unrestricted parallel read scope. A provably
                    # read-only command writes nothing, cwd included —
                    # the same fast path applies.
                    if not self._command_is_readonly(target.command or ""):
                        return self._working_dir_verdict(
                            working_dir if isinstance(working_dir, str) else None
                        )
                    return verdict
                return verdict
            case ToolEffect.WEB:
                if target.url:
                    return self.evaluate_url(target.url)
                return GuardVerdict(category=GuardCategory.CLEAN)
            case ToolEffect.NONE:
                return GuardVerdict(category=GuardCategory.CLEAN)
            case unreachable:
                assert_never(unreachable)
