"""Manual B5 dispatch assembly with one end-to-end cost cap."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

from bot.eval.live_gates.b3_linkage_runtime import run_preflight
from bot.eval.probes._dispatch_models import (
    ProbeDispatchError,
    ProbeDispatchOptions,
    ProbeRunEnvironment,
)
from bot.eval.probes._experiment_api import ExperimentQuery, poll_experiment_dump
from bot.eval.probes._harness_langfuse import (
    LangfuseExperimentConfig,
    LangfuseExperimentRegistrar,
)
from bot.eval.probes._harness_models import (
    ProbeHarnessConfig,
    ProbeHarnessServices,
    ProbeRunRecord,
    ScoreFn,
)
from bot.eval.probes._harness_models import ProbeScore as HarnessProbeScore
from bot.eval.probes.dual_arm import (
    NomemoryRunConfig,
    NomemoryRunResult,
    NomemoryRunServices,
    run_nomemory_controls,
)
from bot.eval.probes.evidence import (
    B5EvidenceInput,
    ServicePreflight,
    assemble_b5_evidence,
    score_record,
)
from bot.eval.probes.generate import load_frozen_library
from bot.eval.probes.harness import run_probe_harness
from bot.eval.probes.schema import WorldSpec
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.trace.pricing import TOKENS_PER_MILLION, PriceBook, load_pricebook
from modex_agent.trace.score_injector import L2ScoreInjector

_EVIDENCE_PATH: Final = Path("evals/evidence/b5_first_run.json")


async def dispatch_probe_run(options: ProbeDispatchOptions) -> Path:
    """Run the memory arm, bounded controls, API dump, and evidence write."""
    load_dotenv(Path(".env"), override=False)
    environment = ProbeRunEnvironment.from_mapping(os.environ)
    preflight = run_preflight()
    if preflight.missing:
        missing = ", ".join(preflight.missing)
        raise ProbeDispatchError(f"live preflight failed; missing: {missing}")

    world, _manifest = load_frozen_library(options.library, options.manifest)
    pricebook = load_pricebook(yml_path=Path("config/model_prices.yml"))
    provider = create_llm_provider(
        LLMConfig(
            model=environment.model,
            api_key=environment.api_key,
            base_url=environment.base_url,
            temperature=0.0,
            max_output_tokens=environment.answer_max_output_tokens,
        )
    )
    memory_cap = memory_arm_cost_cap(options.max_cost_usd, world, environment, pricebook)
    score_fn = deterministic_score_fn(world)
    auth = base64.b64encode(
        f"{environment.langfuse_public_key}:{environment.langfuse_secret_key}".encode()
    ).decode("ascii")
    injector = L2ScoreInjector(
        ingestion_url=(f"{environment.langfuse_host.rstrip('/')}/api/public/ingestion"),
        headers={"Authorization": f"Basic {auth}"},
    )
    run_root = (
        Path("evals/runs/probes") / hashlib.sha256(options.run_name.encode()).hexdigest()[:12]
    )
    harness_result = None
    nomemory_result = NomemoryRunResult(results=[], spent_cost_usd=0.0, cost_capped=False)
    try:
        harness_result = await run_probe_harness(
            ProbeHarnessConfig(
                library_path=options.library,
                manifest_path=options.manifest,
                workspace=run_root / "workspace",
                checkpoint_path=run_root / "checkpoint.jsonl",
                snapshot_path=run_root / "snapshot.json",
                run_name=options.run_name,
                max_cost_usd=memory_cap,
                minimum_call_reserve_usd=environment.minimum_call_reserve_usd,
                answer_max_output_tokens=environment.answer_max_output_tokens,
            ),
            ProbeHarnessServices(
                provider=provider,
                pricebook=pricebook,
                score_fn=score_fn,
                experiment_setup=LangfuseExperimentRegistrar(
                    LangfuseExperimentConfig(
                        host=environment.langfuse_host,
                        public_key=environment.langfuse_public_key,
                        secret_key=environment.langfuse_secret_key,
                        dataset_name=environment.dataset_name,
                    )
                ),
                score_injector=injector,
            ),
        )
        remaining_cost = options.max_cost_usd - harness_result.spent_cost_usd
        if remaining_cost > 0:
            nomemory_result = await run_nomemory_controls(
                harness_result.records,
                NomemoryRunConfig(
                    max_cost_usd=remaining_cost,
                    minimum_call_reserve_usd=environment.minimum_call_reserve_usd,
                    answer_max_output_tokens=environment.answer_max_output_tokens,
                ),
                NomemoryRunServices(
                    provider=provider,
                    pricebook=pricebook,
                    score_fn=score_fn,
                ),
            )
        else:
            nomemory_result = nomemory_result.model_copy(update={"cost_capped": True})
    finally:
        await injector.aclose()

    experiment_dump = await poll_experiment_dump(
        ExperimentQuery(
            host=environment.langfuse_host,
            public_key=environment.langfuse_public_key,
            secret_key=environment.langfuse_secret_key,
            run_name=options.run_name,
        )
    )
    evidence = assemble_b5_evidence(
        B5EvidenceInput(
            run_name=options.run_name,
            max_cost_usd=options.max_cost_usd,
            world=world,
            harness_result=harness_result,
            nomemory_result=nomemory_result,
            experiment_compare=experiment_dump,
            preflight=ServicePreflight.model_validate(preflight.model_dump()),
        )
    )
    _EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE_PATH.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    return _EVIDENCE_PATH


def memory_arm_cost_cap(
    max_cost_usd: float,
    world: WorldSpec,
    environment: ProbeRunEnvironment,
    pricebook: PriceBook,
) -> float:
    """Reserve one governed answer call for each no-memory control."""
    price = pricebook.match(environment.model)
    if price is None:
        raise ProbeDispatchError(f"answer model has no price entry: {environment.model}")
    output_reserve = (environment.answer_max_output_tokens / TOKENS_PER_MILLION) * price.output
    per_call = max(environment.minimum_call_reserve_usd, output_reserve)
    control_reserve = sum(probe.dual_arm for probe in world.probes) * per_call
    memory_cap = max_cost_usd - control_reserve
    if memory_cap <= 0:
        raise ProbeDispatchError(
            f"max cost ${max_cost_usd:.6f} cannot reserve ${control_reserve:.6f} "
            "for dual-arm controls"
        )
    return memory_cap


def deterministic_score_fn(world: WorldSpec) -> ScoreFn:
    """Adapt ticket 23's typed verdict to the harness score contract."""

    def score(record: ProbeRunRecord) -> HarnessProbeScore:
        result = score_record(world, record)
        if result.passed is None:
            raise ProbeDispatchError(
                f"deterministic score unavailable for probe {record.probe.probe_id}"
            )
        return HarnessProbeScore(
            name=f"memory_probe_{record.probe.probe_type.value}",
            value=result.passed,
            data_type="BOOLEAN",
        )

    return score


__all__ = [
    "ProbeDispatchError",
    "ProbeDispatchOptions",
    "ProbeRunEnvironment",
    "deterministic_score_fn",
    "dispatch_probe_run",
    "memory_arm_cost_cap",
    "poll_experiment_dump",
]
