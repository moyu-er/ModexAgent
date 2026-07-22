"""Architecture guard for the ADR-0032 async-safety contract.

Catches the regression class that produced the original "synchronous
blocking I/O inside async def" defect. Four assertions per concrete
``TerminalBackend`` subclass found in
``src/modex_agent/tools/terminal/backends/``:

1. **Hook-or-override consistency** — if a backend does not override
   ``write`` / ``read_pending``, it must implement the corresponding
   ``_write_blocking`` / ``_read_blocking`` hook (so the base-class
   template has something to wrap). Catches "deleted the hook but
   didn't override the template."
2. **Async-safety evidence** — every overridden ``write`` /
   ``read_pending`` contains ``run_in_executor`` or ``await``; the
   base-class templates use ``run_in_executor``.
3. **``_shell_family`` implementation** — every concrete subclass
   defines ``_shell_family`` directly (not just inherited).
4. **No ``settimeout`` leak surface** — no raw-socket ``settimeout``
   call anywhere in ``backends/``. Only ``fileobj.settimeout`` /
   ``fobj.settimeout`` (pywinpty per-instance read-side socket) are
   allowed; ``def settimeout`` (method definitions in ABCs) are not
   calls and are not matched.

Manual regression verification (per spec acceptance criteria):
- Temporarily commented out ``_write_blocking`` on ``WinptyHiddenBackend``
  -> ``test_write_hook_or_override[WinptyHiddenBackend]`` failed:
  "WinptyHiddenBackend inherits write from base but does not implement
  _write_blocking".
- Restored the code -> all tests pass.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.backends.pexpect_pty import PexpectPtyBackend
from modex_agent.tools.terminal.backends.tmux_pty import TmuxPtyBackend
from modex_agent.tools.terminal.backends.visible_windows import WinptyConsoleWindowBackend
from modex_agent.tools.terminal.backends.windows_hidden import WinptyHiddenBackend

_CONCRETE_BACKENDS = [
    pytest.param(WinptyHiddenBackend, id="WinptyHiddenBackend"),
    pytest.param(PexpectPtyBackend, id="PexpectPtyBackend"),
    pytest.param(WinptyConsoleWindowBackend, id="WinptyConsoleWindowBackend"),
    pytest.param(TmuxPtyBackend, id="TmuxPtyBackend"),
]

_BACKENDS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "modex_agent"
    / "tools"
    / "terminal"
    / "backends"
)

# Variable names allowed to call ``.settimeout(`` — pywinpty's per-instance
# read-side socket (``proc.fileobj`` / local alias ``fobj``). These are NOT
# the raw TCP socket API that produced the ADR-0032 root cause 2 defect.
_ALLOWED_SETTIMEOUT_VARS = frozenset({"fileobj", "fobj"})


# ---------------------------------------------------------------------------
# Assertion 1: hook-or-override consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _CONCRETE_BACKENDS)
def test_write_hook_or_override(cls: type[TerminalBackend]) -> None:
    """If ``write`` is not overridden, ``_write_blocking`` must exist.

    Catches the regression "deleted the hook but didn't override the
    template" — a backend that inherits the base-class ``write`` template
    but fails to implement ``_write_blocking`` would hit
    ``NotImplementedError`` at runtime.
    """
    if "write" not in vars(cls):
        assert "_write_blocking" in vars(cls), (
            f"{cls.__name__} inherits write from base but does not "
            f"implement _write_blocking — the base-class template would "
            f"call the NotImplementedError default"
        )


@pytest.mark.parametrize("cls", _CONCRETE_BACKENDS)
def test_read_pending_hook_or_override(cls: type[TerminalBackend]) -> None:
    """If ``read_pending`` is not overridden, ``_read_blocking`` must exist."""
    if "read_pending" not in vars(cls):
        assert "_read_blocking" in vars(cls), (
            f"{cls.__name__} inherits read_pending from base but does not "
            f"implement _read_blocking — the base-class template would "
            f"call the NotImplementedError default"
        )


# ---------------------------------------------------------------------------
# Assertion 2: async-safety evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _CONCRETE_BACKENDS)
def test_write_async_safety_evidence(cls: type[TerminalBackend]) -> None:
    """Overridden ``write`` must contain ``run_in_executor`` or ``await``.

    Backends that inherit ``write`` from the base class are covered by
    ``test_base_write_template_uses_run_in_executor`` below.
    """
    if "write" not in vars(cls):
        return  # Inherited — checked via base-class test.
    src = inspect.getsource(cls.write)
    assert "run_in_executor" in src or "await" in src, (
        f"{cls.__name__}.write is overridden but its source contains "
        f"neither run_in_executor nor await — synchronous blocking I/O "
        f"inside async def (ADR-0032 root cause 1)"
    )


@pytest.mark.parametrize("cls", _CONCRETE_BACKENDS)
def test_read_pending_async_safety_evidence(cls: type[TerminalBackend]) -> None:
    """Overridden ``read_pending`` must contain ``run_in_executor`` or ``await``."""
    if "read_pending" not in vars(cls):
        return  # Inherited — checked via base-class test.
    src = inspect.getsource(cls.read_pending)
    assert "run_in_executor" in src or "await" in src, (
        f"{cls.__name__}.read_pending is overridden but its source "
        f"contains neither run_in_executor nor await — synchronous "
        f"blocking I/O inside async def (ADR-0032 root cause 1)"
    )


def test_base_write_template_uses_run_in_executor() -> None:
    """Base-class ``write`` template wraps the hook via ``run_in_executor``.

    Backends that inherit ``write`` (WinptyHiddenBackend, PexpectPtyBackend)
    rely on this template for non-blocking I/O.
    """
    src = inspect.getsource(TerminalBackend.write)
    assert "run_in_executor" in src, (
        "TerminalBackend.write template must use run_in_executor so "
        "backends inheriting it get non-blocking I/O"
    )


def test_base_read_pending_template_uses_run_in_executor() -> None:
    """Base-class ``read_pending`` template wraps the hook via ``run_in_executor``."""
    src = inspect.getsource(TerminalBackend.read_pending)
    assert "run_in_executor" in src, (
        "TerminalBackend.read_pending template must use run_in_executor "
        "so backends inheriting it get non-blocking I/O"
    )


# ---------------------------------------------------------------------------
# Assertion 3: _shell_family implementation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", _CONCRETE_BACKENDS)
def test_shell_family_implemented(cls: type[TerminalBackend]) -> None:
    """Every concrete subclass defines ``_shell_family`` directly.

    After ticket 06 promoted ``_shell_family`` to ``@abstractmethod``,
    Python enforces this at instantiation time. The architecture test
    adds a static structural check so the regression is caught at test
    time, not at runtime instantiation.
    """
    assert "_shell_family" in vars(cls), (
        f"{cls.__name__} does not define _shell_family directly — "
        f"it must implement the @abstractmethod hook (ADR-0032 D4.1)"
    )


# ---------------------------------------------------------------------------
# Assertion 4: no settimeout leak surface
# ---------------------------------------------------------------------------


def test_no_settimeout_leak_surface() -> None:
    """No raw-socket ``settimeout`` call anywhere in ``backends/``.

    ADR-0032 root cause 2: ``socket.settimeout`` mutated the visible-
    windows TCP socket's timeout state, leaking into the write path and
    producing the "command typed but not submitted" defect. This
    assertion structurally prevents the regression class.

    Allowed: ``fileobj.settimeout`` / ``fobj.settimeout`` — pywinpty's
    per-instance read-side socket (write goes through a different handle,
    so the mutation does not leak). Also allowed: ``def settimeout``
    method definitions in ABCs (``ReadablePtyFile`` in
    ``visible_windows_host.py``) — these are signatures, not calls, and
    are not matched by the ``\\w+\\.settimeout(`` call regex.
    """
    # Matches `var.settimeout(` calls. Does NOT match `def settimeout(`
    # because there is no dot before `settimeout` in a method definition.
    call_pattern = re.compile(r"(\w+)\.settimeout\s*\(")
    violations: list[str] = []
    for py_file in sorted(_BACKENDS_DIR.glob("*.py")):
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for match in call_pattern.finditer(line):
                var = match.group(1)
                if var in _ALLOWED_SETTIMEOUT_VARS:
                    continue
                violations.append(
                    f"{py_file.name}:{lineno}: {line.strip()!r} "
                    f"(variable {var!r} is not a pywinpty fileobj/fobj)"
                )
    assert not violations, (
        "socket.settimeout leak surface detected (ADR-0032 root cause 2). "
        "Only fileobj.settimeout / fobj.settimeout (pywinpty per-instance "
        "read-side socket) are allowed. Violations:\n  - "
        + "\n  - ".join(violations)
    )
