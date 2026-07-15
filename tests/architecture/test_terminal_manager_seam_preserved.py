from __future__ import annotations

import importlib.util
import inspect

import modex_agent.tools.terminal as terminal
from modex_agent.tools.terminal.managers import (
    BaseTerminalManager,
    TerminalManagerBase,
    create_terminal_manager,
)

_CAPABILITY_METHODS = (
    "_evict_oldest",
    "_check_memory_pressure",
)


def test_base_terminal_manager_retains_capability_methods() -> None:
    missing = [method for method in _CAPABILITY_METHODS if method not in vars(BaseTerminalManager)]
    assert not missing, (
        f"BaseTerminalManager lost retained capability methods {missing}."
    )


def test_terminal_json_persistence_seam_is_absent() -> None:
    assert importlib.util.find_spec("modex_agent.tools.terminal.state_store") is None
    assert "JsonTerminalStateStore" not in vars(terminal)
    assert "save_state" not in vars(BaseTerminalManager)
    assert "load_state" not in vars(BaseTerminalManager)


def test_terminal_manager_constructors_do_not_accept_storage_dir() -> None:
    assert "storage_dir" not in inspect.signature(BaseTerminalManager).parameters
    assert "storage_dir" not in inspect.signature(create_terminal_manager).parameters


def test_terminal_manager_base_abc_still_exists() -> None:
    """The seam ABC still exists with at least one production subclass."""
    assert issubclass(BaseTerminalManager, TerminalManagerBase)


def test_capability_method_list_is_nonempty() -> None:
    """Sanity: the guard must actually watch something."""
    assert _CAPABILITY_METHODS
