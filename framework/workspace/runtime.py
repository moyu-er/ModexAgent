"""Per-turn workspace root contextvar.

Set by the business dispatcher around each turn so residual tools that used to
call ``Path.cwd()`` can resolve the active workspace working dir instead, without
the business dispatcher having to thread a root through every tool call. Lives
in the framework so framework tools can import it with no business coupling.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path

_current: ContextVar[Path | None] = ContextVar("current_workspace_root", default=None)


def resolve_workspace_root() -> Path:
    """The workspace working dir for the current turn, or the process CWD if unset.

    Unset is the safe default outside any workspace-scoped turn (tests, boot).
    """
    value = _current.get()
    return value if value is not None else Path.cwd()


@contextlib.contextmanager
def bind_workspace_root(target: Path) -> Iterator[None]:
    """Bind ``target`` as the workspace root for the duration of the ``with`` block."""
    token = _current.set(Path(target))
    try:
        yield
    finally:
        _current.reset(token)


def is_workspace_root_bound() -> bool:
    """True when the per-turn workspace root contextvar is set (inside a dispatch turn).

    Use this where ``resolve_workspace_root`` is ambiguous — outside a turn it
    returns ``Path.cwd()``, which is indistinguishable from a turn that genuinely
    bound cwd.  This predicate answers "are we inside a turn?" directly.
    """
    return _current.get() is not None
