"""Architecture guard for the TerminalBackend async-safety contract.

Asserts the contract shape from ADR-0032 D1/D4 on the base class.

Ticket 01 (expand step) added the scaffolding methods as non-abstract
defaults. Tickets 02–05 activated the contract per backend (each backend
implemented ``_shell_family`` and either the blocking-IO hooks or
native-async overrides). Ticket 06 promoted ``_shell_family`` to
``@abstractmethod`` (the contract is now enforced at instantiation time)
and deleted the safe-default body. Ticket 07 adds the AST-level guard
test (``test_terminal_async_safety.py``) that catches the regression
class structurally.
"""

from __future__ import annotations

import inspect

import pytest

from modex_agent.tools.terminal.backends.base import TerminalBackend

# Methods that the scaffolding must add to TerminalBackend.
_SCAFFOLD_METHODS = (
    "_write_blocking",
    "_read_blocking",
    "write",
    "read_pending",
    "current_segment",
    "clear_input_line",
    "drain_startup",
    "_shell_family",
)

# Methods that were @abstractmethod before ticket 01 and must now be
# concrete (so a bare TerminalBackend subclass no longer needs to override
# them just to satisfy the ABC). ``_shell_family`` is NOT in this list —
# ticket 06 promoted it back to ``@abstractmethod``.
_DEABSTRACTED_METHODS = (
    "write",
    "read_pending",
    "current_segment",
    "clear_input_line",
    "drain_startup",
)


def test_terminal_backend_has_scaffolding_methods() -> None:
    """All new contract methods exist on the base class itself."""
    for name in _SCAFFOLD_METHODS:
        assert name in vars(TerminalBackend), (
            f"TerminalBackend is missing scaffolding method: {name}"
        )


def test_scaffolding_methods_are_not_abstract() -> None:
    """The de-abstracted methods are no longer in __abstractmethods__."""
    abstract = TerminalBackend.__abstractmethods__
    for name in _DEABSTRACTED_METHODS:
        assert name not in abstract, (
            f"{name} should not be abstract on TerminalBackend after ticket 01"
        )


def test_shell_family_is_abstract() -> None:
    """``_shell_family`` is ``@abstractmethod`` after ticket 06."""
    assert "_shell_family" in TerminalBackend.__abstractmethods__, (
        "_shell_family should be abstract on TerminalBackend after ticket 06"
    )


def test_write_blocking_hook_raises_not_implemented() -> None:
    """Default _write_blocking is an opt-in hook that raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        TerminalBackend._write_blocking(None, "data")  # type: ignore[arg-type]


def test_read_blocking_hook_raises_not_implemented() -> None:
    """Default _read_blocking is an opt-in hook that raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        TerminalBackend._read_blocking(None, 0.1, 64)  # type: ignore[arg-type]


def test_write_template_uses_run_in_executor() -> None:
    """Structural guard: write template wraps the hook via run_in_executor."""
    src = inspect.getsource(TerminalBackend.write)
    assert "run_in_executor" in src
    assert "_write_blocking" in src


def test_read_pending_template_uses_run_in_executor() -> None:
    """Structural guard: read_pending template wraps the hook via run_in_executor."""
    src = inspect.getsource(TerminalBackend.read_pending)
    assert "run_in_executor" in src
    assert "_read_blocking" in src


def test_current_segment_default_uses_buffer_extractor() -> None:
    """current_segment default uses extract_current_segment_from_buffer."""
    src = inspect.getsource(TerminalBackend.current_segment)
    assert "extract_current_segment_from_buffer" in src


def test_clear_input_line_default_gated_on_shell_family() -> None:
    """clear_input_line default is gated on _shell_family().uses_readline()."""
    src = inspect.getsource(TerminalBackend.clear_input_line)
    assert "_shell_family" in src
    assert "uses_readline" in src


def test_drain_startup_default_calls_shared_helper() -> None:
    """drain_startup default wires the shared drain_windows_startup helper."""
    src = inspect.getsource(TerminalBackend.drain_startup)
    assert "drain_windows_startup" in src
    assert "_shell_family" in src
