"""The tracing capability's score-injector URL derivation: ``eval_ingestion_url``
takes precedence over the ``otel_endpoint`` derivation (the retired
``DefaultAgentFactory`` construction, migrated to ``TracingCapability.supply``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.plugins.capability import PoolSupplyAgentEntry, PoolSupplyView
from modex_agent.plugins.defaults.capabilities.tracing import (
    TraceSupply,
    TracingCapability,
    TracingCapabilityConfig,
)
from modex_agent.trace.otel_store import OtelSpanTraceStore


def _view_with_config(config: ObservabilityConfig, tmp_path: Path) -> PoolSupplyView:
    entry_config = TracingCapabilityConfig.from_observability(config).model_dump()
    return PoolSupplyView(
        pool_name="p",
        entries=(PoolSupplyAgentEntry(agent_name="root", config=entry_config),),
        trace_store=OtelSpanTraceStore(base_dir=tmp_path / "trace", backend=TraceBackend.FILE),
    )


def _injector_url(supply: TraceSupply | None) -> str | None:
    assert supply is not None
    injector = supply.score_injector
    if injector is None:
        return None
    return injector._ingestion_url  # noqa: SLF001


@pytest.mark.asyncio
async def test_score_injector_url_derived_from_otel_endpoint_when_eval_url_unset(
    tmp_path: Path,
) -> None:
    obs = ObservabilityConfig(
        eval_score_injection=True,
        trace_backend=TraceBackend.OTEL_HTTP,
        otel_endpoint="https://lf.example.invalid/api/public/otel/v1/traces",
    )
    supply = TracingCapability().supply(_view_with_config(obs, tmp_path))
    assert _injector_url(supply) == "https://lf.example.invalid/api/public/ingestion"


@pytest.mark.asyncio
async def test_score_injector_url_prefers_eval_ingestion_url_over_derivation(
    tmp_path: Path,
) -> None:
    obs = ObservabilityConfig(
        eval_score_injection=True,
        trace_backend=TraceBackend.OTEL_HTTP,
        otel_endpoint="http://localhost:4318/v1/traces",
        eval_ingestion_url="https://lf.example.invalid/api/public/ingestion",
    )
    supply = TracingCapability().supply(_view_with_config(obs, tmp_path))
    assert _injector_url(supply) == "https://lf.example.invalid/api/public/ingestion"


def test_headers_threaded_into_injector(tmp_path: Path) -> None:
    obs = ObservabilityConfig(
        eval_score_injection=True,
        trace_backend=TraceBackend.OTEL_HTTP,
        otel_endpoint="https://lf.example.invalid/v1/traces",
        otel_headers={"Authorization": "Basic abc"},
    )
    supply: Any = TracingCapability().supply(_view_with_config(obs, tmp_path))
    assert supply.score_injector._headers == {"Authorization": "Basic abc"}  # noqa: SLF001
