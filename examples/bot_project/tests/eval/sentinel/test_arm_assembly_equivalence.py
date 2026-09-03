from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from bot.eval.harbor.memory_workspace import (
    MemoryArm,
    MemoryWorkspaceRequest,
    build_memory_workspace,
)
from bot.eval.memory_harness import build_memory_runtime_services
from bot.eval.sentinel.execution import HostSentinelExecutionPlane
from bot.eval.sentinel.orchestrator import SentinelInstance
from evals.sentinel.tasks import MEMORY_CHAIN_V1_CHAIN, SentinelArm

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse
from modex_agent.trace.experiment_attrs import ExperimentLinkage

PINNED_SENTINEL_TOOLS: Final = (
    "read",
    "write",
    "edit",
    "ls",
    "grep",
    "glob",
)
PINNED_SENTINEL_MEMORY_CONFIG: Final = (
    '{"session":{"max_context_tokens":32000,"max_token_ratio":0.85,'
    '"keep_ratio":0.3,"max_output_tokens":0},"compact":{"enabled":true,'
    '"max_output_tokens":8192,"max_iterations":3,"temperature":0.2,'
    '"tool_output_max_chars":2000},"archive":{"enabled":true,'
    '"max_entries":1000,"retained_consumed_pairs":3,"max_archive_count":10,'
    '"max_archive_total":20,"max_archive_inject":3,'
    '"archive_inject_max_chars":20000,"archive_inject_step_chars":5000,'
    '"archive_inject_min_chars":5000,"scope":["user"]},"core":'
    '{"enabled":true,"default_templates_dir":null,"scope":["user"]},'
    '"dream_engine":{"enabled":true,"interval":1200,'
    '"max_consume_per_run":3},"summarizer_agent":null,"retention":'
    '{"min_recent_user_turns":2,"min_recent_agent_turns":1,'
    '"recent_tool_result_count":3},"governance":{"tool_chain_repair":true,'
    '"budget":{"governance_ratio":0.6,"protect_tokens":40000,'
    '"min_gain_tokens":20000,"keep_recent":10,"whitelist_tools":[]}},'
    '"pruned":{"enabled":true,"max_files":50,"topic_max_chars":200}}'
)


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
        raise AssertionError("sentinel assembly must not call the provider")

    def get_default_model(self) -> str:
        return "sentinel-assembly-pin"


def test_declared_sentinel_arm_assembly_seam_is_available() -> None:
    # Given / When
    assembly_seam = getattr(HostSentinelExecutionPlane, "_assemble_arm", None)

    # Then
    assert callable(assembly_seam)


@pytest.mark.parametrize("arm", tuple(SentinelArm))
async def test_declared_sentinel_arm_assembly_matches_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arm: SentinelArm,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    workspace = build_memory_workspace(
        MemoryWorkspaceRequest(
            root=tmp_path,
            arm=MemoryArm(arm.value),
            namespace=f"sentinel-pin.{arm.value}",
            instance_id=f"{arm.value}-1",
        )
    )
    workspace.mount.host_path.mkdir(parents=True)
    instance = SentinelInstance(
        instance_id=f"{arm.value}-1",
        arm=arm,
        experiment_name=f"sentinel-pin.{arm.value}",
        seed=731,
        task=MEMORY_CHAIN_V1_CHAIN.tasks[0],
        workspace=workspace,
    )
    linkage = ExperimentLinkage(
        experiment_id="sentinel-pin",
        experiment_name=f"sentinel-pin.{arm.value}",
        dataset_id="sentinel-pin",
        item_id="sentinel-pin",
    )
    execution = HostSentinelExecutionPlane(
        _NoCallProvider(),
        lambda _instance: linkage,
        run_ref="sentinel-pin",
    )

    # When
    assembled, _runtime_services = await execution._assemble_arm(
        instance,
        build_memory_runtime_services,
        linkage,
    )

    # Then
    try:
        assert tuple(assembled.tool_manager.list_tools()) == PINNED_SENTINEL_TOOLS
        assert assembled.descriptor.max_iterations == 25
        memory_config = assembled.descriptor.memory_config
        if arm is SentinelArm.MEMORY:
            assert memory_config.model_dump_json() == PINNED_SENTINEL_MEMORY_CONFIG
        else:
            assert memory_config.session.max_context_tokens == 32_000
            assert memory_config.archive is None
            assert memory_config.core is None
            assert memory_config.dream_engine is None
    finally:
        await assembled.close()
