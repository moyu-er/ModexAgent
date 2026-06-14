from __future__ import annotations

from framework.core.session_id import SessionId
from framework.core.types import InputMessage
from framework.multi_agent.router import DefaultMeshRouter


class TestDefaultMeshRouter:
    def test_uses_input_session_directly(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="hello",
                session=SessionId.from_str("chat-1", default_agent_name="main"),
            )
        )

        assert str(result.session) == "chat-1"
        assert result.session.agent_name == "main"
        assert result.session.snowflake == "chat-1"

    def test_default_agent_name_used_when_session_has_no_agent(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="hello",
                session=SessionId.from_str("chat-1", default_agent_name="office-expert"),
            )
        )

        assert str(result.session) == "chat-1"
        assert result.session.agent_name == "office-expert"

    def test_trusts_input_session_not_metadata(self) -> None:
        """Router uses input_msg.session directly; metadata agent_session_id is ignored."""
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="task",
                session=SessionId.from_str("chat-1.main", default_agent_name="main"),
                metadata={"agent_session_id": "chat-1.office-expert.task-42"},
            )
        )

        assert str(result.session) == "chat-1.main"
        assert result.session.agent_name == "main"

    def test_session_from_input_msg_is_authoritative(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="task",
                session=SessionId.from_str(
                    "transport-session", default_agent_name="main"
                ),
                metadata={"agent_session_id": "chat-1.office-expert.task-42"},
            )
        )

        assert str(result.session) == "transport-session"
        assert result.session.agent_name == "main"

    def test_envelope_detection_from_metadata(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="task",
                session=SessionId.from_str("chat-1.main", default_agent_name="main"),
                metadata={
                    "agent_session_id": "chat-1.office-expert.task-42",
                    "message_type": "subagent_result",
                    "source_agent": "office-expert",
                },
            )
        )

        assert result.is_envelope is True
        assert result.prompt_modifier == "[Subagent office-expert result]\n\n"
        assert result.session.agent_name == "main"

    def test_agent_message_is_envelope(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="hi",
                session=SessionId.from_str("chat-1.main", default_agent_name="main"),
                metadata={"message_type": "agent_message"},
            )
        )

        assert result.is_envelope is True
        assert result.prompt_modifier is None
