"""DefaultAgentFactory derives the L2ScoreInjector ingestion URL with
``eval_ingestion_url`` taking precedence over the ``otel_endpoint`` derivation."""

from pathlib import Path

import pytest

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.root_span_hook import RootSpanHook


def _descriptor() -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name="main"),
        execution_strategy=ExecutionStrategyKind.REACT,
        comm_kind=AgentCommKind.NORMAL,
        system_prompt_template="",
    )


def _root_injector_url(instance: object) -> str | None:
    runner = instance.pipeline.hook_runner  # type: ignore[attr-defined]
    assert runner is not None
    for spec in runner.hook_specs:
        if isinstance(spec.hook, RootSpanHook):
            injector = spec.hook._score_injector
            if injector is None:
                return None
            return injector._ingestion_url
    raise AssertionError("RootSpanHook not registered")


@pytest.mark.asyncio
async def test_score_injector_url_derived_from_otel_endpoint_when_eval_url_unset(tmp_path: Path):
    obs = ObservabilityConfig(
        eval_score_injection=True,
        trace_backend=TraceBackend.OTEL_HTTP,
        otel_endpoint="https://lf.example.invalid/api/public/otel/v1/traces",
    )
    factory = DefaultAgentFactory(
        observability_config=obs,
        trace_store=OtelSpanTraceStore(tmp_path),
    )

    instance = await factory.create_agent(_descriptor(), broker=None)

    assert _root_injector_url(instance) == "https://lf.example.invalid/api/public/ingestion"


@pytest.mark.asyncio
async def test_score_injector_url_prefers_eval_ingestion_url_over_derivation(tmp_path: Path):
    obs = ObservabilityConfig(
        eval_score_injection=True,
        trace_backend=TraceBackend.OTEL_HTTP,
        otel_endpoint="http://localhost:4318/v1/traces",
        eval_ingestion_url="https://lf.example.invalid/api/public/ingestion",
    )
    factory = DefaultAgentFactory(
        observability_config=obs,
        trace_store=OtelSpanTraceStore(tmp_path),
    )

    instance = await factory.create_agent(_descriptor(), broker=None)

    assert _root_injector_url(instance) == "https://lf.example.invalid/api/public/ingestion"
