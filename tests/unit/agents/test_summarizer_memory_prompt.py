from __future__ import annotations

from modex_agent.agents.summarizer.agent import SummarizerAgent


def test_memory_compression_prompt_has_handoff_sections() -> None:
    prompt = SummarizerAgent.PROMPT_MEMORY_COMPRESSION

    assert "Active Task" in prompt
    assert "Pending User Asks" in prompt
    assert "Agent Inputs" in prompt
    assert "Completed Actions" in prompt
    assert "Remaining Work" in prompt
    assert "Important Tool Results" in prompt
    assert "Relevant Files/Artifacts" in prompt
    assert "reference context" in prompt.lower()
