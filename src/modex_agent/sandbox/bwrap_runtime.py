"""Linux LOCAL runtime: compile bwrap arguments and validate startup.

Compiles a :class:`~modex_agent.sandbox.settings.SandboxSettings` write surface into
a bubblewrap argv prefix — the execve-style CLI wrapper the PRD's local
family is built on (the wrapper configures the namespaces and then becomes
the target process, so pexpect/marker-protocol semantics carry through
unchanged). Argument compilation inspects filesystem paths but launches no
process. The ``resolve_available`` lifecycle subsequently executes a constant
no-op with the compiled arguments before binding a shell.

Final argv shapes (``<prefix>`` ends with the ``--`` separator):

- READ_ONLY::

      bwrap --ro-bind / / --tmpfs /tmp --dev /dev --proc /proc
            --die-with-parent [--unshare-net] --
      shell_argv                    = <prefix> + [shell, --noprofile, --norc, -i]
      one_shot_command_argv_prefix  = <prefix>  (command argv follows)

- ``workspace`` adds, right after the root ro-bind::

      --bind <ws> <ws> ...
    ``roots`` keeps the workspace ro-bound and rw-binds only the
    declared ``writable_roots``::

      --bind <ws> <ws>                       (workspace writable)
      --ro-bind <ws>/<sub> <ws>/<sub>        (protected_subpaths shadow)
      --bind <root> <root>                   (each writable_roots entry)
      --ro-bind <root>/<sub> <root>/<sub>    (shadows there too)

  bwrap mounts in argument order, so a later ``--ro-bind`` shadows the
  earlier ``--bind`` at the same destination — that ordering *is* the
  read-only-subpath enforcement.

``--dev /dev`` mounts the minimal device tree (bwrap-created nodes, not the
host ``/dev``), ``--proc /proc`` a fresh procfs. ``--die-with-parent`` ties the
sandbox lifetime to the agent process. Network isolation is coarse-grained:
``--unshare-net`` unless ``network=True``.

Off-Linux or confirmed pre-command engine unavailability
probe yields the :class:`HostRuntime` result with the ``degraded_reason``
carried verbatim — never a silently compiled weaker sandbox.
``full`` keeps the selected engine with ``--bind / /`` and
no private tmpfs or read-only shadows; network and process setup still apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import assert_never

from modex_agent.workspace.boundary import canonicalize_path

from .platform import Platform, get_platform, resolve_shell
from .runtime import HostRuntime, ResolvedSandbox, SandboxRuntime, validate_local_startup
from .settings import SandboxBackend, SandboxSettings, WriteSurface
from .types import EnforcementLevel

_get_platform = get_platform
_resolve_host_shell = resolve_shell

_BWRAP = "bwrap"
# Host argv tail — identical to HostRuntime's (the marker protocol needs the
# deterministic no-profile interactive bash; non-bash shells are the caller's
# concern, never emitted here).
_BASH_SPAWN_ARGS = ["--noprofile", "--norc", "-i"]

__all__ = ["BwrapRuntime"]


def _shadow_mounts(root: Path, subpaths: list[str]) -> list[str]:
    """``--ro-bind`` shadow args keeping each *subpath* read-only inside a
    writable *root*. Emitted after the root's ``--bind`` — later mounts
    shadow earlier ones at the same destination.

    A subpath with no host source is skipped: bwrap refuses to bind a
    nonexistent source (the whole sandbox fails to start), and a
    nonexistent path carries nothing to protect (the isolation.py
    ``BubblewrapProvider`` posture — bind only what exists).
    """
    argv: list[str] = []
    for sub in subpaths:
        source = canonicalize_path(sub, base=root)
        if not source.exists():
            continue
        shadow = str(source)
        argv.extend(["--ro-bind", shadow, shadow])
    return argv


def _compile_argv_prefix(
    settings: SandboxSettings, workspace_root: Path
) -> list[str]:
    """Compile the write surface into the bwrap argv prefix (ends with ``--``)."""
    surface = settings.exclusive.write_surface
    root_bind = "--bind" if surface is WriteSurface.FULL else "--ro-bind"
    prefix = [_BWRAP, root_bind, "/", "/"]
    if surface is not WriteSurface.FULL:
        prefix.extend(["--tmpfs", "/tmp"])
    match surface:
        case WriteSurface.NONE:
            pass
        case WriteSurface.WORKSPACE:
            ws = workspace_root
            prefix.extend(["--bind", str(ws), str(ws)])
            prefix.extend(_shadow_mounts(ws, settings.exclusive.protected_subpaths))
            _bind_roots(prefix, settings, ws)
        case WriteSurface.ROOTS:
            # The workspace root stays ro-bound (the root bind above);
            # only the declared roots gain rw mounts.
            _bind_roots(prefix, settings, workspace_root)
        case WriteSurface.FULL:
            pass
        case unreachable:
            assert_never(unreachable)
    prefix.extend(["--dev", "/dev", "--proc", "/proc", "--die-with-parent"])
    if not settings.network:
        prefix.append("--unshare-net")
    prefix.append("--")
    return prefix


def _bind_roots(prefix: list[str], settings: SandboxSettings, base: Path) -> None:
    """rw-bind each declared root (anchored to the live workspace) with its
    protected-subpath shadows.

    Same bwrap constraint as shadows: a missing source cannot be bound —
    the write then hits the ro-bound root and fails with the actionable
    denial instead of breaking startup.
    """
    for root in settings.exclusive.writable_roots:
        anchored = canonicalize_path(root, base=base)
        if not anchored.exists():
            continue
        prefix.extend(["--bind", str(anchored), str(anchored)])
        prefix.extend(_shadow_mounts(anchored, settings.exclusive.protected_subpaths))


class BwrapRuntime(SandboxRuntime):
    """Linux local-family runtime: compiles policy → bwrap argv prefix.

    ``resolve()`` compiles argv; ``resolve_available()`` also validates startup
    by executing a constant no-op. Engine selection belongs to
    ``selection.resolve_selection`` and ``selection.select_runtime``. The
    platform gate stays as the honest guard when constructed directly: a
    non-Linux platform yields the HostRuntime result with a reason.
    """

    async def _validate_startup(self, resolved: ResolvedSandbox, workspace_root: Path) -> None:
        await validate_local_startup(resolved, workspace_root)

    async def resolve(
        self, settings: SandboxSettings, workspace_root: Path
    ) -> ResolvedSandbox:
        platform = _get_platform()
        if platform is not Platform.LINUX:
            return await self._host_degraded(
                settings,
                workspace_root,
                f"bwrap is linux-only, platform is {platform.value}",
            )
        prefix = _compile_argv_prefix(settings, workspace_root)
        shell = _resolve_host_shell()
        shell_argv = [*prefix, shell, *_BASH_SPAWN_ARGS] if shell else []
        return ResolvedSandbox(
            backend=SandboxBackend.LOCAL,
            enforcement=EnforcementLevel.FULL,
            shell_argv=shell_argv,
            one_shot_command_argv_prefix=prefix,
        )

    async def _host_degraded(
        self,
        settings: SandboxSettings,
        workspace_root: Path,
        reason: str | None,
    ) -> ResolvedSandbox:
        """The host-shaped result with the degradation fact attached."""
        host = await HostRuntime().resolve(settings, workspace_root)
        return host.model_copy(update={"degraded_reason": reason})
