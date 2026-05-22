from __future__ import annotations

from framework.core.types import InputMessage
from framework.multi_agent.router import DefaultMeshRouter


class TestDefaultMeshRouter:
    def test_defaults_external_conversation_to_main_agent_session(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(InputMessage(content="hello", session_id="chat-1"))

        assert result.conversation_id == "chat-1"
        assert result.agent_session_id == "chat-1:main"
        assert result.agent_name == "main"

    def test_defaults_external_conversation_to_current_agent_name(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(content="hello", session_id="chat-1"),
            default_agent_name="office-expert",
        )

        assert result.conversation_id == "chat-1"
        assert result.agent_session_id == "chat-1:office-expert"
        assert result.agent_name == "office-expert"

    def test_parses_agent_name_from_three_part_task_session(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="task",
                session_id="chat-1",
                metadata={"agent_session_id": "chat-1:office-expert:task-42"},
            )
        )

        assert result.conversation_id == "chat-1"
        assert result.agent_session_id == "chat-1:office-expert:task-42"
        assert result.agent_name == "office-expert"

    def test_uses_agent_session_conversation_when_metadata_omits_conversation(self) -> None:
        router = DefaultMeshRouter()

        result = router.route(
            InputMessage(
                content="task",
                session_id="transport-session",
                metadata={"agent_session_id": "chat-1:office-expert:task-42"},
            )
        )

        assert result.conversation_id == "chat-1"
        assert result.agent_session_id == "chat-1:office-expert:task-42"
        assert result.agent_name == "office-expert"
