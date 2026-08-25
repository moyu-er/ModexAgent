"""Permanent keyless golden replay gates.

SABOTAGE QA: once per PR touching the eval harness, governance, or trace code,
run a red-then-green sensitivity check. PRIMARY (guaranteed model-visible):
temporarily append one word (e.g. ``" EXTRA"``) to the prompt returned by
``bot.eval.agent_harness.static_system_prompt``, replay ``file-pipeline`` via
``python -m bot.eval.cli replay-golden``, and expect RED through the
fingerprint gate (``prompt_sha256`` mismatch ValueError). SECONDARY
(conditional): a throwaway monkeypatch of
``bot.eval.agent_harness.main_agent_memory`` delegating to
``modex_agent.memory.presets.main_agent_memory(max_context_tokens=500)``
only
turns red on long trajectories -- ``keep_recent=10`` tool results and
``min_gain_tokens=20000`` keep short goldens such as ``file-pipeline``
(3 tool results) green, so use it only with a compaction-sensitive case.
Either way: restore the source, rerun the case to green, and retain only the
red-then-green transcript. A sabotage must never be committed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.eval import cli as eval_cli
from bot.eval.replay import (
    CaseResult,
    GoldenCase,
    GoldenMeta,
    GoldenReplayConfig,
    GoldenReplayRunner,
)
from bot.eval.task_spec import EvalItemSpec

BOT_PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = BOT_PROJECT_ROOT / "evals" / "golden"
# The committed golden suite was removed 2026-08-18 pending a v2 rebuild (see
# evals/README.md "Golden v2 (TODO)"). These gates parametrize over whatever
# cases exist, so the suite collects only the double-run identity test until
# cases are committed again — re-enable the workflow's PR/schedule triggers
# when that happens.
GOLDEN_CASES = (
    GoldenReplayRunner.load_suite(GOLDEN_ROOT) if GOLDEN_ROOT.is_dir() else []
)


def _runner(case: GoldenCase) -> GoldenReplayRunner:
    meta = GoldenMeta.model_validate_json(
        (case.dir / "meta.json").read_text(encoding="utf-8")
    )
    return GoldenReplayRunner(
        GoldenReplayConfig(
            model=meta.model,
            temperature=meta.temperature,
            system_prompt=eval_cli._GOLDEN_SYSTEM_PROMPT,
        )
    )


async def _run_case(case: GoldenCase) -> CaseResult:
    with eval_cli._stable_golden_message_serialization():
        return await _runner(case).run_case(case)


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.name)
async def test_golden_replay(case: GoldenCase) -> None:
    result = await _run_case(case)

    assert result.passed is True
    spec = EvalItemSpec.model_validate_json(
        (case.dir / "item.json").read_text(encoding="utf-8")
    )
    threshold = spec.metadata.get("tool_success_rate")
    if threshold is not None:
        assert result.tool_stats.success_rate >= float(threshold)


async def test_double_run_identity() -> None:
    if not GOLDEN_CASES:
        pytest.skip("no golden cases committed (v2 pending)")
    case = GOLDEN_CASES[-1]

    first = await _run_case(case)
    second = await _run_case(case)

    assert first.model_dump() == second.model_dump()
