from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from modex_agent.core.scope import MemoryContext
from modex_agent.memory import cleanup_hooks
from modex_agent.memory.cleanup import CleanupResult
from modex_agent.memory.core.models import CompressionReason
from modex_agent.memory.hooks import MemoryHookContext


def _hook_context(*, triggered: bool) -> MemoryHookContext:
    return MemoryHookContext(
        memory_context=MemoryContext(session_id="metrics-session"),
        cleanup_result=CleanupResult(
            triggered=triggered,
            messages_kept=5,
            messages_pruned=3,
            tokens_before=50,
            tokens_after=20,
            compact_generated=True,
            reason=CompressionReason.TOKEN_PRESSURE,
        ),
    )


async def test_triggered_cleanup_appends_one_typed_json_line(tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    hook = cleanup_hooks.CleanupMetricsHook(metrics_dir=metrics_dir)

    await hook.on_cleanup_finished(_hook_context(triggered=True))

    lines = (metrics_dir / "cleanup.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == {
        "ts",
        "session_id",
        "reason",
        "messages_kept",
        "messages_pruned",
        "tokens_before",
        "tokens_after",
        "tokens_saved",
        "compact_generated",
        "prune_ratio",
    }
    utc_offset = datetime.fromisoformat(record["ts"]).utcoffset()
    assert utc_offset is not None
    assert utc_offset.total_seconds() == 0
    assert record["session_id"] == "metrics-session"
    assert record["reason"] == CompressionReason.TOKEN_PRESSURE.value
    assert record["messages_kept"] == 5
    assert record["messages_pruned"] == 3
    assert record["tokens_before"] == 50
    assert record["tokens_after"] == 20
    assert record["tokens_saved"] == 30
    assert record["compact_generated"] is True
    assert record["prune_ratio"] == 3 / 8


async def test_write_failure_logs_warning_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    hook = cleanup_hooks.CleanupMetricsHook(metrics_dir=blocked_parent / "metrics")

    with caplog.at_level(logging.WARNING, logger="modex_agent.memory.cleanup_hooks"):
        await hook.on_cleanup_finished(_hook_context(triggered=True))

    assert any("Failed to write cleanup metric" in record.message for record in caplog.records)


async def test_non_triggered_cleanup_does_not_write(tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    hook = cleanup_hooks.CleanupMetricsHook(metrics_dir=metrics_dir)

    await hook.on_cleanup_finished(_hook_context(triggered=False))

    assert not metrics_dir.exists()
