"""Tests for :func:`modex_agent.trace.factory.build_trace_hooks`."""

from __future__ import annotations

from pathlib import Path

from modex_agent.hook.abc import HookErrorPolicy, HookSpec
from modex_agent.ioc.configs.observability import (
    ObservabilityConfig,
    TraceBackend,
    TraceSpanMode,
)
from modex_agent.trace.agent_start_hook import AgentStartSpanHook
from modex_agent.trace.approval_span_hook import ApprovalSpanHook
from modex_agent.trace.chat_span_hook import ChatSpanHook
from modex_agent.trace.factory import build_trace_hooks
from modex_agent.trace.handoff_span_hook import HandoffSpanHook
from modex_agent.trace.iteration_span_hook import IterationSpanHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.tool_span_hook import ToolSpanHook


def _config(
    tier: TraceSpanMode = TraceSpanMode.STANDARD,
    backend: TraceBackend = TraceBackend.FILE,
) -> ObservabilityConfig:
    return ObservabilityConfig(trace_backend=backend, trace_spans=tier)


def _build(
    tier: TraceSpanMode,
    backend: TraceBackend = TraceBackend.FILE,
    *,
    store: OtelSpanTraceStore | None = None,
) -> list[HookSpec]:
    return build_trace_hooks(
        _config(tier, backend),
        model=None,
        provider_name=None,
        request_params=None,
        score_injector=None,
        store=store,
    )


def test_minimal_tier_has_only_root_hook(tmp_path: Path) -> None:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    specs = _build(TraceSpanMode.MINIMAL, store=store)
    assert len(specs) == 1
    assert isinstance(specs[0].hook, RootSpanHook)


def test_standard_tier_has_5_hooks(tmp_path: Path) -> None:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    specs = _build(TraceSpanMode.STANDARD, store=store)
    assert len(specs) == 5
    expected = {RootSpanHook, ChatSpanHook, ToolSpanHook, HandoffSpanHook, ApprovalSpanHook}
    assert {type(s.hook) for s in specs} == expected


def test_full_tier_has_7_hooks(tmp_path: Path) -> None:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    specs = _build(TraceSpanMode.FULL, store=store)
    assert len(specs) == 7
    expected = {
        RootSpanHook,
        ChatSpanHook,
        ToolSpanHook,
        HandoffSpanHook,
        ApprovalSpanHook,
        AgentStartSpanHook,
        IterationSpanHook,
    }
    assert {type(s.hook) for s in specs} == expected


def test_root_hook_is_first(tmp_path: Path) -> None:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    for tier in (TraceSpanMode.MINIMAL, TraceSpanMode.STANDARD, TraceSpanMode.FULL):
        specs = _build(tier, store=store)
        assert isinstance(specs[0].hook, RootSpanHook), f"root not first in {tier}"


def test_tool_hook_before_handoff_hook(tmp_path: Path) -> None:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    specs = _build(TraceSpanMode.FULL, store=store)
    tool_idx = next(i for i, s in enumerate(specs) if isinstance(s.hook, ToolSpanHook))
    handoff_idx = next(
        i for i, s in enumerate(specs) if isinstance(s.hook, HandoffSpanHook)
    )
    assert tool_idx < handoff_idx


def test_off_backend_returns_empty() -> None:
    for tier in (TraceSpanMode.MINIMAL, TraceSpanMode.STANDARD, TraceSpanMode.FULL):
        assert _build(tier, backend=TraceBackend.OFF) == []


def test_build_trace_hooks_returns_empty_when_store_none() -> None:
    """When store is None, no hooks are registered regardless of tier."""
    for tier in (TraceSpanMode.MINIMAL, TraceSpanMode.STANDARD, TraceSpanMode.FULL):
        assert _build(tier, store=None) == []


def test_all_hooks_share_same_session(tmp_path: Path) -> None:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    specs = _build(TraceSpanMode.FULL, store=store)
    sessions = {id(s.hook._session) for s in specs}  # type: ignore[attr-defined]
    assert len(sessions) == 1
    assert isinstance(specs[0].hook._session, TraceSessionState)  # type: ignore[attr-defined]


def test_each_hook_wrapped_with_log_policy(tmp_path: Path) -> None:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    specs = _build(TraceSpanMode.FULL, store=store)
    assert all(s.on_error == HookErrorPolicy.LOG for s in specs)
    assert all(isinstance(s, HookSpec) for s in specs)


def test_environment_version_tags_threaded_to_hooks(tmp_path: Path) -> None:
    """build_trace_hooks threads environment/version/tags from config into every hook."""
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    config = ObservabilityConfig(
        trace_backend=TraceBackend.FILE,
        trace_spans=TraceSpanMode.STANDARD,
        environment="staging",
        version="2.1.0",
        tags=["eval", "math-qa"],
    )
    specs = build_trace_hooks(
        config,
        model=None,
        provider_name=None,
        request_params=None,
        score_injector=None,
        store=store,
    )
    assert len(specs) > 0
    for spec in specs:
        hook = spec.hook
        assert hook._environment == "staging"  # type: ignore[attr-defined]
        assert hook._version == "2.1.0"  # type: ignore[attr-defined]
        assert hook._tags == ["eval", "math-qa"]  # type: ignore[attr-defined]


def test_environment_version_tags_default_when_unset(tmp_path: Path) -> None:
    """When config does not set environment/version/tags, hooks get defaults."""
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    specs = _build(TraceSpanMode.MINIMAL, store=store)
    assert len(specs) == 1
    hook = specs[0].hook
    assert hook._environment == "default"  # type: ignore[attr-defined]
    assert hook._version is None  # type: ignore[attr-defined]
    assert hook._tags == []  # type: ignore[attr-defined]
