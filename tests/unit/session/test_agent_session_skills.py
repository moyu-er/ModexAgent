"""Unit tests for AgentSession skill_manager integration."""

import pytest

from framework.core.emitter import AgentResult
from framework.core.skills import DefaultSkillBuilder, InlineSkillSource, SkillManager
from framework.core.skills.models import Skill
from framework.core.types import InputMessage
from framework.session.agent_session import AgentSession


class FakeAgent:
    name = "FakeAgent"

    async def run(self, context, emitter, streaming=True):
        # Return content that includes the system prompt for verification
        return AgentResult(
            content=f"System was: {context.system_prompt}",
            stop_reason="complete",
            messages=[],
        )


class FakeContextManager:
    def __init__(self, base_system_prompt=""):
        self.base_system_prompt = base_system_prompt
        self._states: dict[str, Any] = {}

    async def load(self, session_id, **kwargs):
        from framework.core.context import ContextState

        if session_id not in self._states:
            self._states[session_id] = ContextState(
                system_prompt=self.base_system_prompt, history=[]
            )
        return self._states[session_id]

    async def load_with_metadata(self, session_id, metadata=None):
        return await self.load(session_id)

    async def flush(self, session_id):
        pass

    async def save(self, session_id, user_message, assistant_result, metadata=None):
        from framework.memory.history import MessageHistory

        state = self._states.get(session_id)
        if state is None:
            return
        if isinstance(state.history, MessageHistory):
            if user_message:
                await state.history.append(user_message)
            if assistant_result.messages:
                await state.history.extend(assistant_result.messages)
            elif assistant_result.content:
                await state.history.append({"role": "assistant", "content": assistant_result.content})
        else:
            if user_message:
                state.history.append(user_message)
            if assistant_result.messages:
                state.history.extend(assistant_result.messages)
            elif assistant_result.content:
                state.history.append({"role": "assistant", "content": assistant_result.content})

    async def build_system_prompt(self, tool_manager, skill_manager=None, runtime_info=None):
        parts = [self.base_system_prompt]
        if skill_manager is not None:
            from framework.core.skills import ResolutionContext

            skill_prompt = await skill_manager.build_prompt(
                ResolutionContext.from_runtime(tool_manager=tool_manager)
            )
            if skill_prompt:
                parts.append(skill_prompt)
        return "\n\n---\n\n".join(parts)

    async def clear(self, session_id):
        self._states.pop(session_id, None)


class FakeToolManager:
    async def startup(self):
        pass

    async def shutdown(self):
        pass

    def list_tools(self):
        return []


class MinimalEmitter:
    """A minimal emitter that satisfies ContentEmitter interface."""

    async def emit_delta(self, delta):
        pass

    async def emit_content(self, content):
        pass

    async def emit_reasoning(self, reasoning):
        pass

    async def emit_tool_call(self, tool_call):
        pass

    async def emit_stream_end(self, resuming=False):
        pass

    async def emit_complete(self, result):
        pass

    async def emit_error(self, error):
        pass


class TestAgentSessionSkills:
    @pytest.mark.asyncio
    async def test_process_message_without_skill_manager(self):
        agent = FakeAgent()
        # Empty base_system_prompt so AgentSession calls build_system_prompt
        cm = FakeContextManager(base_system_prompt="")
        tm = FakeToolManager()
        session = AgentSession(agent=agent, context_manager=cm, tool_manager=tm)
        result = await session.process_message(
            InputMessage(content="hello"),
            emitter=MinimalEmitter(),
            session_id="s1",
        )
        # FakeAgent echoes system_prompt; without skills it should be empty or base only
        assert "## Skills" not in result.content

    @pytest.mark.asyncio
    async def test_process_message_with_skill_manager(self):
        agent = FakeAgent()
        cm = FakeContextManager(base_system_prompt="")
        tm = FakeToolManager()
        source = InlineSkillSource([Skill(name="s1", content="skill body")])
        sm = SkillManager(source=source, builder=DefaultSkillBuilder())
        session = AgentSession(agent=agent, context_manager=cm, tool_manager=tm, skill_manager=sm)
        result = await session.process_message(
            InputMessage(content="hello"),
            emitter=MinimalEmitter(),
            session_id="s2",
        )
        assert 'name="s1"' in result.content
        assert "skill body" not in result.content

    @pytest.mark.asyncio
    async def test_process_message_with_empty_skills(self):
        agent = FakeAgent()
        cm = FakeContextManager(base_system_prompt="")
        tm = FakeToolManager()
        source = InlineSkillSource([])
        sm = SkillManager(source=source, builder=DefaultSkillBuilder())
        session = AgentSession(agent=agent, context_manager=cm, tool_manager=tm, skill_manager=sm)
        result = await session.process_message(
            InputMessage(content="hello"),
            emitter=MinimalEmitter(),
            session_id="s3",
        )
        assert "## Skills" not in result.content
