"""``modexctl`` binary directory resolver — the single source of truth.

External coding agent integration spawns Pi/OpenCode subprocesses whose
LLM-driven bash tools call ``modexctl send`` / ``modexctl agents`` to
communicate with peers. For that to work, the spawn ``PATH`` must contain
the directory holding the ``modexctl`` executable.

Historically this resolution lived inline in two business-layer modules
(``external_strategy.py`` and ``subagent_external_builder.py``)
as identical copies of::

    exe = shutil.which("modexctl")
    if exe:
        return Path(exe).parent
    return Path(".")   # <-- never points at a real modexctl; silent failure

The fallback ``Path(".")`` is the root cause of the "modexctl not found"
flakes: when the bot is launched without the venv ``Scripts`` directory on
``PATH`` (e.g. from an anaconda shell, or after Windows env-var propagation
hasn't yet reached the launching shell), ``shutil.which`` returns ``None``,
the fallback resolves to the bot's CWD — which never contains ``modexctl``
— and every spawned external agent silently loses cross-pool messaging.

This module centralises resolution in one place with a deterministic
four-level strategy that **does not depend on the bot process's PATH**:

1. **Explicit override** — ``MODEXBOT_BIN_DIR`` env var (test/pin scenarios,
   and the lever future packaging code can set when it knows the install
   layout). Authoritative when present.
2. **Sibling of ``sys.executable``** — wheel console-script installers put
   ``modexctl`` next to ``python`` (``Scripts/`` on Windows, ``bin/`` on
   POSIX). This is independent of the launching shell's PATH: it follows the
   Python that's actually running the bot. Resolves the typical case even
   when the user launched from an anaconda or system shell. The LITERAL
   parent of ``sys.executable`` is probed first — Debian/Ubuntu venvs
   symlink ``bin/python`` to the system interpreter, so resolving first
   would escape the venv — with the resolved parent as a secondary
   candidate for exotic layouts.
3. **``shutil.which("modexctl")``** — legacy PATH lookup, last resort.
4. **Raise** — :class:`ModexctlResolutionError` with diagnostic context.
   Failing loudly at boot is better than the previous silent runtime
   corruption: an external pool that can't call ``modexctl`` is
   functionally dead for cross-pool messaging, so we surface the problem
   immediately instead of letting half the runs work.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

__all__ = ["ModexctlResolutionError", "resolve_modexctl_bin_dir"]


class ModexctlResolutionError(RuntimeError):
    """Raised when ``modexctl`` cannot be located deterministically.

    Attributes:
        sys_executable: ``sys.executable`` at resolution time — included so
            callers (logs, error UI) can show the Python that failed.
        which_result: Whatever ``shutil.which("modexctl")`` returned (may
            be ``None``) — same reason.
    """

    def __init__(self, sys_executable: str, which_result: str | None) -> None:
        self.sys_executable = sys_executable
        self.which_result = which_result
        super().__init__(
            "Could not resolve modexctl binary directory. "
            f"sys.executable={sys_executable!r}, "
            f"shutil.which('modexctl')={which_result!r}. "
            "Set MODEXBOT_BIN_DIR explicitly, or ensure modexctl is installed "
            "alongside the running Python."
        )


def _sibling_bin_dirs() -> tuple[Path, ...]:
    """Candidate directories that may hold ``modexctl`` beside the interpreter.

    On Windows the wheel installs console scripts into ``Scripts/``
    alongside ``python.exe``; on POSIX into ``bin/`` alongside ``python``.
    Some embeddable Python distributions (e.g. python-build-standalone used
    by the Windows installer) put scripts in the same directory as
    ``python.exe``, so the executable's own parent is accepted too.

    Candidate order matters. ``python3 -m venv`` on Debian/Ubuntu creates
    ``venv/bin/python`` as a SYMLINK to ``/usr/bin/python3``, so resolving
    ``sys.executable`` before taking the parent lands in ``/usr/bin`` and
    defeats the venv bin layout. The literal parent is therefore the FIRST
    candidate (wheel installers place ``modexctl`` next to the interpreter
    path the OS actually executed); the resolved parent is a SECONDARY
    candidate for exotic layouts where the literal path is only a redirect.
    Duplicates are removed, so a non-symlinked executable yields a single
    candidate.

    The function never raises — callers probe each candidate via
    :func:`_has_modexctl` and fall through to the next strategy.
    """
    candidates: list[Path] = []
    for exe in (Path(sys.executable), Path(sys.executable).resolve()):
        if sys.platform == "win32":
            # Standard venv / wheel install layout
            scripts = exe.parent / "Scripts"
            candidate = scripts if scripts.is_dir() else exe.parent
        else:
            # POSIX: venv uses bin/, system Python uses bin/ as well
            candidate = exe.parent
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _has_modexctl(directory: Path) -> bool:
    """Return True iff *directory* contains a runnable ``modexctl``.

    Accepts ``modexctl``, ``modexctl.exe``, or ``modexctl.bat`` (the Windows
    installer ships a ``.bat`` shim). Existence is the only check — we do
    not spawn the binary here; the spawn happens later when the external
    agent's bash actually calls ``modexctl``, and a stale-but-present binary
    surfaces as a clear error at that point.
    """
    if not directory.is_dir():
        return False
    candidates = ("modexctl", "modexctl.exe", "modexctl.bat")
    return any((directory / name).is_file() for name in candidates)


def resolve_modexctl_bin_dir() -> Path:
    """Resolve the directory that should be prepended to the spawn ``PATH``.

    Strategy (highest priority first):

    1. ``MODEXBOT_BIN_DIR`` env var — explicit override. Returned verbatim
       after existence check; if the directory does not exist we fall
       through (a stale override is silently ignored rather than fatal —
       the user may have moved the install and forgotten to clear the var).
    2. Sibling of ``sys.executable`` (see :func:`_sibling_bin_dirs`) — the
       authoritative location wheel installers use, independent of PATH.
       Literal parent first (Debian/Ubuntu venv ``bin/python`` is a symlink
       to the system interpreter, so ``Path.resolve()`` would escape the
       venv), resolved parent second.
    3. ``shutil.which("modexctl")`` parent — legacy PATH lookup fallback.
    4. :class:`ModexctlResolutionError` — never return ``Path(".")``.

    Returns:
        Absolute ``Path`` to the directory containing a runnable modexctl.

    Raises:
        ModexctlResolutionError: All four strategies failed.
    """
    # 1. Explicit override
    env_override = os.environ.get("MODEXBOT_BIN_DIR")
    if env_override:
        override_path = Path(env_override)
        if _has_modexctl(override_path):
            return override_path

    # 2. Siblings of sys.executable (wheel layout — authoritative, PATH-free;
    # literal parent first, resolved parent second — see _sibling_bin_dirs)
    for sibling in _sibling_bin_dirs():
        if _has_modexctl(sibling):
            return sibling

    # 3. shutil.which — legacy PATH lookup
    which_result = shutil.which("modexctl")
    if which_result:
        return Path(which_result).parent

    # 4. All strategies failed — raise with diagnostic context
    raise ModexctlResolutionError(
        sys_executable=sys.executable,
        which_result=which_result,
    )
