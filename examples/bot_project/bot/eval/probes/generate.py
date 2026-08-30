"""CLI and persistence boundary for deterministic frozen probe libraries."""

from __future__ import annotations

import os
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import anyio
import typer
from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes.budget import BudgetConfig, BudgetedProvider, CostCapExceededError
from bot.eval.probes.renderer import ProbeRenderer, RendererConfig
from bot.eval.probes.sampler import LibraryScale, SamplerConfig, config_for_scale, sample_world
from bot.eval.probes.schema import ProbeType, WorldSpec
from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.trace.pricing import PriceBook, load_pricebook


class GenerationStatus(StrEnum):
    """Terminal state recorded in a generation manifest."""

    COMPLETE = "complete"
    COST_CAPPED = "cost_capped"


class GenerationConfig(BaseModel):
    """Inputs that fully determine one generation dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library_scale: LibraryScale
    seed: int
    max_cost_usd: float = Field(gt=0)
    output_dir: Path
    minimum_call_reserve_usd: float = Field(default=0.005, gt=0)
    renderer_max_output_tokens: int = Field(default=16_000, ge=1)


class GenerationManifest(BaseModel):
    """Auditable counts, parameters, model, budget, and completion state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_version: str
    library_scale: LibraryScale
    seed: int
    generation_parameters: SamplerConfig
    renderer_config: RendererConfig
    type_counts: dict[ProbeType, int]
    dual_arm_count: int = Field(ge=0)
    total_probe_count: int = Field(ge=0)
    generation_model: str
    max_cost_usd: float = Field(gt=0)
    spent_cost_usd: float = Field(ge=0)
    complete: bool
    status: GenerationStatus


class GenerationResult(BaseModel):
    """Files and cost state returned to CLI and tests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library_path: Path
    manifest_path: Path
    status: GenerationStatus
    spent_cost_usd: float = Field(ge=0)


class GeneratorConfigurationError(RuntimeError):
    """A required generation-provider environment variable is absent."""

    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(f"{variable} is required to build the probe generation provider")


class LibraryConsistencyError(RuntimeError):
    """A frozen world and its manifest disagree."""


def build_generation_provider_from_env() -> LLMProvider:
    """Build the explicit rendering provider from independent probe env vars."""
    model = os.environ.get("PROBE_GENERATOR_MODEL")
    if not model:
        raise GeneratorConfigurationError("PROBE_GENERATOR_MODEL")
    return create_llm_provider(
        LLMConfig(
            model=model,
            api_key=os.environ.get("PROBE_GENERATOR_API_KEY") or "",
            base_url=os.environ.get("PROBE_GENERATOR_BASE_URL") or "",
        )
    )


async def generate_library(
    config: GenerationConfig,
    *,
    provider: LLMProvider,
    pricebook: PriceBook,
) -> GenerationResult:
    """Sample, checkpoint, render, and finalize one frozen library."""
    sampler_config = config_for_scale(config.library_scale)
    sampled = sample_world(seed=config.seed, config=sampler_config)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    library_path = config.output_dir / "frozen_v1.jsonl"
    manifest_path = config.output_dir / "manifest_v1.json"
    _write_world(library_path, sampled)
    budgeted = BudgetedProvider(
        provider,
        pricebook,
        BudgetConfig(
            max_cost_usd=config.max_cost_usd,
            minimum_call_reserve_usd=config.minimum_call_reserve_usd,
        ),
    )
    renderer_config = RendererConfig(max_output_tokens=config.renderer_max_output_tokens)
    try:
        rendered = await ProbeRenderer(budgeted, renderer_config).render(sampled)
    except CostCapExceededError:
        status = GenerationStatus.COST_CAPPED
        final_world = sampled
    else:
        status = GenerationStatus.COMPLETE
        final_world = rendered.world
        _write_world(library_path, final_world)
    main_counts = Counter(probe.probe_type for probe in final_world.probes if not probe.dual_arm)
    manifest = GenerationManifest(
        suite_version=final_world.suite_version,
        library_scale=config.library_scale,
        seed=config.seed,
        generation_parameters=sampler_config,
        renderer_config=renderer_config,
        type_counts={probe_type: main_counts[probe_type] for probe_type in ProbeType},
        dual_arm_count=sum(probe.dual_arm for probe in final_world.probes),
        total_probe_count=len(final_world.probes),
        generation_model=provider.get_default_model(),
        max_cost_usd=config.max_cost_usd,
        spent_cost_usd=budgeted.spent_cost_usd,
        complete=status is GenerationStatus.COMPLETE,
        status=status,
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    validate_library(final_world, manifest)
    return GenerationResult(
        library_path=library_path,
        manifest_path=manifest_path,
        status=status,
        spent_cost_usd=budgeted.spent_cost_usd,
    )


def load_frozen_library(
    library_path: Path,
    manifest_path: Path,
) -> tuple[WorldSpec, GenerationManifest]:
    """Parse a one-record JSONL library and validate its manifest."""
    lines = [line for line in library_path.read_text(encoding="utf-8").splitlines() if line]
    if len(lines) != 1:
        raise LibraryConsistencyError(
            f"frozen library must contain exactly one world record, found {len(lines)}"
        )
    world = WorldSpec.model_validate_json(lines[0])
    manifest = GenerationManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    validate_library(world, manifest)
    return world, manifest


def validate_library(world: WorldSpec, manifest: GenerationManifest) -> None:
    """Reject count or identity drift between a library and manifest."""
    main_counts = Counter(probe.probe_type for probe in world.probes if not probe.dual_arm)
    actual_counts = {probe_type: main_counts[probe_type] for probe_type in ProbeType}
    if actual_counts != manifest.type_counts:
        raise LibraryConsistencyError(
            f"probe type counts differ: library={actual_counts}, manifest={manifest.type_counts}"
        )
    dual_count = sum(probe.dual_arm for probe in world.probes)
    if dual_count != manifest.dual_arm_count:
        raise LibraryConsistencyError(
            f"dual-arm count differs: library={dual_count}, manifest={manifest.dual_arm_count}"
        )
    if len(world.probes) != manifest.total_probe_count:
        raise LibraryConsistencyError(
            f"total probe count differs: library={len(world.probes)}, "
            f"manifest={manifest.total_probe_count}"
        )
    if world.seed != manifest.seed or world.suite_version != manifest.suite_version:
        raise LibraryConsistencyError("world identity differs from manifest")


def _write_world(path: Path, world: WorldSpec) -> None:
    path.write_text(world.model_dump_json() + "\n", encoding="utf-8")


app = typer.Typer(add_completion=False)


@app.command()
def main(
    library_scale: Annotated[LibraryScale, typer.Option("--library-scale")],
    seed: Annotated[int, typer.Option("--seed")],
    max_cost: Annotated[float, typer.Option("--max-cost", min=0.000001)],
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("evals/probes"),
) -> None:
    """Generate a frozen probe library outside CI."""
    config = GenerationConfig(
        library_scale=library_scale,
        seed=seed,
        max_cost_usd=max_cost,
        output_dir=output_dir,
    )
    provider = build_generation_provider_from_env()
    pricebook = load_pricebook(yml_path=Path("config/model_prices.yml"))
    result = anyio.run(_run_generation, config, provider, pricebook)
    typer.echo(
        f"status={result.status.value} spent=${result.spent_cost_usd:.6f} "
        f"library={result.library_path} manifest={result.manifest_path}"
    )
    if result.status is GenerationStatus.COST_CAPPED:
        raise typer.Exit(code=2)


async def _run_generation(
    config: GenerationConfig,
    provider: LLMProvider,
    pricebook: PriceBook,
) -> GenerationResult:
    return await generate_library(config, provider=provider, pricebook=pricebook)


if __name__ == "__main__":
    app()
