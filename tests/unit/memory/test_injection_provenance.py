from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec

from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.memory.core.models import CoreMemoryContents, InjectionResult, MemoryBudget
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.hooks import SectionProvenance
from modex_agent.memory.injection.full_injection import FullInjectionPolicy
from modex_agent.memory.scope import MemoryContext


def _section() -> SectionProvenance:
    return SectionProvenance(
        source="core_memory",
        retrieved_tokens=40,
        injected_tokens=30,
        pruned_tokens=10,
        priority=100,
    )


def test_injection_result_preserves_existing_fields_with_provenance() -> None:
    messages = [ChatMessage(role=MessageRole.USER, content="keep this message")]
    provenance = [_section()]

    result = InjectionResult(
        system_prompt="keep this prompt",
        messages=messages,
        provenance=provenance,
    )

    assert result.system_prompt == "keep this prompt"
    assert result.messages == messages
    assert result.provenance == provenance


def test_injection_result_defaults_provenance_without_changing_existing_fields() -> None:
    messages = [ChatMessage(role=MessageRole.ASSISTANT, content="existing response")]

    result = InjectionResult(system_prompt="existing prompt", messages=messages)

    assert result.system_prompt == "existing prompt"
    assert result.messages == messages
    assert result.provenance == []


async def test_full_injection_reports_honest_section_trimming_provenance() -> None:
    memory_system = create_autospec(MemorySystem, instance=True)
    memory_system.retrieve_core_memory = AsyncMock(
        return_value=CoreMemoryContents(soul="identity " * 40)
    )
    memory_system.get_core_memory_directory = AsyncMock(return_value=None)
    memory_system.get_history = AsyncMock(
        return_value=[ChatMessage(role=MessageRole.USER, content="hello")]
    )
    policy = FullInjectionPolicy(budget=MemoryBudget(max_system_prompt_tokens=50))

    result = await policy.assemble(
        context=MemoryContext(session_id="session-provenance"),
        memory_system=memory_system,
    )

    assert result.messages == [ChatMessage(role=MessageRole.USER, content="hello")]
    assert [section.source for section in result.provenance] == ["disclaimer", "core_memory"]
    assert [section.priority for section in result.provenance] == [110, 100]
    assert all(
        section.retrieved_tokens == section.injected_tokens + section.pruned_tokens
        for section in result.provenance
    )
    assert any(section.pruned_tokens > 0 for section in result.provenance)
