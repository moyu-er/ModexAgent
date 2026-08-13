"""Regression tests: transcript writes route to the workspace from the resolver cell.

The bind_workspace_root ContextVar is lost across the broker-queue task boundary
(between the business dispatcher and the framework resident consumer task).
Before the fix, WorkspaceScopedTranscriptStore.append relied exclusively on that
ctxvar, so a transcript write inside the agent turn (consumer task lineage)
landed under Path.cwd() instead of the conversation's workspace.

The fix: the emitter holds a sessions_dir_provider derived from the per-workspace
resolver cell (pipeline.workspace_manager) — the SAME source memory/runtime/
output use. It passes the resolved sessions_dir to store.append explicitly,
surviving any task boundary. The ctxvar fallback is kept for backward compat.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import CompositeEmitter, WebBotEmitter
from bot.webui.events import AssistantTextEvent
from modex_agent.core.emitter import EmitterConfig
from modex_agent.workspace.runtime import bind_workspace_root, is_workspace_root_bound

_DATA_DIR_NAME = ".modex"


# ── Store: explicit sessions_dir │ ctxvar fallback ───────────────────────────


def _build_store() -> WorkspaceScopedTranscriptStore:
    return WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)


def _event(sid: str = "conv.main") -> AssistantTextEvent:
    return AssistantTextEvent(session_id=sid, agent_name="main", turn_id="t1", text="x")


@pytest.mark.asyncio
async def test_append_with_explicit_sessions_dir_routes_to_that_dir(
    tmp_path: Path,
) -> None:
    """sessions_dir=X → transcript lands under X even when ctxvar unbound."""
    ws_a = tmp_path / "ws_a"
    sessions_a = ws_a / _DATA_DIR_NAME / "sessions"
    sessions_a.mkdir(parents=True)

    store = _build_store()

    # No ctxvar binding at all (simulates consumer task).
    assert not is_workspace_root_bound()
    await store.append("conv.main", _event(), sessions_dir=sessions_a)

    # Transcript must land under ws_a.
    expected = sessions_a / "main" / "conv.main.jsonl"
    assert expected.exists(), (
        f"explicit sessions_dir must route to {expected}"
    )


@pytest.mark.asyncio
async def test_append_without_sessions_dir_falls_back_to_ctxvar(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Without sessions_dir, the store still falls back to ctxvar (backward compat).
    Unbound ctxvar → cwd + warning."""
    store = _build_store()
    with caplog.at_level(logging.WARNING, logger="bot.service.workspace_store"):
        await store.append("conv.main", _event())
    assert any("[ws-partition]" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_append_with_explicit_sessions_dir_silences_ctxvar_warning(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """With explicit sessions_dir, the unbound-ctxvar warning is NOT emitted
    (the store trusts the resolver)."""
    sessions_dir = tmp_path / _DATA_DIR_NAME / "sessions"
    sessions_dir.mkdir(parents=True)
    store = _build_store()
    with caplog.at_level(logging.WARNING, logger="bot.service.workspace_store"):
        await store.append("conv.main", _event(), sessions_dir=sessions_dir)
    assert not any("[ws-partition]" in r.message for r in caplog.records)


# ── Emitter: sessions_dir_provider survives consumer-task lineage ────────────


def _fake_output() -> MagicMock:
    out = MagicMock()
    out.send_envelope = AsyncMock()
    return out


@pytest.mark.asyncio
async def test_emitter_with_provider_writes_to_cell_workspace(
    tmp_path: Path,
) -> None:
    """WebBotEmitter with sessions_dir_provider → ws_a. Append from an unbound
    task (simulating the resident consumer lineage) must land under ws_a."""
    ws_a = tmp_path / "ws_a"
    ws_a.mkdir()
    sessions_a = ws_a / _DATA_DIR_NAME / "sessions"
    sessions_a.mkdir(parents=True)

    store = _build_store()
    emitter = WebBotEmitter(
        output_adapter=_fake_output(),
        session_id="conv.main",
        transcript_store=store,
        sessions_dir_provider=lambda: sessions_a,
    )

    # Run in a task created WITHOUT bind_workspace_root — the exact scenario
    # that broke before (resident consumer task does not inherit the dispatcher's
    # ctxvar binding).
    async def consumer_turn() -> None:
        assert not is_workspace_root_bound()
        await emitter.emit_content("hello from cell")
        await emitter.emit_stream_end(resuming=False)

    await asyncio.create_task(consumer_turn())

    transcript = sessions_a / "main" / "conv.main.jsonl"
    assert transcript.exists(), (
        f"emitter with provider must write under sessions_a, not cwd"
    )


@pytest.mark.asyncio
async def test_emitter_without_provider_still_falls_back_to_ctxvar(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Backward compat: emitter without provider → store ctxvar fallback
    (unbound → cwd + warning)."""
    store = _build_store()
    emitter = WebBotEmitter(
        output_adapter=_fake_output(),
        session_id="conv.main",
        transcript_store=store,
    )
    with caplog.at_level(logging.WARNING, logger="bot.service.workspace_store"):
        async def consumer_turn() -> None:
            await emitter.emit_content("fallback")
            await emitter.emit_stream_end(resuming=False)

        await asyncio.create_task(consumer_turn())
    assert any("[ws-partition]" in r.message for r in caplog.records)


# ── CompositeEmitter forwards the provider ────────────────────────────────────


@pytest.mark.asyncio
async def test_composite_emitter_forwards_provider_to_web_child(
    tmp_path: Path,
) -> None:
    """CompositeEmitter.set_sessions_dir_provider forwards to every
    WebBotEmitter child, so a transcript tool call emitted via the composite
    lands in the cell workspace."""
    ws_a = tmp_path / "ws_a"
    ws_a.mkdir()
    sessions_a = ws_a / _DATA_DIR_NAME / "sessions"
    sessions_a.mkdir(parents=True)

    store = _build_store()
    inner = WebBotEmitter(
        output_adapter=_fake_output(),
        session_id="conv.main",
        transcript_store=store,
    )
    composite: CompositeEmitter[Any] = CompositeEmitter([inner])
    composite.set_sessions_dir_provider(lambda: sessions_a)

    async def consumer_turn() -> None:
        assert not is_workspace_root_bound()
        await composite.emit_content("via composite")
        await composite.emit_stream_end(resuming=False)

    await asyncio.create_task(consumer_turn())

    transcript = sessions_a / "main" / "conv.main.jsonl"
    assert transcript.exists(), (
        "composite must forward provider to inner WebBotEmitter"
    )
