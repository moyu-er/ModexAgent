from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest
from bot.eval.probes.generate import (
    GenerationConfig,
    GenerationStatus,
    LibraryConsistencyError,
    generate_library,
    load_frozen_library,
    validate_library,
)
from bot.eval.probes.renderer import (
    ConversationRender,
    QuestionRender,
    RenderedFactSentence,
    RenderedQuestion,
)
from bot.eval.probes.sampler import LibraryScale, config_for_scale, sample_world
from bot.eval.probes.schema import Speaker

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse
from modex_agent.trace.pricing import PriceBook, PriceEntry


class ScriptedCostProvider(CallbackStreamProvider):
    def __init__(self, outputs: list[str], *, tokens_per_call: int) -> None:
        super().__init__(retry_backoff_seconds=())
        self.outputs = deque(outputs)
        self.tokens_per_call = tokens_per_call
        self.calls = 0

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs,
    ) -> LLMResponse:
        del model, temperature, max_output_tokens, tools, kwargs
        self.calls += 1
        return LLMResponse(
            content=self.outputs.popleft(),
            usage={
                "prompt_tokens": self.tokens_per_call // 2,
                "completion_tokens": self.tokens_per_call // 2,
            },
        )

    def get_default_model(self) -> str:
        return "scripted-model"


def _outputs(seed: int) -> list[str]:
    world = sample_world(seed=seed, config=config_for_scale(LibraryScale.SMOKE))
    conversation = ConversationRender(
        sentences=[
            RenderedFactSentence(
                fact_id=fact.fact_id,
                speaker=Speaker.USER,
                text=f"Rendered {fact.fact_id}: {fact.value}",
            )
            for fact in world.facts
        ]
    )
    questions = QuestionRender(
        questions=[
            RenderedQuestion(
                probe_id=probe.probe_id,
                question=f"Rendered question for {probe.probe_id}?",
            )
            for probe in world.probes
        ]
    )
    return [conversation.model_dump_json(), questions.model_dump_json()]


def _pricebook() -> PriceBook:
    return PriceBook(
        models={
            "scripted-model": PriceEntry(
                input=10.0,
                output=10.0,
                cache_read=1.0,
                cache_write=1.0,
            )
        }
    )


@pytest.mark.asyncio
async def test_generator_completes_under_cost_cap_and_manifest_matches_library(
    tmp_path: Path,
) -> None:
    seed = 101
    provider = ScriptedCostProvider(_outputs(seed), tokens_per_call=1_000)
    config = GenerationConfig(
        library_scale=LibraryScale.SMOKE,
        seed=seed,
        max_cost_usd=0.10,
        output_dir=tmp_path,
        minimum_call_reserve_usd=0.001,
        renderer_max_output_tokens=500,
    )

    result = await generate_library(config, provider=provider, pricebook=_pricebook())
    world, manifest = load_frozen_library(result.library_path, result.manifest_path)

    assert result.status is GenerationStatus.COMPLETE
    assert result.spent_cost_usd == pytest.approx(0.02)
    assert manifest.complete is True
    assert manifest.total_probe_count == 7
    assert manifest.dual_arm_count == 2
    assert sum(manifest.type_counts.values()) == 5
    validate_library(world, manifest)


@pytest.mark.asyncio
async def test_generator_stops_near_cap_and_preserves_sampled_partial_library(
    tmp_path: Path,
) -> None:
    seed = 202
    provider = ScriptedCostProvider(_outputs(seed), tokens_per_call=1_000)
    config = GenerationConfig(
        library_scale=LibraryScale.SMOKE,
        seed=seed,
        max_cost_usd=0.015,
        output_dir=tmp_path,
        minimum_call_reserve_usd=0.006,
        renderer_max_output_tokens=500,
    )

    result = await generate_library(config, provider=provider, pricebook=_pricebook())
    world, manifest = load_frozen_library(result.library_path, result.manifest_path)

    assert result.status is GenerationStatus.COST_CAPPED
    assert result.spent_cost_usd == pytest.approx(0.01)
    assert provider.calls == 1
    assert manifest.complete is False
    assert manifest.status is GenerationStatus.COST_CAPPED
    assert len(world.probes) == 7
    assert world.probes[0].question.startswith("What should you remember")


@pytest.mark.asyncio
async def test_same_seed_and_script_reproduce_library_and_manifest_bytes(
    tmp_path: Path,
) -> None:
    seed = 303
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = await generate_library(
        GenerationConfig(
            library_scale=LibraryScale.SMOKE,
            seed=seed,
            max_cost_usd=0.10,
            output_dir=first_dir,
            minimum_call_reserve_usd=0.001,
            renderer_max_output_tokens=500,
        ),
        provider=ScriptedCostProvider(_outputs(seed), tokens_per_call=100),
        pricebook=_pricebook(),
    )
    second = await generate_library(
        GenerationConfig(
            library_scale=LibraryScale.SMOKE,
            seed=seed,
            max_cost_usd=0.10,
            output_dir=second_dir,
            minimum_call_reserve_usd=0.001,
            renderer_max_output_tokens=500,
        ),
        provider=ScriptedCostProvider(_outputs(seed), tokens_per_call=100),
        pricebook=_pricebook(),
    )

    assert first.library_path.read_bytes() == second.library_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()


@pytest.mark.asyncio
async def test_manifest_consistency_rejects_tampered_counts(tmp_path: Path) -> None:
    seed = 404
    result = await generate_library(
        GenerationConfig(
            library_scale=LibraryScale.SMOKE,
            seed=seed,
            max_cost_usd=0.10,
            output_dir=tmp_path,
            minimum_call_reserve_usd=0.001,
            renderer_max_output_tokens=500,
        ),
        provider=ScriptedCostProvider(_outputs(seed), tokens_per_call=100),
        pricebook=_pricebook(),
    )
    world, manifest = load_frozen_library(result.library_path, result.manifest_path)
    tampered = manifest.model_copy(update={"dual_arm_count": 99})

    with pytest.raises(LibraryConsistencyError, match="dual-arm"):
        validate_library(world, tampered)


@pytest.mark.asyncio
async def test_generator_reserves_declared_max_output_cost_before_dispatch(
    tmp_path: Path,
) -> None:
    seed = 505
    provider = ScriptedCostProvider(_outputs(seed), tokens_per_call=100)

    result = await generate_library(
        GenerationConfig(
            library_scale=LibraryScale.SMOKE,
            seed=seed,
            max_cost_usd=0.05,
            output_dir=tmp_path,
            minimum_call_reserve_usd=0.001,
            renderer_max_output_tokens=10_000,
        ),
        provider=provider,
        pricebook=_pricebook(),
    )

    assert result.status is GenerationStatus.COST_CAPPED
    assert provider.calls == 0
