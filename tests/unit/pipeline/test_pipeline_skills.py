"""Unit tests for AgentPipeline skill-resolver integration."""

import asyncio

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.plugins.defaults.capabilities.skills.catalog import SkillCatalog
from modex_agent.plugins.defaults.capabilities.skills.models import Skill
from modex_agent.plugins.defaults.capabilities.skills.source import InlineSkillSource

from tests.unit.pipeline._helpers import _make_react_pipeline


class FakeAgent:
    name = "FakeAgent"

    async def run(self, context, emitter, streaming=True):
        return type(
            "Result",
            (),
            {"content": f"System was: {context.system_prompt}", "reasoning": "", "messages": []},
        )()


class FakeContextManager:
    def __init__(self, base_system_prompt=""):
        self.base_system_prompt = base_system_prompt

    async def load(self, session_id, **kwargs):
        from modex_agent.core.context import ContextState

        return ContextState(system_prompt=self.base_system_prompt, history=[])

    async def save(self, session_id, user_message, assistant_result, metadata=None):
        pass

    async def build_system_prompt(self, tool_manager, runtime_info=None):
        parts = [self.base_system_prompt]
        if runtime_info:
            parts.append(str(runtime_info))
        return "\n\n---\n\n".join(parts)

    async def clear(self, session_id):
        pass


class FakeToolManager:
    async def startup(self):
        pass

    async def shutdown(self):
        pass

    def list_tools(self):
        return []


class FakeInputAdapter:
    name = "FakeInput"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def receive(self):
        from modex_agent.pipeline.pipeline import InputMessage

        yield InputMessage(
            content="hi",
            source="test",
            session=SessionInfo.from_str("test"),
        )
        await asyncio.sleep(10)  # block indefinitely after first message


class FakeOutputAdapter:
    name = "FakeOutput"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, message, session_id):
        pass

    async def send_delta(self, delta, session_id):
        pass

    async def flush_deltas(self, session_id):
        pass

    @property
    def supports_streaming(self):
        return False


class TestAgentPipelineSkills:
    @pytest.mark.asyncio
    async def test_pipeline_stores_skill_resolver(self):
        cm = FakeContextManager(base_system_prompt="Base")
        tm = FakeToolManager()
        source = InlineSkillSource([Skill(name="ps1", content="pipeline skill")])
        catalog = SkillCatalog(source=source)
        pipeline = _make_react_pipeline(
            agent=FakeAgent(),
            context_manager=cm,
            tool_manager=tm,
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            skill_resolver=catalog,
        )
        assert pipeline.skill_resolver is catalog

    @pytest.mark.asyncio
    async def test_pipeline_without_skill_resolver(self):
        cm = FakeContextManager(base_system_prompt="Base")
        tm = FakeToolManager()
        pipeline = _make_react_pipeline(
            agent=FakeAgent(),
            context_manager=cm,
            tool_manager=tm,
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            skill_resolver=None,
        )
        assert pipeline.skill_resolver is None

    @pytest.mark.asyncio
    async def test_pipeline_sanitizer_can_be_disabled_with_none(self):
        cm = FakeContextManager(base_system_prompt="Base")
        tm = FakeToolManager()
        pipeline = _make_react_pipeline(
            agent=FakeAgent(),
            context_manager=cm,
            tool_manager=tm,
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            sanitizer=None,
        )
        assert pipeline.sanitizer is None
