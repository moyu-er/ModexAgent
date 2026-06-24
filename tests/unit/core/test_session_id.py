"""Tests for SessionInfo pydantic model and SessionIdFactory."""

from __future__ import annotations

import time

import pytest

from modex_agent.core.session_id import (
    SessionInfo,
    SessionIdFactory,
    encode_snowflake,
    now_ms,
)


def test_now_ms_is_int_milliseconds():
    ts = now_ms()
    assert isinstance(ts, int)
    assert abs(ts - int(time.time() * 1000)) < 1000


def test_encode_snowflake_is_deterministic_and_short():
    a = encode_snowflake("1234567890")
    b = encode_snowflake("1234567890")
    assert a == b
    assert len(a) <= 24
    assert "." not in a and "/" not in a


def test_session_id_str_returns_display():
    session = SessionInfo(session_id="abc.main", agent_name="main")
    assert str(session) == "abc.main"


def test_session_id_hash_and_eq_by_string():
    a = SessionInfo(session_id="abc.main", agent_name="main", metadata={"x": 1})
    b = SessionInfo(session_id="abc.main", agent_name="main")
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_session_id_touch_updates_only_updated_at():
    base = SessionInfo(
        session_id="abc.main", agent_name="main", created_at=1000, updated_at=1000
    )
    touched = base.touch()
    assert touched.created_at == 1000
    assert touched.updated_at >= base.updated_at


def test_session_id_is_frozen():
    """Frozen model: field mutation raises; safe as dict key after creation."""
    from pydantic import ValidationError

    session = SessionInfo(session_id="abc.main", agent_name="main")
    with pytest.raises(ValidationError):
        session.session_id = "xyz.main"  # type: ignore[misc]
    # still usable as a dict key
    d = {session: 1}
    assert d[SessionInfo(session_id="abc.main", agent_name="main")] == 1


def test_from_str_with_separator():
    session = SessionInfo.from_str("abc.reviewer", default_agent_name="main")
    assert session.session_id == "abc.reviewer"
    assert session.agent_name == "reviewer"


def test_from_str_without_separator_warns():
    with pytest.warns(UserWarning):
        session = SessionInfo.from_str("abc", default_agent_name="main")
    assert session.agent_name == "main"


def test_from_str_empty_suffix_warns():
    with pytest.warns(UserWarning):
        SessionInfo.from_str("abc.", default_agent_name="main")


def test_factory_creates_main_session():
    factory = SessionIdFactory()
    session = factory.create(agent_name="main")
    assert session.agent_name == "main"
    assert "." in session.session_id
    assert session.parent_session_id is None
    assert session.created_at == session.updated_at
    assert session.session_id.endswith(".main")


def test_factory_subagent_links_parent():
    factory = SessionIdFactory()
    parent = factory.create(agent_name="main")
    child = factory.create(agent_name="reviewer", parent_session_id=parent)
    assert child.parent_session_id == parent.session_id
    # subagent snowflake differs from parent
    assert child.session_id_prefix != parent.session_id_prefix


def test_factory_external_id_becomes_snowflake():
    factory = SessionIdFactory()
    session = factory.create(agent_name="main", external_id="qq-group-12345")
    assert session.session_id.startswith(encode_snowflake("qq-group-12345"))


def test_factory_invocation_id_as_external_becomes_session():
    factory = SessionIdFactory()
    # A subagent whose invocation_id was "a1b2c3d4" now has that as its snowflake
    session = factory.create(agent_name="reviewer", external_id="a1b2c3d4")
    assert session.session_id_prefix == encode_snowflake("a1b2c3d4")


def test_session_id_snowflake_property():
    session = SessionInfo(session_id="abc123.reviewer", agent_name="reviewer")
    assert session.session_id_prefix == "abc123"


def test_session_id_is_subagent_property():
    main = SessionInfo(session_id="abc.main", agent_name="main")
    sub = SessionInfo(
        session_id="xyz.reviewer", agent_name="reviewer", parent_session_id="abc.main"
    )
    assert main.is_subagent is False
    assert sub.is_subagent is True
