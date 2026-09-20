"""Editor-session origin stored with the existing session identity."""
from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from modex_agent.core.session_id import SessionIdFactory, SessionInfo

_ORIGIN_KEY: Final = "acp_origin"


class AcpSessionOrigin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    pool_name: str
    agent_name: str


def create_acp_session(*, pool_name: str, agent_name: str) -> SessionInfo:
    origin = AcpSessionOrigin(pool_name=pool_name, agent_name=agent_name)
    return SessionIdFactory().create(
        agent_name,
        metadata={_ORIGIN_KEY: origin.model_dump(mode="json"), "pool": pool_name},
    )


def validate_acp_session(session: SessionInfo, *, pool_name: str, agent_name: str) -> None:
    raw = session.metadata.get(_ORIGIN_KEY)
    if raw is None:
        raise ValueError("Session was not created by ACP")
    origin = AcpSessionOrigin.model_validate(raw)
    if session.parent_session_id is not None:
        raise ValueError("ACP requires a root session")
    if origin.pool_name != pool_name or origin.agent_name != agent_name or session.agent_name != agent_name:
        raise ValueError("Session is bound to a different pool or agent")
