from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

import pytest
from bot.eval.agent_harness import (
    assemble_harness_agent,
    build_runtime_services,
    build_trace_only_services,
    static_system_prompt,
)
from bot.eval.task_spec import EvalToolset

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse
from modex_agent.runtime.models import JsonValue

STANDALONE_TOOLSETS: Final = (
    (EvalToolset.NONE, ()),
    (EvalToolset.READ_ONLY, ("read", "ls", "grep", "glob")),
    (EvalToolset.READ_WRITE, ("read", "write", "edit", "ls", "grep", "glob")),
    (EvalToolset.FULL, ("read", "write", "edit", "ls", "grep", "glob")),
)
STANDALONE_TOOLSETS_WITHOUT_BASH: Final = tuple(EvalToolset)
STANDALONE_SERVICES_HOOKS: Final = (
    (
        "RootSpanHook",
        "ChatSpanHook",
        "ToolSpanHook",
        "HandoffSpanHook",
        "ApprovalSpanHook",
        "AgentStartSpanHook",
        "IterationSpanHook",
        "loop_detection",
        "checkpoint",
    ),
    (
        "RootSpanHook",
        "ChatSpanHook",
        "ToolSpanHook",
        "HandoffSpanHook",
        "ApprovalSpanHook",
        "AgentStartSpanHook",
        "IterationSpanHook",
    ),
)
STANDALONE_SERVICES_GOVERNANCE: Final = (
    "CompositeGovernance",
    ("ContextBudgetGovernance", "ToolChainRepairGovernance"),
    None,
)
STANDALONE_STATIC_PROMPT: Final = "You are a helpful assistant."


class _UnusedProvider(CallbackStreamProvider):
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = messages, model, temperature, max_output_tokens, tools, kwargs
        raise AssertionError("characterization assembly must not call the provider")

    def get_default_model(self) -> str:
        return "fixture-model"


@pytest.mark.asyncio
async def test_standalone_assembly_face_pins_tools_services_and_static_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_FORMAT", "file")
    rosters: list[tuple[EvalToolset, tuple[str, ...]]] = []
    for toolset in EvalToolset:
        services = build_trace_only_services(tmp_path / f"{toolset.value}-traces")
        assembled = await assemble_harness_agent(
            workspace=tmp_path,
            data_dir=tmp_path / f"{toolset.value}-runtime",
            provider=_UnusedProvider(),
            toolset=toolset,
            deny_tools=[],
            runtime_services=services,
            governance_enabled=False,
        )
        rosters.append((toolset, tuple(assembled.tool_manager.list_tools())))
    production = build_runtime_services(tmp_path / "production-traces")
    await assemble_harness_agent(
        workspace=tmp_path,
        data_dir=tmp_path / "production-runtime",
        provider=_UnusedProvider(),
        toolset=EvalToolset.READ_WRITE,
        deny_tools=[],
        runtime_services=production,
        governance_enabled=True,
    )
    trace_only = build_trace_only_services(tmp_path / "trace-only")
    expected_rosters = tuple(
        (
            toolset,
            roster
            + (() if toolset is EvalToolset.NONE else ("bash",))
            + (() if toolset is EvalToolset.NONE or sys.platform == "win32" else ("bash_input",)),
        )
        for toolset, roster in STANDALONE_TOOLSETS
    )
    assert tuple(rosters) == expected_rosters
    assert tuple(toolset for toolset, roster in rosters if "bash" not in roster) == (
        EvalToolset.NONE,
    )
    assert production.hooks is not None
    assert trace_only.hooks is not None
    assert (
        tuple(spec.hook.name for spec in production.hooks.hook_specs),
        tuple(spec.hook.name for spec in trace_only.hooks.hook_specs),
    ) == STANDALONE_SERVICES_HOOKS
    assert production.governance is not None
    assert (
        type(production.governance).__name__,
        tuple(type(strategy).__name__ for strategy in production.governance._strategies),
        trace_only.governance,
    ) == STANDALONE_SERVICES_GOVERNANCE
    # Governance source defect documented; fixed in todo 8.
    assert static_system_prompt(STANDALONE_STATIC_PROMPT) == STANDALONE_STATIC_PROMPT
