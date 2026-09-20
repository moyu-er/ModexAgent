"""Only this project's explicitly indexed ACP root sessions may be loaded."""
from __future__ import annotations

import pytest
from bot.acp.identity import create_acp_session, validate_acp_session

from modex_agent.core.session_id import SessionIdFactory


def test_new_acp_sessions_have_distinct_framework_ids_and_bound_origin() -> None:
    first = create_acp_session(pool_name="coding", agent_name="main")
    second = create_acp_session(pool_name="coding", agent_name="main")
    assert first.session_id != second.session_id
    assert first.agent_name == "main"
    assert first.parent_session_id is None
    validate_acp_session(first, pool_name="coding", agent_name="main")


def test_resident_session_cannot_be_loaded_as_acp() -> None:
    session = SessionIdFactory().create("main")
    with pytest.raises(ValueError, match="ACP"):
        validate_acp_session(session, pool_name="coding", agent_name="main")


@pytest.mark.parametrize("pool,agent", [("other", "main"), ("coding", "other")])
def test_acp_origin_cannot_be_rebound_to_different_pool_or_agent(pool: str, agent: str) -> None:
    session = create_acp_session(pool_name="coding", agent_name="main")
    with pytest.raises(ValueError, match="bound"):
        validate_acp_session(session, pool_name=pool, agent_name=agent)


def test_child_session_cannot_be_opened_as_acp_root() -> None:
    session = create_acp_session(pool_name="coding", agent_name="main").model_copy(
        update={"parent_session_id": "parent.main"}
    )
    with pytest.raises(ValueError, match="root"):
        validate_acp_session(session, pool_name="coding", agent_name="main")
