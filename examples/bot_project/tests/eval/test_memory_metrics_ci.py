from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Final

import pytest
from bot.eval.memory_metrics import reduce_memory_spans
from pydantic import BaseModel, ConfigDict

from modex_agent.memory.context import RuntimeInfoKey
from modex_agent.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from modex_agent.memory.injection.full_injection import FullInjectionPolicy
from modex_agent.memory.layers.config import CoreMemoryConfig, MemoryLayerConfigSet
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.system import MemorySystemContextManager, create_memory_system
from modex_agent.trace.store import SpanModel

pytestmark = pytest.mark.memory_metrics_ci

_FIXTURES: Final = Path(__file__).parent / "fixtures"


class _SnapshotMemory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    file_name: str
    content: str


class _ProbeSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    target_user_id: str
    contaminant_user_id: str
    memories: tuple[_SnapshotMemory, ...]
    expected_evidence: tuple[str, ...]
    forbidden_evidence: tuple[str, ...]


def _load_spans() -> list[SpanModel]:
    lines = (_FIXTURES / "memory_spans_v1.jsonl").read_text(encoding="utf-8").splitlines()
    return [SpanModel.model_validate_json(line) for line in lines if line]


def test_frozen_memory_spans_reduce_to_v1_golden_metrics() -> None:
    metrics = reduce_memory_spans(_load_spans())

    assert metrics.memory_compression_ratio == 1.0
    assert metrics.memory_compression_monotonic is True
    assert metrics.prefix_stable is True
    assert metrics.memory_write_cost_usd == 2.1445
    assert metrics.memory_read_latency_ms is not None
    assert metrics.memory_read_latency_ms.min_ms == 10.0
    assert metrics.memory_read_latency_ms.mean_ms == 15.0
    assert metrics.memory_read_latency_ms.max_ms == 20.0
    assert metrics.injection_retention == 0.66


async def test_probe_snapshot_load_recalls_evidence_without_cross_user_pollution(
    tmp_path: Path,
) -> None:
    snapshot = _ProbeSnapshot.model_validate_json(
        (_FIXTURES / "probe_memory_snapshot_v1.json").read_text(encoding="utf-8")
    )
    config = MemoryLayerConfigSet(archive=None, core=CoreMemoryConfig())
    memory_system = create_memory_system(tmp_path / "memory", config=config)
    await memory_system.initialize()
    try:
        core = memory_system.layers.core
        assert core is not None
        for memory in snapshot.memories:
            await core.apply_update(
                context=MemoryContext(
                    session_id=f"fixture-{memory.user_id}",
                    user_id=memory.user_id,
                ),
                update=MemoryUpdate(
                    file_name=memory.file_name,
                    content=memory.content,
                    mode=str(MemoryUpdateMode.SECTION_REPLACE),
                    reason="frozen probe fixture",
                ),
            )
        manager = MemorySystemContextManager(
            memory_system,
            default_user_id=snapshot.target_user_id,
            injection_policy=FullInjectionPolicy(),
        )

        state = await manager.load(
            "probe-session",
            runtime_info={RuntimeInfoKey.MESSAGE: snapshot.query},
        )
    finally:
        await memory_system.close()

    try:
        scoring_spec = find_spec("bot.eval.probes.scoring")
    except ModuleNotFoundError:
        scoring_spec = None
    assert scoring_spec is not None, "probe scoring module must exist"
    scoring = import_module("bot.eval.probes.scoring")
    recalls_all_evidence = getattr(scoring, "recalls_all_evidence", None)
    has_no_isolation_contamination = getattr(
        scoring,
        "has_no_isolation_contamination",
        None,
    )
    assert callable(recalls_all_evidence)
    assert callable(has_no_isolation_contamination)
    assert recalls_all_evidence(state.system_prompt, snapshot.expected_evidence)
    assert has_no_isolation_contamination(
        state.system_prompt,
        snapshot.forbidden_evidence,
    )
