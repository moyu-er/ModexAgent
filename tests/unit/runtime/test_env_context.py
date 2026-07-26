"""Tests for runtime env ContextVars — defaults, set+get, and context isolation.

Covers the three acceptance criteria from the spec:
(a) default is None,
(b) set+get within the same context returns the value,
(c) two contexts copied via ``contextvars.copy_context()`` hold independent
    values and do not leak back into the parent.
"""
from __future__ import annotations

import contextvars

from modex_agent.runtime.env_context import _current_session_id, _modex_env


def test_default_is_none() -> None:
    """Both ContextVars default to None when no hook has set them."""
    assert _modex_env.get() is None
    assert _current_session_id.get() is None


def test_set_and_get_returns_value() -> None:
    """Setting a value in the current context is observable via get()."""
    env_token = _modex_env.set({"PATH": "/usr/bin"})
    session_token = _current_session_id.set("sess-123")

    try:
        assert _modex_env.get() == {"PATH": "/usr/bin"}
        assert _current_session_id.get() == "sess-123"
    finally:
        _modex_env.reset(env_token)
        _current_session_id.reset(session_token)

    assert _modex_env.get() is None
    assert _current_session_id.get() is None


def test_contextvar_isolation_across_contexts() -> None:
    """Two contexts from copy_context() hold independent values.

    Mutations inside one copied context must not be visible in the other, nor
    leak back into the parent context that spawned them.
    """
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    def run_a() -> tuple[dict[str, str] | None, str | None]:
        _modex_env.set({"A": "1"})
        _current_session_id.set("session-a")
        return _modex_env.get(), _current_session_id.get()

    def run_b() -> tuple[dict[str, str] | None, str | None]:
        _modex_env.set({"B": "2"})
        _current_session_id.set("session-b")
        return _modex_env.get(), _current_session_id.get()

    result_a = ctx_a.run(run_a)
    result_b = ctx_b.run(run_b)

    assert result_a == ({"A": "1"}, "session-a")
    assert result_b == ({"B": "2"}, "session-b")

    # Parent context remains untouched by child-context mutations.
    assert _modex_env.get() is None
    assert _current_session_id.get() is None
