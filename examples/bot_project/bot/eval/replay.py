"""Offline golden-case replay with strict fingerprint and oracle gates."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from bot.eval.agent_harness import (
    assemble_harness_agent,
    build_trace_only_services,
    static_system_prompt,
)
from bot.eval.experiment_runner import EvalRunner
from bot.eval.task_output import EvalTaskOutput, ToolStats, TurnRecord, WorldResult
from bot.eval.task_spec import EvalItemSpec
from modex_agent.core.emitter import StopReason
from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.tool_manager import ToolManager
from modex_agent.trace.cassette import (
    CassetteEntry,
    CassetteManifest,
    CassetteReplayEngine,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FingerprintValues(_FrozenModel):
    model: str
    temperature: float
    tool_names: list[str]
    tool_schema_sha256: str
    prompt_sha256: str
    platform: str


class GoldenMeta(FingerprintValues):
    recorded_at: str
    baseline: bool = False


class GoldenCase(_FrozenModel):
    name: str
    dir: Path


class CaseResult(_FrozenModel):
    case: str
    fingerprint_ok: bool
    cassette_misses: int
    turn_outcomes: list[TurnRecord]
    world_results: list[WorldResult]
    tool_stats: ToolStats
    stop_mismatches: list[str]
    passed: bool


class GoldenReplayConfig(_FrozenModel):
    model: str
    temperature: float
    system_prompt: str


RunnerFactory = Callable[[EvalItemSpec], Awaitable[EvalTaskOutput]]


class _EvalItem:
    def __init__(self, spec: EvalItemSpec) -> None:
        self.id = spec.id
        self.input = spec.model_dump(mode="json")


class _OfflineProvider(CallbackStreamProvider):
    def __init__(self, model: str) -> None:
        super().__init__()
        self._model = model

    def get_default_model(self) -> str:
        return self._model

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        message = "Cassette replay attempted to call its offline seed provider"
        raise RuntimeError(message)


def merge_cassettes(cassette_dirs: list[Path]) -> Path:
    if not cassette_dirs:
        raise ValueError("At least one cassette directory is required")

    merged_dir = Path(tempfile.mkdtemp(prefix="modex-cassette-merge-"))
    merged_entries: list[CassetteEntry] = []
    payloads: dict[str, tuple[bytes, Path]] = {}
    for cassette_dir in cassette_dirs:
        manifest = CassetteManifest.model_validate_json(
            (cassette_dir / "index.json").read_text(encoding="utf-8")
        )
        for entry in manifest.entries:
            payload_path = cassette_dir / f"{entry.key}.json"
            payload = payload_path.read_bytes()
            previous = payloads.get(entry.key)
            if previous is not None:
                if previous[0] != payload:
                    raise ValueError(
                        f"Conflicting cassette payloads for key {entry.key}: "
                        f"{previous[1]} != {payload_path}"
                    )
                continue
            payloads[entry.key] = (payload, payload_path)
            merged_entries.append(entry)
            (merged_dir / payload_path.name).write_bytes(payload)

    manifest = CassetteManifest(trace_id="merged", entries=merged_entries, created_at=time.time())
    (merged_dir / "index.json").write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return merged_dir


class GoldenReplayRunner:
    def __init__(
        self,
        config: GoldenReplayConfig,
        *,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        self._config = config
        self._runner_factory = runner_factory

    @staticmethod
    def load_suite(golden_root: Path) -> list[GoldenCase]:
        cases: list[GoldenCase] = []
        for case_dir in sorted(path for path in golden_root.iterdir() if path.is_dir()):
            item_path = case_dir / "item.json"
            meta_path = case_dir / "meta.json"
            if not item_path.is_file() or not meta_path.is_file():
                raise FileNotFoundError(
                    f"Golden case {case_dir.name} requires item.json and meta.json"
                )
            EvalItemSpec.model_validate_json(item_path.read_text(encoding="utf-8"))
            GoldenMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
            cases.append(GoldenCase(name=case_dir.name, dir=case_dir))
        return cases

    @staticmethod
    def check_fingerprint(case: GoldenCase, constructed: FingerprintValues) -> None:
        recorded = GoldenMeta.model_validate_json(
            (case.dir / "meta.json").read_text(encoding="utf-8")
        )
        recorded_values = FingerprintValues.model_validate(
            recorded.model_dump(exclude={"recorded_at", "baseline"})
        )
        recorded_fields = recorded_values.model_dump()
        constructed_fields = constructed.model_dump()
        differences = [
            f"{field}: recorded={recorded_value!r}, "
            f"constructed={constructed_fields[field]!r}"
            for field, recorded_value in recorded_fields.items()
            if recorded_value != constructed_fields[field]
        ]
        if differences:
            raise ValueError("Golden fingerprint mismatch:\n" + "\n".join(differences))

    async def run_suite(self, golden_root: Path) -> list[CaseResult]:
        cases = self.load_suite(golden_root)
        cassette_dirs = [path for case in cases for path in self._cassette_dirs(case)]
        merged_cassette = merge_cassettes(cassette_dirs)
        return [await self._run_case(case, merged_cassette) for case in cases]

    async def run_case(self, case: GoldenCase) -> CaseResult:
        merged_cassette = merge_cassettes(self._cassette_dirs(case))
        return await self._run_case(case, merged_cassette)

    async def _run_case(
        self,
        case: GoldenCase,
        cassette_dir: Path,
    ) -> CaseResult:
        spec = EvalItemSpec.model_validate_json((case.dir / "item.json").read_text(encoding="utf-8"))
        meta = GoldenMeta.model_validate_json((case.dir / "meta.json").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory(prefix=f"modex-golden-{case.name}-") as raw_workspace:
            workspace = Path(raw_workspace)
            fingerprint_services = build_trace_only_services(
                workspace / ".fingerprint-trace",
                model=self._config.model,
            )
            assembled = await assemble_harness_agent(
                workspace=workspace,
                data_dir=workspace / ".fingerprint-runtime",
                provider=_OfflineProvider(self._config.model),
                toolset=spec.toolset,
                deny_tools=spec.deny_tools,
                runtime_services=fingerprint_services,
                governance_enabled=False,
            )
            tool_manager = assembled.tool_manager
            system_prompt = static_system_prompt(self._config.system_prompt)
            constructed = _fingerprint(self._config, system_prompt, tool_manager)
            self.check_fingerprint(case, constructed)
            await assembled.close()

            engine = CassetteReplayEngine(cassette_dir)
            engine.load()
            output = (
                await self._runner_factory(spec)
                if self._runner_factory is not None
                else await self._run_eval_runner(spec, engine)
            )

        clean_turns = len(output.turn_records) == len(spec.turns) and all(
            outcome.error is None and outcome.stop_reason is StopReason.COMPLETED
            for outcome in output.turn_records
        )
        world_ok = len(output.world_results) == len(spec.world_assertions) and all(
            result.passed for result in output.world_results
        )
        oracle_ok = bool(spec.world_assertions) or meta.baseline
        tool_floor = spec.metadata.get("tool_success_rate")
        threshold_ok = tool_floor is None or output.tool_stats.success_rate >= float(tool_floor)
        passed = all(
            (engine.misses == 0, clean_turns, world_ok, not output.stop_mismatches, oracle_ok, threshold_ok)
        )
        return CaseResult(
            case=case.name,
            fingerprint_ok=True,
            cassette_misses=engine.misses,
            turn_outcomes=output.turn_records,
            world_results=output.world_results,
            tool_stats=output.tool_stats,
            stop_mismatches=output.stop_mismatches,
            passed=passed,
        )

    @staticmethod
    def _cassette_dirs(case: GoldenCase) -> list[Path]:
        cassette_root = case.dir / "cassette"
        return sorted(
            path
            for path in cassette_root.iterdir()
            if path.is_dir() and (path / "index.json").is_file()
        )

    async def _run_eval_runner(
        self,
        spec: EvalItemSpec,
        engine: CassetteReplayEngine,
    ) -> EvalTaskOutput:
        runner = EvalRunner(
            provider=_OfflineProvider(self._config.model),
            system_prompt=self._config.system_prompt,
            mode="production",
            cassette=engine,
        )
        raw_output = await runner.task(item=_EvalItem(spec))
        return EvalTaskOutput.model_validate(raw_output)


def _fingerprint(
    config: GoldenReplayConfig,
    system_prompt: str,
    tool_manager: ToolManager,
) -> FingerprintValues:
    schemas = sorted(tool_manager.get_tool_descriptions(), key=lambda item: str(item["function"]["name"]))
    canonical_schemas = json.dumps(schemas, sort_keys=True, ensure_ascii=False, default=str)
    return FingerprintValues(
        model=config.model,
        temperature=config.temperature,
        tool_names=sorted(tool_manager.list_tools()),
        tool_schema_sha256=hashlib.sha256(canonical_schemas.encode("utf-8")).hexdigest(),
        prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        platform=sys.platform,
    )
