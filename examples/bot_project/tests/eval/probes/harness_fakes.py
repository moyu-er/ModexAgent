from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bot.eval.probes.generate import GenerationManifest, GenerationStatus
from bot.eval.probes.harness import (
    ExperimentItem,
    ExperimentSetup,
    ProbeHarnessConfig,
    ProbeHarnessServices,
    ProbeRunRecord,
    ProbeScore,
)
from bot.eval.probes.renderer import RendererConfig
from bot.eval.probes.sampler import LibraryScale, config_for_scale, sample_world
from bot.eval.probes.schema import ProbeType, WorldSpec

from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.trace.pricing import PriceBook, PriceEntry
from modex_agent.trace.score_injector import L2ScoreInjector, ScoreSpec


class ScriptedAnswerProvider(CallbackStreamProvider):
    def __init__(self, *, tokens_per_call: int = 100) -> None:
        super().__init__(retry_backoff_seconds=())
        self.tokens_per_call = tokens_per_call
        self.questions: list[str] = []
        self.tool_arguments: list[list[dict[str, Any]] | None] = []

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
        del model, temperature, max_output_tokens, kwargs
        question = str(messages[-1].content)
        self.questions.append(question)
        self.tool_arguments.append(tools)
        return LLMResponse(
            content=f"scripted answer for {question}",
            usage={
                "prompt_tokens": self.tokens_per_call // 2,
                "completion_tokens": self.tokens_per_call // 2,
            },
        )

    def get_default_model(self) -> str:
        return "scripted-model"


class RecordingScoreInjector(L2ScoreInjector):
    def __init__(self) -> None:
        super().__init__(ingestion_url="http://unused.invalid", headers={})
        self.batches: list[tuple[str, list[ScoreSpec], str | None]] = []

    async def inject_score_batch(
        self,
        trace_id: str,
        scores: list[ScoreSpec],
        *,
        observation_id: str | None = None,
    ) -> None:
        self.batches.append((trace_id, scores, observation_id))


def write_five_probe_library(root: Path) -> tuple[Path, Path, WorldSpec]:
    sampled = sample_world(seed=404, config=config_for_scale(LibraryScale.SMOKE))
    probes = [probe for probe in sampled.probes if not probe.dual_arm]
    world = WorldSpec.model_validate(
        sampled.model_dump(mode="json") | {"probes": [probe.model_dump(mode="json") for probe in probes]}
    )
    counts = Counter(probe.probe_type for probe in world.probes)
    manifest = GenerationManifest(
        suite_version=world.suite_version,
        library_scale=LibraryScale.SMOKE,
        seed=world.seed,
        generation_parameters=config_for_scale(LibraryScale.SMOKE),
        renderer_config=RendererConfig(),
        type_counts={probe_type: counts[probe_type] for probe_type in ProbeType},
        dual_arm_count=0,
        total_probe_count=len(world.probes),
        generation_model="scripted-model",
        max_cost_usd=1.0,
        spent_cost_usd=0.0,
        complete=True,
        status=GenerationStatus.COMPLETE,
    )
    library_path = root / "frozen.jsonl"
    manifest_path = root / "manifest.json"
    library_path.write_text(world.model_dump_json() + "\n", encoding="utf-8")
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    return library_path, manifest_path, world


def harness_config(root: Path, library_path: Path, manifest_path: Path) -> ProbeHarnessConfig:
    return ProbeHarnessConfig(
        library_path=library_path,
        manifest_path=manifest_path,
        workspace=root / "run",
        checkpoint_path=root / "checkpoint.jsonl",
        snapshot_path=root / "snapshot.json",
        run_name="memory-probes.test-run",
        max_cost_usd=1.0,
        minimum_call_reserve_usd=0.001,
        answer_max_output_tokens=100,
    )


def pricebook() -> PriceBook:
    return PriceBook(
        models={
            "scripted-model": PriceEntry(
                input=1.0,
                output=1.0,
                cache_read=0.0,
                cache_write=0.0,
            )
        }
    )


async def scripted_experiment_setup(world: WorldSpec, run_name: str) -> ExperimentSetup:
    return ExperimentSetup(
        dataset_id="dataset-memory-probes",
        experiment_id="experiment-memory-probes",
        experiment_name=run_name,
        ingest_item_id="item-ingest",
        probe_items=[
            ExperimentItem(probe_id=probe.probe_id, item_id=f"item-{probe.probe_id}")
            for probe in world.probes
        ],
    )


def passing_score(record: ProbeRunRecord) -> ProbeScore:
    return ProbeScore(
        name=f"memory_probe_{record.probe.probe_type.value}",
        value=1.0,
        data_type="NUMERIC",
    )


def services(
    provider: ScriptedAnswerProvider,
    injector: RecordingScoreInjector,
    *,
    score_fn: Callable[[ProbeRunRecord], ProbeScore] = passing_score,
    experiment_setup: Callable[[WorldSpec, str], Awaitable[ExperimentSetup]] = (
        scripted_experiment_setup
    ),
) -> ProbeHarnessServices:
    return ProbeHarnessServices(
        provider=provider,
        pricebook=pricebook(),
        score_fn=score_fn,
        experiment_setup=experiment_setup,
        score_injector=injector,
    )
