from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bot.eval.memory_harness import build_memory_runtime_services
from bot.service.pool.declaration import boot_scope_spec

from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.scope import MemoryAgentRole
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.tools.presets import ToolPreset

_BOT_PROJECT = Path(__file__).resolve().parents[2]
_DECLARATION_PATH = _BOT_PROJECT / "config" / "scopes" / "eval" / "agents" / "memory-harness.yml"


class _NoCallProvider(CallbackStreamProvider):
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
        raise AssertionError("memory stack assembly must not call the provider")

    def get_default_model(self) -> str:
        return "memory-harness-pin"


def test_memory_harness_declaration_compiles_the_pinned_stack(tmp_path: Path) -> None:
    # Given
    declaration = load_scope_declaration(_DECLARATION_PATH)

    # When
    boot = boot_scope_spec(
        declaration,
        project_dir=_BOT_PROJECT,
        data_dir=tmp_path,
        graphs_dirs=(),
        default_llm_provider="default",
    )
    compiled = boot.compilation.agents[0]

    # Then
    assert len(boot.compilation.agents) == 1
    assert compiled.provenance.pool == "memory-harness"
    assert compiled.provenance.agent == "react"
    assert compiled.defaults.toolset_profile is ToolPreset.READ_WRITE
    assert compiled.defaults.archive_enabled is True
    assert compiled.defaults.core_enabled is True
    assert compiled.spec.memory_overrides.max_context_tokens == 32_000


async def test_memory_harness_stack_observables_are_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    base_prompt = "<memory-harness-base/>"

    # When
    bundle = await build_memory_runtime_services(
        tmp_path,
        _NoCallProvider(),
        base_prompt,
    )

    # Then
    try:
        assert isinstance(bundle.memory_system, DefaultMemorySystem)
        assert bundle.memory_config.session.max_context_tokens == 32_000
        assert bundle.memory_config.archive is not None
        assert bundle.memory_config.archive.enabled is True
        assert bundle.memory_config.core is not None
        assert bundle.memory_config.core.enabled is True
        assert bundle.memory_config.dream_engine is not None
        assert bundle.memory_config.dream_engine.enabled is True
        assert bundle.memory_config.governance is not None
        assert bundle.memory_config.governance.model_dump() == {
            "tool_chain_repair": True,
            "budget": {
                "governance_ratio": 0.6,
                "protect_tokens": 40_000,
                "min_gain_tokens": 20_000,
                "keep_recent": 10,
                "whitelist_tools": set(),
            },
        }
        governance = bundle.runtime_services.governance
        assert governance is not None
        assert type(governance).__name__ == "CompositeGovernance"
        assert tuple(type(strategy).__name__ for strategy in governance._strategies) == (
            "ContextBudgetGovernance",
            "ToolChainRepairGovernance",
        )
        assert bundle.context_manager.default_agent_id == "react"
        assert bundle.context_manager.default_agent_role is MemoryAgentRole.MAIN
        assert bundle.context_manager.base_system_prompt == base_prompt
    finally:
        if bundle.runtime_services.hooks is not None:
            await bundle.runtime_services.hooks.aclose()
        await bundle.assembly.close()
