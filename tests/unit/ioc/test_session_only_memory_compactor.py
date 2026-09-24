"""Subagent session-only memory gets a real compactor from its effective provider.

Locks the ADR-0050 chain-completion for subagents: ``build_session_only_memory``
threads the subagent's effective LLM provider into ``build_session_compactor``,
so subagent cleanup produces an LLM compact summary instead of degrading to
tail-only (previously the builder passed no provider at all).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.factories.descriptors import build_session_only_memory
from modex_agent.ioc.factories.memory import build_session_compactor
from modex_agent.memory.cleanup import CleanupResult
from modex_agent.memory.hooks import CleanupFinishedHook, MemoryHookContext
from modex_agent.memory.scope import MemoryAgentRole


class _SummaryProvider(CallbackStreamProvider):
    """Returns one canned summary; records that the compactor called it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        del on_content_delta, on_reasoning_delta
        self.calls += 1
        return LLMResponse(content="## Objective\nSubagent compact summary")

    def get_default_model(self) -> str:
        return "summary-model"


def _cfg(max_context_tokens: int = 8000, compact_enabled: bool = True) -> MemoryConfig:
    return MemoryConfig.model_validate(
        {
            "session": {
                "max_context_tokens": max_context_tokens,
                "max_token_ratio": 0.85,
                "max_output_tokens": 4000,
            },
            "compact": {"enabled": compact_enabled},
        }
    )


def test_build_session_compactor_gates() -> None:
    # No provider → degraded tail-only (None, even when enabled).
    assert build_session_compactor(_cfg(), None) is None
    # Disabled → None regardless of provider.
    assert build_session_compactor(_cfg(compact_enabled=False), _SummaryProvider()) is None
    # Enabled + provider → a real compactor.
    assert build_session_compactor(_cfg(), _SummaryProvider()) is not None


async def test_subagent_cleanup_generates_summary_with_effective_provider(
    tmp_path: Path,
) -> None:
    """Over-budget append on a subagent session triggers cleanup that
    GENERATES a summary via the subagent's effective provider (compact_generated
    True) and prunes the history — not the pre-fix tail-only degradation."""
    provider = _SummaryProvider()
    cm = build_session_only_memory(
        _cfg(max_context_tokens=8000),
        tmp_path,
        "sub",
        MemoryAgentRole.SUBAGENT,
        llm_provider=provider,
    )
    await cm.memory_system.initialize()

    results: list[CleanupResult] = []

    class _Recorder(CleanupFinishedHook):
        async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
            if ctx.cleanup_result is not None:
                results.append(ctx.cleanup_result)

    cm.memory_system.add_cleanup_hook(_Recorder())

    state = await cm.load("session-sub")
    # Alternating user/assistant turns (a safe compaction boundary needs the
    # turn structure) push the session far past the budget.
    for _ in range(6):
        await state.history.append(
            ChatMessage(role=MessageRole.USER, content="x" * 400, token_count=400)
        )
        await state.history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="y" * 400, token_count=400)
        )

    assert provider.calls >= 1, "the effective provider must generate the summary"
    assert any(r.compact_generated for r in results if r.triggered), (
        "subagent cleanup must produce an LLM compact summary"
    )
    # History pruned: the visible history is strictly shorter than what was
    # appended (COMPACT is excluded from the history view by design).
    assert len(await state.history.to_list()) < 12
